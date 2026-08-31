"""TradovateGateway - the API-mode implementation of the gateway seam.

Order execution mirrors the WEB automation kind-for-kind, but over the Tradovate REST API
with the SAME correctness discipline: verify-before-you-claim, never claim a fill the
position didn't move to, and NEVER retry an ambiguous send (that could double an order).
The web mode reads the DOM to verify; this mode reads /position and /order to verify.

No Selenium, no browser is imported here.

Verification model (REST, not WebSocket yet): a placeorder returns synchronously with an
orderId or a failureReason. failureReason != Success -> a VISIBLE rejection (the thing web
mode could never see). Then we poll the broker's own state - net position for market/exit,
order status for resting orders - exactly like web mode polled the Positions widget. A
full WebSocket fill feed (catching async RiskRejected) is a later hardening; REST polling
is the same "confirm against the broker's state" contract the executor already trusts.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from app.gateways.api import credentials, oauth, token_store
from app.gateways.api.client import AmbiguousSend, RestClient, TradovateAPIError

log = logging.getLogger("copier.gateways.api")

_NOT_CONNECTED = ("not connected - open /oauth/tradovate on this copier's dashboard and "
                  "click Connect Tradovate")

# order_kind -> Tradovate orderType / action
_ACTION = {"buy": "Buy", "sell": "Sell"}
_TIF = {"day": "Day", "gtc": "GTC", "gtd": "GTD", "ioc": "IOC", "fok": "FOK"}
# ordStatus enum (verified vs docs): a fill is Filled OR Completed; these end the poll.
_FILLED_ORD = {"Filled", "Completed"}
_TERMINAL_ORD = {"Filled", "Completed", "Rejected", "Canceled", "Cancelled", "Expired"}


class TradovateGateway:
    def __init__(self, settings: Any, session_factory: Optional[Callable] = None,
                 rest_factory: Optional[Callable[[str], Any]] = None):
        self.settings = settings
        self.session_factory = session_factory
        # rest_factory(token) -> a RestClient-like object; injectable for tests.
        self._rest_factory = rest_factory or self._default_rest_factory
        self.abort_check: Optional[Callable[[], Optional[str]]] = None
        self._auth_mode = str(getattr(settings, "tradovate_auth", "oauth") or "oauth").lower()
        self._live_at: Optional[float] = None
        self._acct_cache: dict[str, int] = {}       # account name -> accountId
        self._contract_cache: dict[str, int] = {}   # symbol -> contractId
        # verify windows (mirror web's net_verify_sec; own knobs are fine defaults)
        self._net_verify_sec = float(getattr(settings, "net_verify_sec", 12.0) or 12.0)
        self._resting_verify_sec = 4.0
        log.info("TradovateGateway ready (auth=%s, base=%s)",
                 getattr(settings, "tradovate_auth", "oauth"),
                 getattr(settings, "tradovate_base", "?"))

    def _default_rest_factory(self, token: str):
        return RestClient(self.settings.tradovate_base, token,
                          timeout=float(getattr(self.settings, "http_timeout_sec", 10.0)))

    # --- token / session ------------------------------------------------------------
    def _db(self):
        return self.session_factory() if self.session_factory else None

    def _current_access_token(self) -> tuple[Optional[str], str]:
        db = self._db()
        if db is None:
            return None, "no local session (token store unavailable)"
        try:
            rec = token_store.load_token(db)
            if rec is None:
                # credentials mode is HEADLESS: mint now. oauth mode needs the user flow.
                if self._auth_mode == "credentials":
                    try:
                        rec = token_store.save_token(db, credentials.access_token_request(self.settings))
                        return rec["access_token"], "connected"
                    except credentials.CredError as exc:
                        return None, f"credential auth failed: {exc}"
                return None, _NOT_CONNECTED
            if token_store.refresh_expired(rec):     # only oauth carries a refresh expiry
                return None, "the Tradovate connection expired - reconnect via /oauth/tradovate"
            if token_store.access_expired(rec):
                rec, err = self._refresh(db, rec)
                if err:
                    return None, err
            return rec["access_token"], "connected"
        finally:
            db.close()

    def _refresh(self, db, rec):
        """Extend the session before expiry. credentials -> renewaccesstoken (fallback: re-
        mint); oauth -> refresh_token. Returns (rec, error)."""
        if self._auth_mode == "credentials":
            try:
                fresh = credentials.renew_access_token(self.settings, rec["access_token"])
                return token_store.save_token(db, fresh), None
            except credentials.CredError:
                try:   # renew failed - mint fresh (headless, safe)
                    return token_store.save_token(db, credentials.access_token_request(self.settings)), None
                except credentials.CredError as exc:
                    return None, f"credential renew/mint failed: {exc}"
        refresh_tok = rec.get("refresh_token")
        if not refresh_tok:
            return None, "access token expired and no refresh token - reconnect via /oauth/tradovate"
        try:
            return token_store.save_token(db, oauth.refresh_token(self.settings, refresh_tok)), None
        except oauth.OAuthError as exc:
            return None, f"token refresh failed: {exc} - reconnect via /oauth/tradovate"

    def ensure_connected(self) -> tuple[bool, str]:
        tok, detail = self._current_access_token()
        return (tok is not None), detail

    def login_check(self) -> tuple[bool, str]:
        tok, detail = self._current_access_token()
        return (tok is not None), detail

    def active_account(self) -> Optional[str]:
        return None

    def ensure_account(self, account_ref: str) -> tuple[bool, str]:
        # No switching: accountId travels on every order. Nothing to do.
        return True, f"api mode: account {account_ref} addressed per-order"

    # --- id resolution (cached) -----------------------------------------------------
    def _account_id(self, rest, name: str) -> Optional[int]:
        if name in self._acct_cache:
            return self._acct_cache[name]
        for a in rest.account_list():
            if a.get("name"):
                self._acct_cache[a["name"]] = a.get("id")
        return self._acct_cache.get(name)

    def _contract_id(self, rest, symbol: str) -> Optional[int]:
        if symbol in self._contract_cache:
            return self._contract_cache[symbol]
        found = rest.contract_find(symbol)
        cid = (found or {}).get("id")
        if cid is not None:
            self._contract_cache[symbol] = cid
        return cid

    # --- verification reads ---------------------------------------------------------
    def _net(self, rest, account_id: int, contract_id: int) -> int:
        net = 0
        for p in rest.position_list():
            if p.get("accountId") == account_id and p.get("contractId") == contract_id:
                net += int(p.get("netPos") or 0)
        return net

    def _await_net(self, rest, account_id, contract_id, expected, timeout) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if self._net(rest, account_id, contract_id) == expected:
                return True
            if time.monotonic() >= deadline:
                return self._net(rest, account_id, contract_id) == expected
            if self._aborting():
                return False
            time.sleep(0.4)

    def _working_orders(self, rest, account_id, contract_id) -> list:
        out = []
        for o in rest.order_list():
            if (o.get("accountId") == account_id and o.get("contractId") == contract_id
                    and o.get("ordStatus") == "Working"):
                out.append(o.get("id"))
        return out

    def _aborting(self) -> Optional[str]:
        try:
            return self.abort_check() if self.abort_check else None
        except Exception:  # noqa: BLE001
            return None

    # --- placement primitive (the 3-part acceptance test) ---------------------------
    def _place(self, rest, account_name, account_id, symbol, action, order_type, qty,
               price=None, stop_price=None, tif="day"):
        """Returns (order_id, error_detail). error_detail set => not accepted / rejected /
        ambiguous. A rejection is a 200 with failureReason - now VISIBLE."""
        payload = {
            "accountSpec": account_name,
            "accountId": account_id,
            "action": action,
            "symbol": symbol,
            "orderQty": int(qty),
            "orderType": order_type,
            "isAutomated": True,
        }
        if price is not None:
            payload["price"] = float(price)
        if stop_price is not None:
            payload["stopPrice"] = float(stop_price)
        payload["timeInForce"] = _TIF.get(str(tif).lower(), "Day")
        try:
            resp = rest.place_order(payload)
        except AmbiguousSend as exc:
            return None, (f"AMBIGUOUS: {exc} - an order MAY have been placed; VERIFY on the "
                          f"terminal (not retried)")
        except TradovateAPIError as exc:
            return None, f"not sent: {exc}"
        fr = resp.get("failureReason")
        if fr and fr != "Success":
            return None, f"rejected by broker: {fr} - {resp.get('failureText', '')}".strip(" -")
        order_id = resp.get("orderId")
        if not order_id:
            return None, f"no orderId returned (failureReason={fr!r}) - treat as not placed"
        self._live_at = time.monotonic()   # accepted by the broker
        return order_id, None

    # --- the order path -------------------------------------------------------------
    def execute(self, action: dict) -> dict:
        self._live_at = None
        t0 = time.monotonic()
        try:
            tok, detail = self._current_access_token()
            if tok is None:
                return {"outcome": "failed", "order_ref": None, "detail": detail}
            rest = self._rest_factory(tok)

            name = action.get("account_ref")
            account_id = self._account_id(rest, name)
            if account_id is None:
                return {"outcome": "failed", "order_ref": None,
                        "detail": f"account {name} not visible to this token"}

            kind = action["kind"]
            handler = {
                "place_market": self._market,
                "place_bid": self._bid_ask,
                "place_ask": self._bid_ask,
                "place_limit": self._resting,
                "place_stop": self._resting,
                "place_stop_limit": self._resting,
                "exit_symbol": self._exit_symbol,
                "flatten_all": self._flatten_all,
            }.get(kind)
            if handler is None:
                return {"outcome": "failed", "order_ref": None,
                        "detail": f"unknown action kind '{kind}'"}
            result = handler(rest, name, account_id, action)
            return self._annotate(result, t0)
        except TradovateAPIError as exc:
            return {"outcome": "failed", "order_ref": None, "detail": f"api error: {exc}"}
        except Exception as exc:  # noqa: BLE001 - never crash the executor loop
            return {"outcome": "failed", "order_ref": None,
                    "detail": f"{type(exc).__name__}: {exc}"}

    def _annotate(self, result: dict, t0: float) -> dict:
        try:
            if result.get("outcome") in ("filled", "executing") and self._live_at is not None:
                result["detail"] = (result.get("detail") or "") + (
                    f" [live +{self._live_at - t0:.1f}s, verified +{time.monotonic() - t0:.1f}s]")
        except Exception:  # noqa: BLE001
            pass
        return result

    def _market(self, rest, name, account_id, action):
        symbol, side, qty = action["symbol"], action["side"], int(action["qty"])
        cid = self._contract_id(rest, symbol)
        if cid is None:
            return {"outcome": "failed", "order_ref": None, "detail": f"unknown contract {symbol}"}
        before = self._net(rest, account_id, cid)
        expected = before + (qty if side == "buy" else -qty)
        order_id, err = self._place(rest, name, account_id, symbol, _ACTION[side],
                                    "Market", qty, tif=action.get("tif", "day"))
        if err:
            return {"outcome": "failed", "order_ref": order_id, "detail": err}
        if self._await_net(rest, account_id, cid, expected, self._net_verify_sec):
            return {"outcome": "filled", "order_ref": str(order_id),
                    "detail": f"net {before} -> {expected} (api market)"}
        # sent + accepted but net didn't move: report honestly, never claim a fill
        return {"outcome": "executing", "order_ref": str(order_id),
                "detail": f"market accepted (order {order_id}) but net not verified "
                          f"({before}->{self._net(rest, account_id, cid)}, expected {expected}) "
                          f"- verify on the terminal"}

    def _resting(self, rest, name, account_id, action):
        symbol, side, qty = action["symbol"], action["side"], int(action["qty"])
        kind = action["kind"]
        order_type = {"place_limit": "Limit", "place_stop": "Stop",
                      "place_stop_limit": "StopLimit"}[kind]
        order_id, err = self._place(
            rest, name, account_id, symbol, _ACTION[side], order_type, qty,
            price=action.get("limit_price"), stop_price=action.get("stop_price"),
            tif=action.get("tif", "day"))
        if err:
            return {"outcome": "failed", "order_ref": order_id, "detail": err}
        return self._classify_resting(rest, account_id, order_id, symbol)

    def _bid_ask(self, rest, name, account_id, action):
        # A bid/ask means "rest at the touch". The REST API has no touch primitive and we
        # have no market-data entitlement to fetch it. If the signal carries a limit_price,
        # honour it as a Limit; otherwise FAIL LOUDLY (never silently place a market).
        if action.get("limit_price") is None:
            return {"outcome": "failed", "order_ref": None,
                    "detail": "bid/ask has no REST primitive without market data - the Sender "
                              "must send a limit_price (or use market) for API mode"}
        side = action["side"]
        order_id, err = self._place(
            rest, name, account_id, action["symbol"], _ACTION[side], "Limit",
            int(action["qty"]), price=action.get("limit_price"), tif=action.get("tif", "day"))
        if err:
            return {"outcome": "failed", "order_ref": order_id, "detail": err}
        return self._classify_resting(rest, account_id, order_id, action["symbol"])

    def _classify_resting(self, rest, account_id, order_id, symbol):
        """Poll the order until it's Working (-> executing) or Filled (-> filled) or a
        terminal reject. Mirrors web mode's join-the-book classification."""
        deadline = time.monotonic() + self._resting_verify_sec
        status = None
        while time.monotonic() < deadline:
            item = rest.order_item(order_id) or {}
            status = item.get("ordStatus")
            if status == "Working" or status in _TERMINAL_ORD:
                break
            if self._aborting():
                break
            time.sleep(0.3)
        if status in _FILLED_ORD:
            return {"outcome": "filled", "order_ref": str(order_id),
                    "detail": f"filled on placement (order {order_id})"}
        if status == "Working":
            return {"outcome": "executing", "order_ref": str(order_id),
                    "detail": f"joined the book, order {order_id} working"}
        if status in ("Rejected", "Canceled", "Cancelled", "Expired"):
            return {"outcome": "failed", "order_ref": str(order_id),
                    "detail": f"order {order_id} {status}"}
        return {"outcome": "executing", "order_ref": str(order_id),
                "detail": f"order {order_id} sent - status not confirmed yet; verify"}

    def _exit_symbol(self, rest, name, account_id, action):
        symbol = action["symbol"]
        cid = self._contract_id(rest, symbol)
        if cid is None:
            return {"outcome": "failed", "order_ref": None, "detail": f"unknown contract {symbol}"}
        # Cancel any working orders for this symbol (the '& Cxl' half of Exit-at-Mkt&Cxl).
        for oid in self._working_orders(rest, account_id, cid):
            if self._aborting():
                return {"outcome": "failed", "order_ref": None, "detail": "ABORTED before flatten"}
            try:
                rest.cancel_order(oid)
            except TradovateAPIError:
                pass
        net = self._net(rest, account_id, cid)
        if net == 0:
            return {"outcome": "filled", "order_ref": None,
                    "detail": f"{symbol} already flat (net verified)"}
        side = "sell" if net > 0 else "buy"
        order_id, err = self._place(rest, name, account_id, symbol, _ACTION[side],
                                    "Market", abs(net), tif="day")
        if err:
            return {"outcome": "failed", "order_ref": order_id,
                    "detail": f"exit {symbol}: {err}"}
        if self._await_net(rest, account_id, cid, 0, self._net_verify_sec):
            return {"outcome": "filled", "order_ref": None,
                    "detail": f"Exit at Mkt & Cxl - {symbol} flat (net verified)"}
        return {"outcome": "failed", "order_ref": str(order_id),
                "detail": f"exit {symbol} sent but net still "
                          f"{self._net(rest, account_id, cid)} - VERIFY / flatten manually"}

    def _flatten_all(self, rest, name, account_id, action):
        # Flatten every open position on this account.
        rows = [p for p in rest.position_list()
                if p.get("accountId") == account_id and int(p.get("netPos") or 0) != 0]
        if not rows:
            return {"outcome": "filled", "order_ref": None, "detail": "already flat (net verified)"}
        failures = []
        for p in rows:
            if self._aborting():
                return {"outcome": "failed", "order_ref": None,
                        "detail": f"ABORTED mid-flatten ({len(failures)} symbol(s) failed)"}
            net = int(p["netPos"])
            side = "sell" if net > 0 else "buy"
            # placeorder can take symbol; but flatten_all reads positions by contractId, so
            # resolve the contract name if the position row carries it, else use contractId.
            sym = p.get("name") or p.get("contractId")
            oid, err = self._place(rest, name, account_id, sym, _ACTION[side], "Market",
                                   abs(net), tif="day")
            if err or not self._await_net(rest, account_id, p.get("contractId"), 0,
                                          self._net_verify_sec):
                failures.append(str(sym))
        if failures:
            return {"outcome": "failed", "order_ref": None,
                    "detail": f"flatten-all: {len(failures)} not verified flat: "
                              f"{', '.join(failures)} - VERIFY manually"}
        return {"outcome": "filled", "order_ref": None,
                "detail": f"flatten-all: {len(rows)} symbol(s) flat (net verified)"}

    # --- monitoring / maintenance ---------------------------------------------------
    def read_state(self) -> dict:
        return {"account": None, "positions": [], "working_orders": []}

    def read_accounts_summary(self) -> list[dict]:
        return []

    def keepalive(self) -> bool:
        return True

    def refresh_tab(self, settle_timeout: float = 25.0) -> tuple[bool, str]:
        return True, "api mode: no tab to refresh"

    def recycle_driver(self) -> tuple[bool, str]:
        return True, "api mode: no driver to recycle"

    def restart_browser(self, settle_timeout: float = 30.0) -> tuple[bool, str]:
        return True, "api mode: no browser to restart"
