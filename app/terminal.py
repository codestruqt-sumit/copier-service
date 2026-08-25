"""TerminalGateway - the one modular boundary in front of the trading terminal.

Everything Selenium lives behind this class; the executor only sees plain dicts.
The building blocks are the ALREADY-VALIDATED bot modules copied as-is into
config/ core/ browser/ trading/ (order ticket, chart panel, accounts, workspace,
positions, orders, session guard). This file only orchestrates them - it does not
re-implement any tested behaviour.

Only ONE thread (the TerminalWorker) may ever call into a gateway instance: the
terminal is a single shared UI, so all interaction is serialized by design.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

log = logging.getLogger("copier.terminal")

_IMPORT_ERROR: Optional[str] = None
try:  # selenium + the bot modules; the app must still boot (dashboard/receiver) without them
    from browser.session import BrowserSession
    from config.settings import Settings as BotSettings
    from trading.accounts import AccountPanel
    from trading.composite import CompositeTrader
    from trading.order_ticket import OrderTicket
    from trading.orders_panel import OrdersPanel
    from trading.positions import PositionsPanel
    from trading import tables
    from trading.session_guard import SessionGuard
    from trading.trade_panel import ChartTradePanel
    from trading.workspace import WorkspaceTabs
except Exception as exc:  # noqa: BLE001  pragma: no cover
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

TRADOVATE_NEEDLE = "trader.tradovate.com"


class AbortRequested(RuntimeError):
    """Raised at a gateway CHECKPOINT when the executor asks the in-flight action to
    stop (operator abort or the per-action timeout). Checkpoints sit BETWEEN
    operations (loop boundaries) - never inside a click/confirm sequence - so an
    abort can never interrupt an order submission halfway."""

# Action kinds that leave a resting order in the book (reported as 'executing').
RESTING_KINDS = {"place_limit", "place_stop", "place_bid", "place_ask"}

# Chart-panel-driven kinds eligible for the safe pre-click retry. Only bid/ask still
# use the chart panel; market/limit/stop/exit/flatten go through the OrderTicket, which
# stays reliable even with the Order Ticket widget open (the chart panel does not).
_PANEL_KINDS = {"place_bid", "place_ask"}

# Failure markers that PROVE nothing was clicked yet (safety refusals / setup steps).
_PRECLICK_MARKERS = ("refusing to click", "symbol switch failed", "set qty failed")


def _trim_price(raw: str | None) -> str | None:
    """'2400.50000000' -> '2400.5' - the ticket types exactly what we hand it."""
    if raw in (None, ""):
        return None
    try:
        text = format(Decimal(str(raw)).normalize(), "f")
        return text
    except (InvalidOperation, ValueError):
        return str(raw)


class TerminalGateway:
    def __init__(self, fast_market: bool = True, net_verify_sec: float = 12.0):
        # fast_market: place market orders via the panel's one-click Buy Mkt / Sell Mkt
        # button (fast), falling back to the OrderTicket if the panel can't fire.
        self.fast_market = fast_market
        # How long _await_net waits for the Positions widget to reflect a fill. Bigger on
        # a slow box (returns early on success, so it never slows a fast fill).
        self.net_verify_sec = float(net_verify_sec or 12.0)
        # Optional callable set by the executor for the CURRENT action only: returns a
        # reason string when the action should stop (operator abort / timeout), else
        # None. Polled at _checkpoint() sites - loop boundaries, never mid-click.
        self.abort_check = None
        # Set by handlers the moment an order is LIVE on the terminal (send accepted),
        # so execute() can annotate order-live vs verified latency separately.
        self._live_at = None
        self.available = _IMPORT_ERROR is None
        self.unavailable_reason = _IMPORT_ERROR
        self.connected = False
        self.session = None
        self.driver = None
        self.guard = None
        self.accounts = None
        self.tabs = None
        self.panel = None
        self.positions = None
        self.orders = None
        self.composite = None
        self.ticket = None

    # --- connection --------------------------------------------------------------

    def connect(self) -> tuple[bool, str]:
        """Attach to the already-open debug browser and focus the Tradovate tab.

        attach_only: we NEVER launch a browser here - the human opened and logged
        into the terminal; the copier only drives it.
        """
        if not self.available:
            return False, f"terminal driver unavailable: {self.unavailable_reason}"
        try:
            settings = BotSettings.load()
            settings.ensure_dirs()
            self.session = BrowserSession(settings.browser)
            self.driver = self.session.start(attach_only=True)
            if not self.session.focus_tab(TRADOVATE_NEEDLE):
                self.connected = False
                return False, "no trader.tradovate.com tab in the debug browser"
            self.guard = SessionGuard(self.driver, settings.session)
            self.accounts = AccountPanel(self.driver)
            self.tabs = WorkspaceTabs(self.driver)
            self.panel = ChartTradePanel(self.driver)
            self.positions = PositionsPanel(self.driver)
            self.orders = OrdersPanel(self.driver)
            self.composite = CompositeTrader(self.driver)
            self.ticket = OrderTicket(self.driver)  # the hardened, ticket-open-safe path
            self.connected = True
            version = self.session.cdp_version() or {}
            return True, f"attached to {version.get('Browser', 'browser')}"
        except Exception as exc:  # noqa: BLE001
            self.connected = False
            return False, f"{type(exc).__name__}: {exc}"

    def ensure_connected(self) -> tuple[bool, str]:
        if self.connected and self.driver is not None:
            try:
                _ = self.driver.current_url  # cheap liveness probe
                return True, "connected"
            except Exception:  # noqa: BLE001 - stale driver, reattach below
                self.connected = False
        return self.connect()

    # --- mandatory checks ----------------------------------------------------------

    def login_check(self) -> tuple[bool, str]:
        """Force-checks the session markers (logged-OUT markers are authoritative)."""
        try:
            status = self.guard.check_status(force=True)
            return bool(status.logged_in), status.detail
        except Exception as exc:  # noqa: BLE001
            self.connected = False
            return False, f"login check failed: {type(exc).__name__}: {exc}"

    def active_account(self) -> Optional[str]:
        result = self.accounts.active_account()
        return (result.data or {}).get("active") if result.ok else None

    def ensure_account(self, account_ref: str) -> tuple[bool, str]:
        """The account check: verify the right account is selected, switch if not."""
        try:
            active = self.active_account()
            if active == account_ref:
                return True, f"{account_ref} already active"
            # Clean path: click the account entry directly (the dropdown never opens),
            # avoiding the open-toggle + veil-close dance that desynced React's menu
            # state and wedged the selector over many switches.
            switched = self.accounts.switch_to_direct(account_ref, dry_run=False)
            if switched.ok:
                return True, f"switched {active or '?'} -> {account_ref}"
            # Fallback to the classic open-menu switch (account switching is idempotent,
            # so retrying the same target can never place or double anything).
            legacy = self.accounts.switch_to(account_ref, dry_run=False)
            if not legacy.ok:
                return False, f"account switch failed: {switched.error or legacy.error}"
            return True, f"switched {active or '?'} -> {account_ref} (fallback)"
        except Exception as exc:  # noqa: BLE001
            self.connected = False
            return False, f"account check failed: {type(exc).__name__}: {exc}"

    # --- execution -------------------------------------------------------------------

    # One JS round-trip: the chart trade panel's current contract symbol (the panel is
    # the lm_stack anchored by its unique 'Buy Bid' button; the DOM ladder has none).
    _PANEL_SYMBOL_JS = r"""
        var stack=null;
        document.querySelectorAll('.lm_stack').forEach(function(s){
          if(stack) return;
          var btns=s.querySelectorAll('div.btn');
          for(var i=0;i<btns.length;i++){
            if((btns[i].textContent||'').trim()==='Buy Bid'){stack=s;return;}
          }
        });
        if(!stack) return null;
        var el=stack.querySelector('.contract-symbol');
        return el ? (el.textContent||'').trim() : null;
    """

    def _ensure_tab(self, symbol: str):
        """Workspace switch with a cheap early-exit: ONE JS read of the panel's current
        symbol; if it already matches, skip the full switch (snapshot + match + click,
        ~1-2s). Safe even if the read is ever wrong - the panel's own expect_symbol
        guard still refuses to click on a mismatch."""
        want = (symbol or "").strip().upper()
        try:
            current = self.driver.execute_script(self._PANEL_SYMBOL_JS)
        except Exception:  # noqa: BLE001 - fall through to the real switch
            current = None
        if current and (current.upper().startswith(want) or want.startswith(current.upper())):
            from types import SimpleNamespace
            return SimpleNamespace(ok=True, error=None, data={"skipped": True})
        return self.tabs.switch_to(symbol)

    def _mark_live(self) -> None:
        """Call the moment a send is ACCEPTED by the terminal - order-live time."""
        self._live_at = time.monotonic()

    def _annotate_latency(self, result: dict, t0: float) -> dict:
        """Append '[live +X.Xs, verified +Y.Ys]' to successful results: 'live' is when
        the order reached the terminal (what prices slippage), 'verified' is when the
        outcome was confirmed. Keeps the two numbers from blurring into one."""
        try:
            if result.get("outcome") in ("filled", "executing") and self._live_at is not None:
                result["detail"] = (result.get("detail") or "") + (
                    f" [live +{self._live_at - t0:.1f}s, "
                    f"verified +{time.monotonic() - t0:.1f}s]")
        except Exception:  # noqa: BLE001 - annotation must never break a result
            pass
        return result

    def _checkpoint(self) -> None:
        """Between-operations stop point: raises AbortRequested when the executor set
        an abort/timeout for the current action. Cheap (one callable poll)."""
        check = self.abort_check
        if check is None:
            return
        try:
            reason = check()
        except Exception:  # noqa: BLE001 - a broken check must never break execution
            return
        if reason:
            raise AbortRequested(reason)

    def execute(self, action: dict) -> dict:
        """Run one queue action. Returns {outcome, order_ref, detail}.

        outcome: 'filled' (position moved / flatten done), 'executing' (resting
        order placed and verified working), 'failed'. The symbol check is embedded
        in every validated flow used here: composite/panel paths switch the
        workspace tab and verify `expect_symbol`; the order ticket sets and
        verifies the symbol inside the ticket itself.
        """
        kind = action["kind"]
        t0 = time.monotonic()
        self._live_at = None   # handlers stamp this the moment the send is accepted
        try:
            handler = {
                "place_market": self._market,
                "place_bid": self._bid_ask,
                "place_ask": self._bid_ask,
                "place_limit": self._resting,
                "place_stop": self._resting,
                "place_stop_limit": self._stop_limit,
                "exit_symbol": self._exit_symbol,
                "flatten_all": self._flatten_all,
            }.get(kind)
            if handler is None:
                return {"outcome": "failed", "order_ref": None,
                        "detail": f"unknown action kind '{kind}'"}
            self._checkpoint()
            result = handler(action)
            if kind in _PANEL_KINDS and self._refused_before_click(result):
                # The chart panel can render blank for a beat right after an account
                # switch or order-ticket use; the guard refuses to click then. Nothing
                # was fired, so ONE settled retry is safe. (Never retry after a click
                # may have happened - that could double an order.)
                log.info("pre-click refusal on %s - settling and retrying once", kind)
                self._checkpoint()
                time.sleep(2.0)
                result = handler(action)
            return self._annotate_latency(result, t0)
        except AbortRequested as exc:
            # Cooperative stop between operations. An operation ALREADY performed may
            # have reached the terminal - never re-send; the operator verifies.
            log.warning("action aborted at checkpoint: %s", exc)
            return {"outcome": "failed", "order_ref": None,
                    "detail": f"ABORTED: {exc} - an order may already be on the "
                              f"terminal; VERIFY manually (not retried)"}
        except Exception as exc:  # noqa: BLE001
            self.connected = False
            return {"outcome": "failed", "order_ref": None,
                    "detail": f"{type(exc).__name__}: {exc}"}

    # --- handlers (each built ONLY from validated flows) ------------------------------

    def _net_read(self, symbol: str, scroll: bool) -> tuple[bool, int]:
        """(row_found, net). found=False means the row was NOT in the read - which with
        scroll=False only proves it wasn't among the VISIBLE rows."""
        got = self.positions.get(symbol, scroll=scroll)
        if got.ok and got.data.get("found") and got.data.get("position"):
            return True, got.data["position"].get("net_pos") or 0
        return False, 0

    def _net(self, symbol: str) -> int:
        # Visible-rows-only (scroll=False - never drags the scrollbar). ONLY for the
        # tight polls; every DECISION read must use _net_thorough: the Positions widget
        # keeps one row per traded symbol all session, and rows below the fold are
        # VIRTUALIZED OUT of the DOM - a visible-only read returns 0 for a real position
        # (proven live: MGCZ6 fills read net 0->0 while MNQU6 verified fine).
        return self._net_read(symbol, scroll=False)[1]

    def _net_thorough(self, symbol: str) -> int:
        """The truth: visible read first (free); if the row is not among the visible
        rows, re-read WITH scrolling. The scrolled read only drags when the grid really
        overflows (tables.read_table no-ops the drag when not scrollable), and the drag
        is hit-test-guarded - so this is cheap when rows fit and correct when they don't."""
        found, net = self._net_read(symbol, scroll=False)
        if found:
            return net
        found2, net2 = self._net_read(symbol, scroll=True)
        if found2:
            log.info("positions row %s was OFF-SCREEN - visible read missed it (net=%s)",
                     symbol, net2)
        return net2

    @staticmethod
    def _submitted_note(result) -> str | None:
        """If the ticket sent an order and then FAILED its own verification, say so
        LOUDLY - never retry (duplicate risk); the operator checks the terminal.

        Only meaningful for FAILED results: a SUCCESSFUL ticket send also carries
        meta.submitted=True (every sent order does), and treating that as a failure
        misreported every successful ticket-market as "SUBMITTED but not verified"
        (the empty trailing error was the tell - result.error was None)."""
        if not result.ok and result.meta.get("submitted"):
            return ("order SUBMITTED but not verified - check the terminal / cancel manually: "
                    + (result.error or ""))
        return None

    def _await_net(self, symbol: str, expected: int, timeout: float | None = None,
                   step: float = 0.25) -> bool:
        """Poll the net position until it hits `expected` (or timeout). Returns as soon
        as the fill registers. The window (net_verify_sec, default 12s) is generous because
        the Positions widget can take several seconds to reflect a fill - especially on a
        slower VM - and a too-tight window reports real fills as 'not verified'."""
        timeout = self.net_verify_sec if timeout is None else timeout
        deadline = time.monotonic() + timeout
        last_thorough = 0.0
        while True:
            found, net = self._net_read(symbol, scroll=False)
            # A VISIBLE row is real data - accept its match. A visible-only 0 with the
            # row absent is NOT proof (it may be off-screen), so confirm those via a
            # scrolled read - immediately on the first miss, then at most every 5s, and
            # always once more at the deadline.
            if found and net == expected:
                return True
            now = time.monotonic()
            if (now - last_thorough) >= 5.0 or now >= deadline:
                last_thorough = now
                if self._net_thorough(symbol) == expected:
                    return True
            if time.monotonic() >= deadline:
                return False
            self._checkpoint()   # abort/timeout can stop the wait between polls
            time.sleep(step)

    def _market(self, action: dict) -> dict:
        symbol, side, qty = action["symbol"], action["side"], int(action["qty"])
        before = self._net_thorough(symbol)   # baseline must see off-screen rows too
        expected = before + (qty if side == "buy" else -qty)

        # FAST PATH: the panel's one-click Buy Mkt / Sell Mkt (the button a human uses).
        # Only fall back to the ticket when the panel fired NOTHING (a clean pre-click
        # failure); once it may have clicked we never re-send (duplicate risk).
        if self.fast_market:
            fast = self._market_via_panel(symbol, side, qty, before, expected)
            if fast is not None:
                return fast

        return self._market_via_ticket(symbol, side, qty, before, expected)

    def _market_via_panel(self, symbol, side, qty, before, expected) -> dict | None:
        """Returns a result dict if the panel handled it (fired), or None if nothing was
        sent (pre-click failure) so the caller can fall back to the ticket."""
        switched = self._ensure_tab(symbol)
        if not switched.ok:
            log.info("fast market: tab switch failed (%s) - ticket fallback", switched.error)
            return None
        set_qty = self.panel.set_qty(qty)
        if not set_qty.ok:
            log.info("fast market: set qty failed (%s) - ticket fallback", set_qty.error)
            return None
        fire = self.panel.buy_market if side == "buy" else self.panel.sell_market
        fired = fire(expect_symbol=symbol, dry_run=False, expect_qty=qty)
        log.info("panel market %s %s: ok=%s confirm=%r err=%s", side, symbol, fired.ok,
                 (fired.data or {}).get("confirmation"), fired.error)
        if fired.ok:
            self._mark_live()
        if not fired.ok:
            if self._refused_before_click({"outcome": "failed", "detail": fired.error or ""}):
                log.info("fast market: refused before click (%s) - ticket fallback", fired.error)
                return None
            # It may have clicked - do NOT fall back (duplicate risk).
            return {"outcome": "failed", "order_ref": None,
                    "detail": f"panel Mkt: {fired.error} - verify manually"}
        if self._await_net(symbol, expected):
            return {"outcome": "filled", "order_ref": None,
                    "detail": f"net {before} -> {expected} (panel Mkt)"}
        # Fired but the net didn't reach the target within the window. Fall back to the
        # ticket ONLY if nothing landed (net unchanged AND no new working order for the
        # symbol) - then a market that plainly didn't place is safe to re-send. If
        # something DID land (net moved, or an order is resting), never re-send.
        # THOROUGH read is mandatory here: a visible-only miss of an off-screen filled
        # row would green-light the ticket fallback = a DOUBLE market order.
        after = self._net_thorough(symbol)
        if after == before and not self._find_working_ref(symbol):
            log.info("fast market: fired but nothing landed (net still %s, no working "
                     "order) - ticket fallback", after)
            return None
        return {"outcome": "failed", "order_ref": None,
                "detail": f"panel Mkt: net {before} -> {after} (expected {expected}) or an "
                          f"order is resting - verify manually (not re-sent)"}

    def _market_via_ticket(self, symbol, side, qty, before, expected) -> dict:
        # Sized attempt first (the ticket retries its own single-shot flakiness).
        result = self.ticket.place(symbol, side, qty, "MARKET", dry_run=False, max_attempts=3)
        log.info("ticket market %s %s x%s: ok=%s submitted=%s attempts=%s err=%s",
                 side, symbol, qty, result.ok, result.meta.get("submitted"),
                 result.meta.get("attempts"), result.error)
        if result.ok or result.meta.get("submitted"):
            self._mark_live()
        if not result.ok:
            submitted = self._submitted_note(result)
            if submitted:  # sent but its own verify failed - never retry (duplicate risk)
                return {"outcome": "failed", "order_ref": None, "detail": submitted}
            # Unit-lot fallback: the sized order failed cleanly (e.g. the qty-preset
            # flake) - buy/sell one lot at a time until the net reaches the target. N
            # unit fills == one fill of N, so this is correct and safe for a market entry.
            guard = qty + 3
            while self._net_thorough(symbol) != expected and guard > 0:
                guard -= 1
                self._checkpoint()   # stop point between unit-lot sends
                unit = self.ticket.place(symbol, side, 1, "MARKET", dry_run=False, max_attempts=2)
                if not unit.ok:
                    unit_submitted = self._submitted_note(unit)
                    if unit_submitted:
                        return {"outcome": "failed", "order_ref": None, "detail": unit_submitted}
                    return {"outcome": "failed", "order_ref": None,
                            "detail": f"market failed (sized and unit): {unit.error}"}
                time.sleep(1.5)

        # Sent (sized success or unit loop finished) - give the fill the full verify
        # window (returns the moment it registers; thorough reads see off-screen rows).
        if not self._await_net(symbol, expected):
            after = self._net_thorough(symbol)
            return {"outcome": "failed", "order_ref": None,
                    "detail": f"sent, but position check off: {before} -> {after} "
                              f"(expected {expected}) - verify manually"}
        tail = "" if result.ok else " (unit-lot fallback)"
        return {"outcome": "filled", "order_ref": None,
                "detail": f"net {before} -> {expected}{tail} (ticket)"}

    def _bid_ask(self, action: dict) -> dict:
        symbol, qty = action["symbol"], int(action["qty"])
        side = "buy" if action["kind"] == "place_bid" else "sell"
        before = self._net_thorough(symbol)   # baseline must see off-screen rows too
        expected = before + (qty if side == "buy" else -qty)
        switched = self._ensure_tab(symbol)
        if not switched.ok:
            return {"outcome": "failed", "order_ref": None,
                    "detail": f"symbol switch failed: {switched.error}"}
        set_qty = self.panel.set_qty(qty)
        if not set_qty.ok:
            return {"outcome": "failed", "order_ref": None,
                    "detail": f"set qty failed: {set_qty.error}"}
        fire = self.panel.buy_bid if action["kind"] == "place_bid" else self.panel.sell_ask
        fired = fire(expect_symbol=symbol, dry_run=False, expect_qty=qty)
        if not fired.ok:
            return {"outcome": "failed", "order_ref": None, "detail": fired.error}
        self._mark_live()
        # Classify honestly: a joined bid/ask may rest OR fill instantly. The old code
        # scanned the (visible) Orders grid ONCE with no wait, so a resting order that
        # had not rendered yet was misreported as "filled on join". Now poll briefly
        # for EITHER a working order (-> executing) or the net moving (-> verified
        # fill); claim a fill ONLY when the position actually moved.
        order_ref = None
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            order_ref = self._find_working_ref(symbol)
            if order_ref:
                break
            if self._net(symbol) == expected:
                break
            self._checkpoint()
            time.sleep(0.4)
        after = self._net_thorough(symbol)   # the verdict must see off-screen rows
        log.info("bid/ask classify %s %s: net %s->%s (expected %s), working_ref=%s",
                 side, symbol, before, after, expected, order_ref)
        if order_ref:
            return {"outcome": "executing", "order_ref": order_ref,
                    "detail": "joined the book, order working"}
        if after == expected:
            return {"outcome": "filled", "order_ref": None,
                    "detail": f"filled on join (net verified {before} -> {after})"}
        # Sent, but neither a fill nor a visible working order yet - report honestly
        # instead of claiming a fill; monitoring/state will show what it became.
        return {"outcome": "executing", "order_ref": None,
                "detail": f"sent - fill/working order not visible yet (net {before} -> "
                          f"{after}, expected {expected}); verify on the terminal"}

    def _resting(self, action: dict) -> dict:
        order_type = "LIMIT" if action["kind"] == "place_limit" else "STOP"
        price = _trim_price(action["limit_price"] if order_type == "LIMIT"
                            else action["stop_price"])
        result = self.ticket.place(
            action["symbol"], action["side"], int(action["qty"]), order_type,
            price=price, flag=(action.get("tif") or "day").upper(), dry_run=False, max_attempts=3)
        if not result.ok:
            submitted = self._submitted_note(result)
            return {"outcome": "failed", "order_ref": None,
                    "detail": submitted or result.error}
        working = (result.data or {}).get("working_order") or {}
        return {"outcome": "executing", "order_ref": str(working.get("id") or "") or None,
                "detail": f"{order_type} working @ {price}"}

    def _stop_limit(self, action: dict) -> dict:
        limit = _trim_price(action["limit_price"])  # the 'Price' (cap) field
        stop = _trim_price(action["stop_price"])    # the 'Stop Price' (trigger) field
        result = self.ticket.place(
            action["symbol"], action["side"], int(action["qty"]), "STOP LIMIT",
            price=limit, stop_price=stop, flag=(action.get("tif") or "day").upper(),
            dry_run=False, max_attempts=3)
        if not result.ok:
            submitted = self._submitted_note(result)
            return {"outcome": "failed", "order_ref": None, "detail": submitted or result.error}
        working = (result.data or {}).get("working_order") or {}
        return {"outcome": "executing", "order_ref": str(working.get("id") or "") or None,
                "detail": f"STOP LIMIT stop {stop} / limit {limit} working"}

    def _flatten_symbol(self, symbol_root: str, attempts: int = 3) -> tuple[bool, str]:
        """Flatten one symbol with an opposite MARKET order via the OrderTicket
        (chart-panel-free, so it works even with the Order Ticket widget open).

        Safety-critical, so it re-reads the net each attempt and retries ONLY on a
        clean pre-submission failure (e.g. the ticket's occasional qty-not-committed
        flake). A submitted-but-unverified send is NEVER retried (duplicate risk);
        a successful send is trusted and only verified.
        """
        start = self._net_thorough(symbol_root)
        if start == 0:
            # Confirm with a second thorough read: a transient mid-render miss on a
            # false "already flat" here would skip a REAL exit.
            time.sleep(0.6)
            confirm = self._net_thorough(symbol_root)
            log.info("flatten %s: first read net=0, confirm read net=%s", symbol_root, confirm)
            if confirm == 0:
                return True, f"{symbol_root} already flat"
            start = confirm
        last_err = ""
        used_units = False

        # Phase 1: one correctly-sized opposite MARKET (retry only on clean failures).
        for attempt in range(1, attempts + 1):
            net = self._net_thorough(symbol_root)
            if net == 0:
                break
            side = "sell" if net > 0 else "buy"
            if attempt > 1:
                self._checkpoint()   # stop point between flatten attempts
                time.sleep(1.5)
            result = self.ticket.place(symbol_root, side, abs(net), "MARKET",
                                       dry_run=False, max_attempts=2)
            if result.ok:
                time.sleep(1.5)
                break
            submitted = self._submitted_note(result)
            if submitted:  # may have sent - do NOT retry
                return False, f"{symbol_root} flatten: {submitted}"
            last_err = result.error or "flatten failed"

        # Phase 2: unit-lot fallback. qty=1 is the most reliable preset, so if a sized
        # order won't commit (a known qty-preset flake), close one lot at a time until
        # flat. This bulletproofs the safety-critical path. Bounded to avoid a loop.
        guard = abs(start) + 3
        while self._net_thorough(symbol_root) != 0 and guard > 0:
            guard -= 1
            self._checkpoint()   # stop point between unit-lot flatten sends
            net = self._net_thorough(symbol_root)
            side = "sell" if net > 0 else "buy"
            unit = self.ticket.place(symbol_root, side, 1, "MARKET",
                                     dry_run=False, max_attempts=2)
            used_units = True
            if not unit.ok:
                submitted = self._submitted_note(unit)
                if submitted:
                    return False, f"{symbol_root} unit flatten: {submitted}"
                last_err = unit.error or last_err
            time.sleep(1.5)

        now = self._net_thorough(symbol_root)
        tail = " (unit-lot fallback)" if used_units else ""
        if now == 0:
            return True, f"{symbol_root} net {start} -> 0{tail}"
        return False, f"{symbol_root} still net {now} after flatten attempts: {last_err}"

    def _panel_exit(self, symbol_root: str) -> tuple[bool, str]:
        """Exit at Mkt & Cxl via the chart panel: flattens the symbol AND cancels its
        working orders in one reliable action (Tradovate's own combined control).
        The panel symbol now reads reliably (textContent fix), so this is the primary
        exit path - guaranteed '& Cxl', not the flaky right-click."""
        sw = self._ensure_tab(symbol_root)
        if not sw.ok:
            return False, f"tab switch failed: {sw.error}"
        time.sleep(0.4)
        res = self.panel.exit_at_mkt(expect_symbol=symbol_root, dry_run=False)
        if not res.ok:
            return False, res.error or "exit at mkt & cxl failed"
        self._mark_live()
        return True, "Exit at Mkt & Cxl"

    def _exit_symbol(self, action: dict) -> dict:
        symbol = action["symbol"].split()[0]
        ok, note = self._panel_exit(symbol)
        if not ok:
            # Fallback: guarantee FLAT via opposite-market (cancel may not happen).
            fok, fnote = self._flatten_symbol(symbol)
            if not fok:
                return {"outcome": "failed", "order_ref": None,
                        "detail": f"exit failed ({note}); flatten fallback: {fnote}"}
            left = sum(1 for o in self._working_fast() if symbol in (o.get("contract") or ""))
            detail = f"flattened via fallback ({fnote})"
            if left:
                detail += f"; {left} working order(s) still resting - cancel manually"
            return {"outcome": "filled", "order_ref": None, "detail": detail}
        # The Exit&Cxl CLICK succeeded - now VERIFY flatness before claiming it. The old
        # code reported "flat" without re-reading the position, which could show an exit
        # as done while the position remained (the misreported-exit bug).
        flat = self._await_net(symbol, 0)
        if not flat:
            residual = self._net_thorough(symbol)
            log.warning("exit %s: net still %s after Exit&Cxl - flatten fallback", symbol, residual)
            fok, fnote = self._flatten_symbol(symbol)
            if not fok:
                return {"outcome": "failed", "order_ref": None,
                        "detail": f"Exit&Cxl clicked but {symbol} NOT flat (net {residual}); "
                                  f"fallback: {fnote} - VERIFY on the terminal"}
            return {"outcome": "filled", "order_ref": None,
                    "detail": f"Exit at Mkt & Cxl + flatten fallback ({fnote})"}
        left = sum(1 for o in self._working_fast() if symbol in (o.get("contract") or ""))
        detail = f"Exit at Mkt & Cxl - {symbol} flat (net verified)"
        if left:
            detail += f"; {left} order(s) still resting"
        return {"outcome": "filled", "order_ref": None, "detail": detail}

    def _flatten_all(self, action: dict) -> dict:
        # Every symbol that has a position OR a working order.
        symbols = []
        report = self.positions.report()
        if report.ok:
            symbols += [str(p.get("symbol") or "").split()[0]
                        for p in (report.data or {}).get("positions", []) if p.get("net_pos")]
        for row in self._working_fast():
            root = str(row.get("contract") or "").split()[0]
            if root:
                symbols.append(root)
        targets = [s for s in dict.fromkeys(symbols) if s]
        if not targets:
            return {"outcome": "filled", "order_ref": None, "detail": "already flat, no working orders"}
        failures = []
        for symbol in targets:
            ok, note = self._panel_exit(symbol)
            if ok and not self._await_net(symbol, 0):
                # clicked, but the position did not go flat - treat like a panel failure
                ok, note = False, f"Exit&Cxl clicked but net still {self._net_thorough(symbol)}"
            if not ok:
                fok, _fnote = self._flatten_symbol(symbol)   # fallback: at least get flat
                if not fok:
                    failures.append(f"{symbol}: {note}")
        if failures:
            return {"outcome": "failed", "order_ref": None,
                    "detail": f"exited {len(targets) - len(failures)}/{len(targets)}; "
                              + "; ".join(failures)[:280]}
        time.sleep(1.0)
        remaining = len(self._working_fast())
        detail = f"Exit at Mkt & Cxl on {len(targets)} symbol(s)"
        if remaining:
            detail += f"; {remaining} order(s) still resting"
        return {"outcome": "filled", "order_ref": None, "detail": detail}

    @staticmethod
    def _refused_before_click(result: dict) -> bool:
        if result.get("outcome") != "failed":
            return False
        detail = result.get("detail") or ""
        return any(marker in detail for marker in _PRECLICK_MARKERS)

    def _working_fast(self) -> list[dict]:
        """Working orders from the VISIBLE grid only (scroll=False).

        The scrolled full-history read is the bot's known slow path (O(grid size));
        working orders sit in the recent rows, so the visible read is the hot path
        for verification and monitoring.
        """
        report = self.orders.report(scroll=False)
        if not report.ok:
            return []
        return (report.data or {}).get("working", [])

    def _find_working_ref(self, symbol: str) -> Optional[str]:
        try:
            for row in self._working_fast():
                if symbol in (row.get("contract") or "") or symbol in (row.get("row_text") or ""):
                    return str(row.get("id") or "") or None
        except Exception:  # noqa: BLE001
            return None
        return None

    # --- monitoring / keep-alive --------------------------------------------------------

    def read_state(self) -> dict:
        """Light snapshot of the ACTIVE account: positions + working orders.

        Deliberately avoids the slow full-orders scroll read and avoids switching
        accounts just to monitor - execution owns account switching.
        """
        state: dict[str, Any] = {"account": None, "positions": [], "working_orders": []}
        state["account"] = self.active_account()
        report = self.positions.report(scroll=False)  # monitor read: never drag (hot path)
        if report.ok:
            state["positions"] = [
                {"symbol": p.get("symbol"), "net_pos": p.get("net_pos"),
                 "net_price": p.get("net_price"), "open_pl": p.get("open_pl")}
                for p in (report.data or {}).get("positions", [])
            ]
        state["working_orders"] = [
            {"id": row.get("id"), "contract": row.get("contract"),
             "action": row.get("action"), "order_type": row.get("order_type"),
             "price": row.get("price"), "status": row.get("status")}
            for row in self._working_fast()
        ]
        return state

    def read_accounts_summary(self) -> list[dict]:
        """Read the 'Accounts' widget - EVERY account's Open P/L, Total P/L, Net Liq (and
        more) in one shot, no account switching. Pure DOM read (scroll=False: no drag, no
        clicks) and no network call - the numbers are already streamed into the browser -
        so it never disturbs the terminal and cannot hit Tradovate rate limits.

        Requires the 'Accounts' widget to be the ACTIVE tab in its stack (golden-layout
        unmounts inactive tabs); returns [] otherwise, without disturbing anything.
        """
        try:
            table = tables.read_table(self.driver, "Accounts", scroll=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("accounts widget read failed: %s", exc)
            return []
        if table.get("error"):
            return []
        rows = []
        for rec in table.get("records", []):
            acct = (rec.get("Account") or rec.get("col0") or "").strip()
            if not acct or acct.lower() == "account":
                continue
            rows.append({
                "account": acct,
                "open_pl": rec.get("Dollar Open P L"),
                "total_pl": rec.get("Dollar Total P L"),
                "net_liq": rec.get("Net Liq"),
                "columns": rec,   # full row, so the Sender can show every column as-is
            })
        return rows

    def keepalive(self) -> bool:
        """Harmless nudge (mouse-move + zero scroll) - self-throttled by the guard."""
        try:
            return bool(self.guard.keepalive())
        except Exception:  # noqa: BLE001
            return False

    def refresh_tab(self, settle_timeout: float = 25.0) -> tuple[bool, str]:
        """Reload the Tradovate tab to clear UI wedging (a stuck Account widget that
        won't open/switch until refreshed). The caller (executor) runs this as a
        serialized step, so no order executes during it. Blocks until the SPA re-renders
        and the login marker returns (bounded) so the tab is usable before actions
        resume. The panel/ticket/accounts helpers query the DOM fresh each call, so no
        re-setup is needed after the reload."""
        try:
            if not self.session.focus_tab(TRADOVATE_NEEDLE):
                return False, "no Tradovate tab to refresh"
            self.driver.refresh()
        except Exception as exc:  # noqa: BLE001
            self.connected = False
            return False, f"refresh failed: {type(exc).__name__}: {exc}"

        deadline = time.monotonic() + settle_timeout
        last = "still loading"
        while time.monotonic() < deadline:
            time.sleep(1.5)
            try:
                status = self.guard.check_status(force=True)
            except Exception as exc:  # noqa: BLE001 - SPA mid-render
                last = type(exc).__name__
                continue
            if status.logged_in:
                ready = self._wait_workspace(10.0)
                return True, "reloaded, logged in" + ("" if ready else " (workspace settling)")
            last = status.detail or "not logged in yet"
        # The reload happened; the per-action login check gates anything that follows.
        return True, f"reloaded (session not confirmed: {last})"

    def _workspace_ready(self) -> bool:
        """True once the trading widgets have re-rendered (not merely logged in): the
        Order Ticket tab AND the chart trade panel are present. Waiting for this after a
        reload/restart stops a market entry from resuming on a half-rendered workspace
        (the failure that hit market orders while simpler exits/bids survived)."""
        try:
            return bool(self.driver.execute_script(r"""
                var hasTicket=false;
                document.querySelectorAll('.lm_tab .lm_title').forEach(function(t){
                  if((t.textContent||'').indexOf('Order Ticket')!==-1) hasTicket=true;});
                var hasPanel = !!document.querySelector('.contract-symbol');
                return hasTicket && hasPanel;
            """))
        except Exception:  # noqa: BLE001
            return False

    def _wait_workspace(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._workspace_ready():
                return True
            time.sleep(0.5)
        return self._workspace_ready()

    def recycle_driver(self) -> tuple[bool, str]:
        """Drop the Selenium driver and re-attach a fresh one to the SAME running browser.
        quit() ends the old chromedriver session (releasing its CDP overhead); the browser
        - a separate process on the persistent profile - keeps running and logged in
        (verified live). Clears DRIVER/CDP-side buildup with no reload and no re-login.
        Serialized by the caller (runs only between actions)."""
        old = self.session
        self.connected = False
        self.driver = None
        self.session = None
        if old is not None and getattr(old, "driver", None) is not None:
            try:
                old.driver.quit()
            except Exception:  # noqa: BLE001 - releasing the old session must never raise
                pass
        ok, detail = self.connect()
        if not ok:
            return False, f"re-attach failed: {detail}"
        return True, f"re-attached fresh driver ({detail})"

    def _port_open(self) -> bool:
        import socket
        host, port = "127.0.0.1", 9250
        try:
            settings = BotSettings.load()
            host, port = settings.browser.host, settings.browser.port
        except Exception:  # noqa: BLE001
            pass
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        try:
            return sock.connect_ex((host, port)) == 0
        finally:
            sock.close()

    def restart_browser(self, settle_timeout: float = 30.0) -> tuple[bool, str]:
        """Full recycle: close the browser process and relaunch it (persistent profile
        keeps the login), then re-attach. Heaviest maintenance - clears BROWSER-side
        buildup (memory/DOM). On demand via the dashboard, or if browser_restart_sec is
        enabled. Serialized by the caller."""
        from app.launcher import launch_terminal

        # 1) close the whole browser gracefully via CDP (if a driver is attached)
        try:
            if self.driver is not None:
                self.driver.execute_cdp_cmd("Browser.close", {})
        except Exception as exc:  # noqa: BLE001
            log.info("Browser.close via CDP failed (%s); relaunch proceeds anyway", exc)
        old = self.session
        self.connected = False
        self.driver = None
        self.session = None
        if old is not None and getattr(old, "driver", None) is not None:
            try:
                old.driver.quit()
            except Exception:  # noqa: BLE001
                pass

        # 2) wait for the debug port to drop, so launch_terminal starts a FRESH process.
        #    If the graceful close didn't drop it, FORCE-KILL the process on the port.
        deadline = time.monotonic() + 12.0
        while self._port_open() and time.monotonic() < deadline:
            time.sleep(0.5)
        forced = ""
        if self._port_open():
            from app.launcher import force_kill_browser
            forced = f" (force-kill: {force_kill_browser()})"
            deadline = time.monotonic() + 10.0
            while self._port_open() and time.monotonic() < deadline:
                time.sleep(0.5)

        # 3) relaunch (persistent profile keeps the login) + ensure one Tradovate tab
        ok, detail = launch_terminal()
        if not ok:
            return False, f"relaunch failed{forced}: {detail}"

        # 4) re-attach + wait for the session AND the trading workspace to come back
        deadline = time.monotonic() + settle_timeout
        last = "attaching"
        while time.monotonic() < deadline:
            cok, cdetail = self.connect()
            if cok:
                try:
                    li, ld = self.login_check()
                except Exception as exc:  # noqa: BLE001
                    li, ld = False, str(exc)
                if li:
                    ready = self._wait_workspace(12.0)
                    tail = "" if ready else " (workspace settling)"
                    return True, f"browser restarted, re-attached, logged in{forced}{tail}"
                last = f"attached but {ld}"
            else:
                last = cdetail
            time.sleep(1.5)
        return True, f"browser relaunched{forced} ({detail}); session not confirmed: {last}"
