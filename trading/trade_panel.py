"""Chart trade panel: the buttons above the chart.

Buy Mkt / Sell Mkt / Buy Bid / Sell Ask / qty / Exit at Mkt & Cxl.

These act on the CHART STACK'S current symbol immediately - there is no
symbol field here, so the guarantee that we trade the right instrument comes
from re-reading the panel's own contract-symbol header right before the click
(defence in depth: the workspace switch put us on the symbol; this confirms it
at the trigger). Every order method takes expect_symbol and refuses to fire if
the panel shows anything else.

Building block only: composites (composite.py) chain switch+qty+click+verify.
"""

from __future__ import annotations

from typing import Optional

from browser import actions
from config import locators
from core.results import ActionResult
from trading.base import TerminalModule


class ChartTradePanel(TerminalModule):

    # -- scope ---------------------------------------------------------------
    def _scope(self):
        scope = actions.find(self.driver, locators.TRADE_PANEL_SCOPE, timeout=5)
        if scope is None:
            raise RuntimeError("chart trade panel not found (no 'Buy Bid' anchor)")
        return scope

    def _panel_symbol(self, scope) -> Optional[str]:
        # Read via JS textContent, NOT Selenium .text: when the Order Ticket widget
        # overlaps/reflows the panel, .text (visible text) comes back empty even though
        # the contract-symbol is present and correct - which made every chart-panel
        # action (Exit at Mkt & Cxl, Buy/Sell Mkt, Bid/Ask) refuse to click. textContent
        # is reliable regardless of overlap/visibility.
        try:
            raw = self.driver.execute_script(
                "var e=arguments[0].querySelector('.contract-symbol');"
                "return e ? (e.textContent || '') : null;", scope)
        except Exception:  # noqa: BLE001
            return None
        if not raw:
            return None
        return " ".join(raw.split()).upper() or None

    # -- read ----------------------------------------------------------------
    def read_state(self) -> ActionResult:
        result = ActionResult(action="trade_panel.read")
        try:
            self._precheck(result)
            result.enter("read")
            scope = self._scope()
            qty_el = actions.find(scope, locators.TRADE_QTY_INPUT, timeout=3)
            qty = qty_el.get_attribute("value") if qty_el else None
            state = {
                "symbol": self._panel_symbol(scope),
                "qty": qty,
                "buttons": {
                    "buy_mkt": actions.find(scope, locators.TRADE_BUY_MKT, timeout=1) is not None,
                    "sell_mkt": actions.find(scope, locators.TRADE_SELL_MKT, timeout=1) is not None,
                    "buy_bid": actions.find(scope, locators.TRADE_BUY_BID, timeout=1) is not None,
                    "sell_ask": actions.find(scope, locators.TRADE_SELL_ASK, timeout=1) is not None,
                    "exit": actions.find(scope, locators.TRADE_EXIT_BUTTON, timeout=1) is not None,
                },
            }
            return self._finish(result.succeed(state))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)

    # -- qty -----------------------------------------------------------------
    def set_qty(self, qty: int) -> ActionResult:
        result = ActionResult(action="trade_panel.set_qty")
        result.meta["qty"] = qty
        try:
            if int(qty) < 1:
                raise ValueError(f"qty must be >= 1, got {qty}")
            self._precheck(result)
            scope = self._scope()

            result.enter("locate_qty")
            qty_el = actions.require(scope, locators.TRADE_QTY_INPUT,
                                     "trade panel qty input", timeout=4)
            # SHORT-CIRCUIT (2026-08-25): the panel qty is sticky; when it already shows
            # the target there is nothing to type or verify - skip the type+tick+verify
            # (~1s per action, and qty is almost always unchanged between signals).
            current = (qty_el.get_attribute("value") or "").strip()
            if current == str(int(qty)):
                result.meta["qty_shortcircuit"] = True
                return self._finish(result.succeed({"qty": int(qty)}))
            self.tick()

            result.enter("type_qty")
            if not actions.type_into(self.driver, qty_el, str(int(qty))):
                raise RuntimeError("could not type quantity")
            self.tick()

            result.enter("verify_qty")
            shown = (qty_el.get_attribute("value") or "").strip()
            if shown != str(int(qty)):
                raise RuntimeError(f"qty read-back mismatch: wanted {qty}, shows {shown!r}")
            return self._finish(result.succeed({"qty": int(qty)}))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)

    # -- orders --------------------------------------------------------------
    def _confirm_side(self, label: str) -> Optional[str]:
        """The word the confirmation button carries for a given trade button."""
        low = label.lower()
        if low.startswith("buy"):
            return "Buy"
        if low.startswith("sell"):
            return "Sell"
        return None   # Exit and others: click the non-Cancel button

    # confirmation dialogs come in two shapes:
    #   market Buy/Sell -> title 'Buy 1 MKT?', buttons [Buy|Sell] + Cancel
    #   Exit at Mkt     -> Yes / No
    _NEGATIVE = {"cancel", "no"}
    _AFFIRMATIVE_EXTRA = ("Yes", "Confirm", "OK", "Submit")

    def _popover_buttons(self, popover):
        """Every clickable in the popover as (element, text)."""
        out = []
        for el in actions.find_all(
                popover,
                ["xpath=.//div[contains(@class,'btn')]",
                 "xpath=.//button", "xpath=.//label[contains(@class,'btn')]",
                 "xpath=.//*[@role='button']"]):
            try:
                text = " ".join((el.text or "").split())
            except Exception:  # noqa: BLE001
                text = ""
            out.append((el, text))
        return out

    def _handle_confirmation(self, result, label: str, expect_side: Optional[str],
                             expect_qty: Optional[int]) -> str:
        """Poll for the confirmation popover, verify intent, click the
        affirmative. Handles both the Buy/Sell/Cancel and the Yes/No shapes.
        Raises (after cancelling) if the dialog text contradicts our intent.
        """
        import time
        # ADAPTIVE POLL (2026-08-25): the confirmation dialog is a TERMINAL setting -
        # when the user has suppressed it ('don't show again'), every order was burning
        # the full poll (~6-10s, elastic under load) waiting for a dialog that never
        # comes. Remember whether the last fire saw one: suppressed -> short poll; the
        # moment a popover IS seen again -> back to the full poll. Missing a slow popover
        # is safe: the order simply doesn't go out and the caller's net-verify reports
        # 'nothing landed' -> ticket fallback.
        rounds = 10 if getattr(self, "_confirm_seen", True) else 3
        popover = None
        for _ in range(rounds):
            popover = actions.find(self.driver, locators.ORDER_CONFIRM_POPOVER,
                                   timeout=0.4, visible_only=True)
            if popover is not None:
                break
            time.sleep(0.25)
        self._confirm_seen = popover is not None
        if popover is None:
            return "no confirmation shown (suppressed or instant)"

        try:
            title = " ".join((popover.text or "").split())
        except Exception:  # noqa: BLE001
            title = ""
        result.meta["confirm_title"] = title[:70]

        buttons = self._popover_buttons(popover)
        texts = [t for _, t in buttons]

        # verify intent ONLY when the dialog carries a side/qty (market orders).
        # Yes/No exit dialogs carry neither - nothing to cross-check there.
        from browser import humanize
        if expect_side and any(expect_side.lower() in t.lower() for t in texts):
            if expect_qty is not None and title and str(expect_qty) not in title:
                for el, t in buttons:
                    if t.strip().lower() in self._NEGATIVE:
                        humanize.human_click(self.driver, el, "confirm Cancel")
                        break
                raise RuntimeError(
                    f"confirmation qty mismatch (title {title!r}), cancelled")

        # choose the affirmative button
        wanted = [w for w in ([expect_side] + list(self._AFFIRMATIVE_EXTRA)) if w]
        submit = None
        for want in wanted:
            for el, t in buttons:
                if t.strip().lower() == want.lower():
                    submit = el
                    break
            if submit is not None:
                break
        if submit is None:                       # last resort: first non-negative
            for el, t in buttons:
                if t.strip().lower() not in self._NEGATIVE and t.strip():
                    submit = el
                    break
        if submit is None:
            raise RuntimeError(f"confirmation present ({texts}) but no affirmative "
                               "button identified")

        if not humanize.human_click(self.driver, submit, "confirm submit"):
            raise RuntimeError("could not click confirmation submit")
        self.settle(0.6, 1.0)
        return f"confirmed ({title[:24]!r} via {texts})"

    def _fire(self, action_name: str, button_locator, label: str,
              expect_symbol: Optional[str], dry_run: bool,
              expect_qty: Optional[int] = None) -> ActionResult:
        result = ActionResult(action=action_name)
        result.meta["expect_symbol"] = expect_symbol
        result.meta["dry_run"] = dry_run
        try:
            self._precheck(result)
            scope = self._scope()

            result.enter("verify_symbol")
            panel_symbol = self._panel_symbol(scope)
            if expect_symbol:
                want = expect_symbol.strip().upper()

                # panel header is the root symbol (e.g. MNQU6); accept prefix match
                def _matches(ps):
                    return bool(ps) and (ps.startswith(want) or want.startswith(ps))

                # HARDENED (2026-08-20): the panel header LAGS a fresh symbol/tab switch,
                # so a first read can still show the previous symbol and we'd needlessly
                # refuse (forcing the fragile ticket fallback). Poll briefly for it to
                # catch up before refusing. Read-only - no extra clicks, no dup risk.
                if not _matches(panel_symbol):
                    import time as _time
                    deadline = _time.monotonic() + 4.0
                    while _time.monotonic() < deadline:
                        _time.sleep(0.3)
                        panel_symbol = self._panel_symbol(scope)
                        if _matches(panel_symbol):
                            break
                result.meta["panel_symbol"] = panel_symbol
                if not _matches(panel_symbol):
                    raise RuntimeError(
                        f"panel shows {panel_symbol!r}, refusing to click {label} "
                        f"for {want!r}")
            else:
                result.meta["panel_symbol"] = panel_symbol

            result.enter("locate_button")
            button = actions.require(scope, button_locator, f"{label} button",
                                     timeout=4)
            self.assert_safe_to_click(label)
            self.tick()

            if dry_run:
                result.meta["note"] = "dry_run - button located, NOT clicked"
                return self._finish(result.succeed({"located": True,
                                                    "symbol": panel_symbol}))

            result.enter("click")
            from browser import humanize
            if not humanize.human_click(self.driver, button, f"{label} button"):
                raise RuntimeError(f"could not click {label}")
            self.tick(0.3, 0.6)

            result.enter("confirm")
            status = self._handle_confirmation(
                result, label, self._confirm_side(label), expect_qty)
            result.meta["confirmation"] = status
            self.settle()
            return self._finish(result.succeed({"clicked": True,
                                                "symbol": panel_symbol,
                                                "confirmation": status}))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)

    def buy_market(self, expect_symbol=None, dry_run=True, expect_qty=None) -> ActionResult:
        return self._fire("trade_panel.buy_market", locators.TRADE_BUY_MKT,
                          "Buy Mkt", expect_symbol, dry_run, expect_qty)

    def sell_market(self, expect_symbol=None, dry_run=True, expect_qty=None) -> ActionResult:
        return self._fire("trade_panel.sell_market", locators.TRADE_SELL_MKT,
                          "Sell Mkt", expect_symbol, dry_run, expect_qty)

    def buy_bid(self, expect_symbol=None, dry_run=True, expect_qty=None) -> ActionResult:
        return self._fire("trade_panel.buy_bid", locators.TRADE_BUY_BID,
                          "Buy Bid", expect_symbol, dry_run, expect_qty)

    def sell_ask(self, expect_symbol=None, dry_run=True, expect_qty=None) -> ActionResult:
        return self._fire("trade_panel.sell_ask", locators.TRADE_SELL_ASK,
                          "Sell Ask", expect_symbol, dry_run, expect_qty)

    def exit_at_mkt(self, expect_symbol=None, dry_run=True) -> ActionResult:
        """'Exit at Mkt & Cxl' - flattens the panel symbol AND cancels its
        working orders. Destructive; defaults to dry_run."""
        return self._fire("trade_panel.exit_at_mkt", locators.TRADE_EXIT_BUTTON,
                          "Exit at Mkt", expect_symbol, dry_run)
