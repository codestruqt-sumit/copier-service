"""Order Ticket widget: full order entry (symbol / side / qty / price / type /
TIF), Send, and verification via the Orders panel.

Fields (confirmed 2026-08-18): symbol search box, Buy/Sell, Qty (combobox),
Price, Order Type (MARKET/LIMIT/STOP/STOP LIMIT/TRL STOP/TRL STP), Flags
(DAY/GTC/GTD), Release On/Off, Send, Reset.

Each setter verifies by read-back where possible. place() orchestrates a full
ticket and confirms acceptance:
  * non-market -> a matching working order appears in the Orders panel
  * market     -> caller should verify via Positions (composite does this)
Send defaults to dry_run: the ticket is fully built and validated, Send is
located but NOT clicked, so the whole path is exercised without an order.
"""

from __future__ import annotations

from typing import Optional

from browser import actions, humanize
from config import locators
from core.results import ActionResult
from trading.base import TerminalModule
from trading.orders_panel import OrdersPanel

from selenium.webdriver.common.keys import Keys


class OrderTicket(TerminalModule):
    # LEARNING 2026-08-18: the ticket's search box (and some fields) are custom
    # controls whose real <input> is geometrically present but reports
    # is_displayed()=False (transparent input over a styled display element).
    # So EVERY ticket field is located by PRESENCE, not display, and driven
    # through the JS-fallback paths in actions.type_into / actions.click.
    def _find(self, scope, loc, timeout: float = 5, **fmt):
        return actions.find(scope, loc, timeout=timeout, visible_only=False, **fmt)

    def _ticket_has_box(self) -> bool:
        """True if the ORDER TICKET stack shows its search box (entry mode).

        CRITICAL: there are TWO .search-box--input on the page - the top-nav
        search (always present) and the ticket's. A global querySelector always
        returns the nav one, so the check MUST be scoped to the ticket stack, or
        details-view recovery never fires (this was the bug behind every ticket
        'search box absent' failure)."""
        return bool(self.driver.execute_script(r"""
            function c(s){return (s||'').replace(/\s+/g,' ').trim();}
            var stack=null;
            document.querySelectorAll('.lm_stack').forEach(function(s){
              var t=s.querySelector('.lm_tab.lm_active .lm_title');
              if(t&&c(t.textContent).indexOf('Order Ticket')!==-1) stack=s;});
            return !!(stack && stack.querySelector('.search-box--input'));
        """))

    def _require(self, scope, loc, what, timeout: float = 5, **fmt):
        el = self._find(scope, loc, timeout=timeout, **fmt)
        if el is None:
            raise RuntimeError(f"{what}: not present")
        return el

    def _scope(self, timeout: float = 20.0):
        """Locate the Order Ticket entry form.

        Handles the ORDER-DETAILS view: after an order is placed or a row is
        clicked the ticket flips to a details panel (back arrow, no search box).
        Unified loop: if the search box is absent, click the ticket's own
        .icon-back (NOT .lm_left / generic [class*=back], which are layout
        chrome) to return to entry mode; then return the scope as soon as the
        box is found.
        """
        import time
        from browser import humanize
        deadline = time.time() + timeout
        while time.time() < deadline:
            has_box = self._ticket_has_box()
            if not has_box:
                # Click the back button's .btn element DIRECTLY via JS.
                # LEARNING 2026-08-18: coordinate clicks (human/CDP/native) FAIL
                # because the details-view back arrow is COVERED by the tab-title
                # span - elementFromPoint returns .lm_title, so every coord click
                # hits the tab, not the arrow. A JS .click() on the button element
                # bypasses coverage entirely and works reliably.
                self.driver.execute_script(r"""
                    function c(s){return (s||'').replace(/\s+/g,' ').trim();}
                    var stack=null;
                    document.querySelectorAll('.lm_stack').forEach(function(s){
                      var t=s.querySelector('.lm_tab.lm_active .lm_title');
                      if(t&&c(t.textContent).indexOf('Order Ticket')!==-1) stack=s;});
                    if(!stack) return;
                    var e = stack.querySelector('.icon-back')
                            || stack.querySelector('.icon-arrow-left');
                    if(!e) return;
                    (e.closest('.btn') || e.parentElement || e).click();
                """)
                time.sleep(0.9)
                continue
            candidate = actions.find(self.driver, locators.TICKET_SCOPE, timeout=1)
            box = (actions.find(candidate, locators.TICKET_SYMBOL_INPUT,
                                timeout=0.5, visible_only=False)
                   if candidate is not None else None)
            if candidate is not None and box is not None:
                return candidate
            time.sleep(0.4)
        raise RuntimeError("order ticket widget not found / never settled "
                           "(search box absent)")

    # -- read ----------------------------------------------------------------
    def read_state(self) -> ActionResult:
        result = ActionResult(action="ticket.read")
        try:
            self._precheck(result)
            result.enter("read")
            scope = self._scope()

            def val(loc):
                el = self._find(scope, loc, timeout=2)
                return el.get_attribute("value") if el else None

            sym_el = self._find(scope, locators.TICKET_SYMBOL_INPUT, timeout=2)
            type_el = self._find(scope, locators.TICKET_ORDER_TYPE, timeout=2)
            state = {
                "symbol": sym_el.get_attribute("value") if sym_el else None,
                "qty": val(locators.TICKET_QTY_INPUT),
                "price": val(locators.TICKET_PRICE_INPUT),
                "order_type": " ".join((type_el.text or "").split()) if type_el else None,
                "has_send": self._find(scope, locators.TICKET_SEND, timeout=1) is not None,
            }
            return self._finish(result.succeed(state))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)

    # -- setters (each returns ActionResult) ---------------------------------
    def set_symbol(self, symbol: str) -> ActionResult:
        """Set the ticket symbol via the S-sync toggle (follows the chart).

        LEARNING 2026-08-18: typing into the symbol search box does NOT commit
        the contract - Send then errors 'Symbol should be specified'. The
        reliable path is to switch the CHART to the symbol and let the ticket's
        S-link mirror it. So this switches the chart first, then ensures S is on.
        """
        result = ActionResult(action="ticket.set_symbol")
        want = symbol.strip().upper()
        result.meta["symbol"] = want
        try:
            self._precheck(result)

            # WINNING RECIPE (2026-08-18): type the symbol char-by-char (each
            # keystroke drives the React search), wait for the autocomplete
            # anchor `a.hover`, then DOUBLE-CLICK it. Single clicks (JS, CDP,
            # native, offset) never commit - only a double-click selects the
            # contract into the order model. Verified: places a real working
            # order afterwards.
            from selenium.webdriver.common.action_chains import ActionChains
            from selenium.webdriver.common.by import By

            scope = self._scope()

            def type_symbol():
                box = self._require(scope, locators.TICKET_SYMBOL_INPUT,
                                    "ticket symbol input", timeout=4)
                self.driver.execute_script("arguments[0].focus();", box)
                box.send_keys(Keys.CONTROL, "a")
                box.send_keys(Keys.DELETE)
                self.tick(0.2, 0.4)
                for ch in want:
                    box.send_keys(ch)
                    self.tick(0.25, 0.45)      # per-keystroke, drives the search
                self.tick(0.6, 0.9)            # let the dropdown render
                return box

            def dropdown_open():
                return self.driver.execute_script(
                    "return !!document.querySelector('.search-box.open');")

            # The commit signal is the DROPDOWN CLOSING - the box shows the typed
            # text whether or not the contract committed, so we can't trust the
            # box value alone. Retry type + double-click until the dropdown closes.
            committed = False
            for attempt in range(3):
                result.enter(f"type_symbol_{attempt + 1}")
                box = type_symbol()
                result.enter("select_result")
                anchor = None
                for _ in range(6):
                    hits = self.driver.find_elements(By.CSS_SELECTOR, "a.hover")
                    anchor = next((a for a in hits if want in
                                   (a.get_attribute("textContent") or "").upper()), None)
                    if anchor is not None:
                        break
                    self.tick(0.2, 0.4)
                if anchor is None:
                    continue                   # dropdown didn't render; re-type
                ActionChains(self.driver).move_to_element(anchor).pause(0.3) \
                    .double_click().perform()
                self.settle()
                if not dropdown_open():         # dropdown closed => committed
                    committed = True
                    break

            result.enter("verify")
            box = self._find(scope, locators.TICKET_SYMBOL_INPUT, timeout=3)
            shown = (box.get_attribute("value") or "").upper() if box else ""
            if not committed:
                raise RuntimeError(f"symbol {want!r} did not commit "
                                   f"(dropdown stayed open; box={shown!r})")
            if not shown or not (want in shown or shown in want):
                raise RuntimeError(f"ticket symbol not committed (box={shown!r})")
            return self._finish(result.succeed({"symbol": shown, "committed": True}))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)

    def set_side(self, side: str) -> ActionResult:
        result = ActionResult(action="ticket.set_side")
        side = side.strip().lower()
        result.meta["side"] = side
        try:
            if side not in ("buy", "sell"):
                raise ValueError(f"side must be buy/sell, got {side!r}")
            self._precheck(result)
            scope = self._scope()
            result.enter("click_side")
            loc = locators.TICKET_BUY if side == "buy" else locators.TICKET_SELL
            el = self._require(scope, loc, f"{side} toggle")
            from browser import humanize as _h
            _h.human_click(self.driver, el, f"{side} toggle")
            self.settle()
            return self._finish(result.succeed({"side": side}))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)

    def _set_field(self, action_name, locator, value, label) -> ActionResult:
        result = ActionResult(action=action_name)
        result.meta["value"] = value
        try:
            self._precheck(result)
            scope = self._scope()
            result.enter("locate")
            field = self._require(scope, locator, label, timeout=4)
            humanize.human_click(self.driver, field, label)
            self.tick()
            result.enter("type")
            if not actions.type_into(self.driver, field, str(value)):
                raise RuntimeError(f"could not type into {label}")
            self.tick()
            result.enter("verify")
            shown = (field.get_attribute("value") or "").strip()
            if str(value) not in shown:
                raise RuntimeError(f"{label} read-back mismatch: {shown!r} vs {value!r}")
            return self._finish(result.succeed({label: shown}))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)

    def set_qty(self, qty: int) -> ActionResult:
        """Commit qty via the dropdown (presets) or type+Enter (other values).

        LEARNING 2026-08-18: typing into the qty combobox does NOT commit
        (input shows the number, model stays 'Select'). Presets 1/2/3/4/5/10/
        15/20 commit by picking from the dropdown; other values by type+Enter.
        """
        result = ActionResult(action="ticket.set_qty")
        result.meta["qty"] = qty
        try:
            qty = int(qty)
            if qty < 1:
                raise ValueError(f"qty must be >= 1, got {qty}")
            self._precheck(result)
            scope = self._scope()

            # SHORT-CIRCUIT (2026-08-20): if the ticket already shows the target qty,
            # don't touch the fragile preset dropdown at all. qty=1 is BOTH the ticket's
            # default and the common request, so this skips the "qty option N not found in
            # dropdown" flake entirely for the usual case. Only when the shown value
            # differs do we fall through to the dropdown/type path.
            existing = self._find(scope, locators.TICKET_QTY_INPUT, timeout=1)
            if existing is not None:
                current = (existing.get_attribute("value") or "").strip()
                if current == str(qty):
                    result.meta["qty_shortcircuit"] = True
                    return self._finish(result.succeed({"qty": qty}))

            if qty in locators.TICKET_QTY_PRESETS:
                # WINNING RECIPE: open the qty dropdown and pick the option via
                # CDP TRUSTED clicks (synthetic JS clicks are rejected by this
                # React combobox; CDP is accepted). Coords from getBoundingClientRect.
                # WINNING RECIPE: open the qty dropdown and pick the option via CDP
                # TRUSTED clicks. HARDENED (2026-08-20): under load / after a tab refresh
                # the toggle click sometimes doesn't open the menu (or its coords are
                # stale), which used to fail as "qty option N not found". Retry the whole
                # open-and-find a few times, re-locating the toggle each round and polling
                # for the option to render, so a transient non-open self-heals.
                _LOCATE_TOGGLE = r"""
                    var stack=null;
                    document.querySelectorAll('.lm_stack').forEach(function(s){
                      var t=s.querySelector('.lm_tab.lm_active .lm_title');
                      if(t&&t.textContent.indexOf('Order Ticket')!==-1) stack=s;});
                    if(!stack) return null;
                    var lbl=[].slice.call(stack.querySelectorAll('label'))
                        .find(function(l){return l.textContent.trim()==='Qty';});
                    if(!lbl) return null;
                    var combo=lbl.parentElement.querySelector('.select-input')
                              || lbl.nextElementSibling;
                    var tg=combo?combo.querySelector('.btn,[class*=toggle],[class*=caret]'):null;
                    if(!tg) return null;
                    var r=tg.getBoundingClientRect();
                    return [r.x+r.width/2, r.y+r.height/2];
                """
                _FIND_OPT = r"""
                    var want=String(arguments[0]);
                    var found=null;
                    document.querySelectorAll('li,[class*=option],[role=option]').forEach(function(el){
                      if(found) return;
                      var t=(el.textContent||'').trim();
                      var r=el.getBoundingClientRect();
                      if(t===want && r.width>0 && r.height>0 && r.height<44)
                        found=[r.x+r.width/2, r.y+r.height/2];
                    });
                    return found;
                """
                result.enter("open_qty_dropdown")
                opt_xy = None
                tries = 0
                for tries in range(1, 4):                 # up to 3 open attempts
                    toggle_xy = self.driver.execute_script(_LOCATE_TOGGLE)
                    if not toggle_xy:
                        self.tick(0.4, 0.7)               # ticket not ready yet - settle + retry
                        continue
                    self.cdp_click(toggle_xy[0], toggle_xy[1])
                    for _ in range(8):                    # poll ~2s for options to render
                        self.tick(0.2, 0.35)
                        opt_xy = self.driver.execute_script(_FIND_OPT, qty)
                        if opt_xy:
                            break
                    if opt_xy:
                        break
                    self.tick(0.4, 0.7)                   # didn't open - settle, then re-open
                result.meta["qty_dropdown_tries"] = tries
                if not opt_xy:
                    raise RuntimeError(f"qty option {qty} not found in dropdown "
                                       f"(after {tries} open attempts)")

                result.enter("pick_qty")
                self.cdp_click(opt_xy[0], opt_xy[1])
                self.tick()
            else:
                result.enter("type_qty")
                field = self._require(scope, locators.TICKET_QTY_INPUT, "Qty")
                humanize.human_click(self.driver, field, "qty input")
                actions.type_into(self.driver, field, str(qty))
                field.send_keys(Keys.ENTER)
                self.tick()

            result.enter("verify")
            field = self._find(scope, locators.TICKET_QTY_INPUT, timeout=2)
            shown = (field.get_attribute("value") or "").strip() if field else ""
            if shown != str(qty):
                raise RuntimeError(f"qty not committed: shows {shown!r}, wanted {qty}")
            return self._finish(result.succeed({"qty": qty}))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)

    def set_price(self, price) -> ActionResult:
        result = ActionResult(action="ticket.set_price")
        result.meta["price"] = price
        try:
            self._precheck(result)
            scope = self._scope()
            result.enter("locate")
            field = self._require(scope, locators.TICKET_PRICE_INPUT, "Price",
                                  timeout=4)
            # WINNING RECIPE: focus + clear + send_keys + Tab. Do NOT use
            # type_into's JS-value fallback here - it sets the display value but
            # does NOT commit to the order model (order then places at market
            # default, not the intended price).
            self.driver.execute_script("arguments[0].focus();", field)
            field.send_keys(Keys.CONTROL, "a")
            field.send_keys(Keys.DELETE)
            self.tick(0.2, 0.3)
            result.enter("type")
            field.send_keys(str(price))
            field.send_keys(Keys.TAB)            # commit
            self.tick()
            result.enter("verify")
            shown = (field.get_attribute("value") or "").strip()
            # price may be normalised (29500 -> 29500.00); compare numerically
            try:
                if abs(float(shown) - float(price)) > 1e-6:
                    raise ValueError
            except ValueError:
                raise RuntimeError(f"price read-back mismatch: {shown!r} vs {price!r}")
            return self._finish(result.succeed({"price": shown}))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)

    def set_stop_price(self, price) -> ActionResult:
        """Set the STOP (trigger) price - the SECOND price field, present only for
        STOP LIMIT. Mirrors set_price's committed recipe against the 'Stop Price'
        input (separate from the 'Price'/limit input)."""
        result = ActionResult(action="ticket.set_stop_price")
        result.meta["price"] = price
        try:
            self._precheck(result)
            scope = self._scope()
            result.enter("locate")
            field = self._require(scope, locators.TICKET_STOP_PRICE_INPUT, "Stop Price",
                                  timeout=4)
            self.driver.execute_script("arguments[0].focus();", field)
            field.send_keys(Keys.CONTROL, "a")
            field.send_keys(Keys.DELETE)
            self.tick(0.2, 0.3)
            result.enter("type")
            field.send_keys(str(price))
            field.send_keys(Keys.TAB)            # commit
            self.tick()
            result.enter("verify")
            shown = (field.get_attribute("value") or "").strip()
            try:
                if abs(float(shown) - float(price)) > 1e-6:
                    raise ValueError
            except ValueError:
                raise RuntimeError(f"stop price read-back mismatch: {shown!r} vs {price!r}")
            return self._finish(result.succeed({"stop_price": shown}))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)

    def set_order_type(self, order_type: str) -> ActionResult:
        result = ActionResult(action="ticket.set_order_type")
        want = order_type.strip().upper()
        result.meta["order_type"] = want
        try:
            if want not in locators.ORDER_TYPES:
                raise ValueError(f"unknown order type {want!r}; "
                                 f"known: {locators.ORDER_TYPES}")
            self._precheck(result)
            scope = self._scope()
            result.enter("open_dropdown")
            select = self._require(scope, locators.TICKET_ORDER_TYPE,
                                   "order type select", timeout=4)

            # The selected value is the FIRST line of the select text; the rest
            # is the (always-present) option list. Compare the selected line
            # EXACTLY - 'STOP'.startswith is True for 'STOP LIMIT' too, which
            # caused false "already"/false "verified" results.
            def selected_type():
                el = self._find(scope, locators.TICKET_ORDER_TYPE, timeout=2)
                if not el:
                    return ""
                raw = (el.text or "").strip().upper()
                first = raw.split("\n")[0].strip()
                # the display line may be 'STOP LIMIT' (2 words) or one word;
                # match against the known types longest-first
                for t in ("STOP LIMIT", "TRL STOP", "TRL STP", "MARKET",
                          "LIMIT", "STOP"):
                    if first.startswith(t):
                        return t
                return first

            if selected_type() == want:
                return self._finish(result.succeed({"order_type": want,
                                                    "already": True}))
            sel_bottom = self.driver.execute_script(
                "var b=arguments[0].getBoundingClientRect();return b.y+b.height;", select)

            # LEARNING 2026-08-18: order-type OPTIONS have EMPTY class (not <li>,
            # not [class*=option]) - search leaf elements by EXACT text BELOW the
            # select (its own display holds the text too). Retry the open+find:
            # a single CDP open sometimes doesn't render the list in time.
            def find_option():
                # search ALL leaf elements (options can be any tag with empty
                # class); exact text; below the select's own display row.
                return self.driver.execute_script(r"""
                    var want=String(arguments[0]); var minY=arguments[1]; var found=null;
                    var all=document.querySelectorAll('*');
                    for(var i=0;i<all.length;i++){
                      var el=all[i];
                      if(el.childElementCount>0) continue;
                      if((el.textContent||'').trim()!==want) continue;
                      var r=el.getBoundingClientRect();
                      if(r.width>0 && r.height>0 && r.height<44 && r.y>=minY-2){
                        found=[r.x+r.width/2, r.y+r.height/2]; break;}
                    }
                    return found;
                """, want, sel_bottom)

            self.assert_safe_to_click(want)
            # RETRY the whole open->pick->verify: the option click occasionally
            # misses, leaving the type unchanged. Re-open and re-select until the
            # selected value actually reads back as `want`.
            for attempt in range(3):
                result.enter(f"pick_option_{attempt + 1}")
                sel = self._find(scope, locators.TICKET_ORDER_TYPE, timeout=3) or select
                self.cdp_click_element(sel)          # open dropdown
                opt_xy = None
                for _ in range(8):
                    self.tick(0.2, 0.4)
                    opt_xy = find_option()
                    if opt_xy:
                        break
                if not opt_xy:
                    self.tick(0.3, 0.5)              # dropdown didn't render; retry
                    continue
                self.cdp_click(opt_xy[0], opt_xy[1])
                self.settle()
                shown = selected_type()
                if shown == want:
                    return self._finish(result.succeed(
                        {"order_type": want, "attempts": attempt + 1}))
            raise RuntimeError(f"order type not set after retries: shows {shown!r}")
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)

    def set_flag(self, flag: str) -> ActionResult:
        result = ActionResult(action="ticket.set_flag")
        want = flag.strip().upper()
        result.meta["flag"] = want
        try:
            if want not in ("DAY", "GTC", "GTD"):
                raise ValueError(f"flag must be DAY/GTC/GTD, got {want!r}")
            self._precheck(result)
            scope = self._scope()
            result.enter("click_flag")
            el = self._require(scope, locators.TICKET_FLAG, f"flag {want}", value=want)
            from browser import humanize as _h
            _h.human_click(self.driver, el, f"flag {want}")
            self.settle()
            return self._finish(result.succeed({"flag": want}))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)

    def reset(self) -> ActionResult:
        result = ActionResult(action="ticket.reset")
        try:
            self._precheck(result)
            scope = self._scope()
            result.enter("click_reset")
            el = self._require(scope, locators.TICKET_RESET, "Reset")
            from browser import humanize as _h
            _h.human_click(self.driver, el, "Reset")
            self.settle()
            return self._finish(result.succeed({"reset": True}))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)

    def send(self, dry_run: bool = True) -> ActionResult:
        result = ActionResult(action="ticket.send")
        result.meta["dry_run"] = dry_run
        try:
            self._precheck(result)
            scope = self._scope()
            result.enter("locate_send")
            send = self._require(scope, locators.TICKET_SEND, "Send button",
                                 timeout=4)
            if dry_run:
                result.meta["note"] = "dry_run - Send located, NOT clicked"
                return self._finish(result.succeed({"located": True}))
            self.assert_safe_to_click("Send")
            result.enter("click_send")
            if not humanize.human_click(self.driver, send, "Send"):
                raise RuntimeError("could not click Send")
            self.tick(0.3, 0.6)

            # Send may raise a confirmation popover, an error popover, or place
            # directly. Resolve whichever appears.
            result.enter("confirm")
            status, text = self.handle_confirm_dialog()
            result.meta["dialog"] = f"{status}: {text[:50]}"
            if status == "error":
                raise RuntimeError(f"order rejected by ticket: {text}")
            self.settle()
            return self._finish(result.succeed({"sent": True, "dialog": status}))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)

    # -- reset ---------------------------------------------------------------
    def reset_to_entry(self) -> bool:
        """Force the ticket back to a clean entry form: recover from the
        order-details view, then click Reset to clear leftover fields. Returns
        True if the search box is present afterward."""
        try:
            self._scope()                     # recovers from details view
        except Exception:  # noqa: BLE001
            return False
        try:
            self.reset()                      # clear any leftover field state
        except Exception:  # noqa: BLE001
            pass
        return self._ticket_has_box()

    # -- orchestrated (HARDENED) --------------------------------------------
    def place(self, symbol: str, side: str, qty: int, order_type: str,
              price=None, stop_price=None, flag: str = "DAY", dry_run: bool = True,
              max_attempts: int = 3) -> ActionResult:
        """Place an order via the Order Ticket, retrying through the ticket's
        single-shot flakiness.

        SAFETY: only PRE-submission failures are retried. Once an order has
        actually been submitted (confirmation accepted), we never retry - a
        second attempt would duplicate it. If it was submitted but we could not
        verify it in the Orders grid, we return the result flagged
        `submitted=True` + a warning so the caller can check/cancel rather than
        risk a double.
        """
        last = None
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                self.reset_to_entry()
                self.settle(1.0, 1.8)         # let it settle before retry
            last = self._place_once(symbol, side, qty, order_type, price, stop_price,
                                    flag, dry_run, attempt=attempt)
            if last.ok:
                last.meta["attempts"] = attempt
                return last
            if last.meta.get("submitted"):
                # an order WAS sent; do NOT retry (duplicate risk)
                last.meta["attempts"] = attempt
                last.meta["warning"] = ("order submitted but not verified - "
                                        "check the Orders panel / cancel manually")
                return last
            # pre-submission failure -> safe to retry
        if last is not None:
            last.meta["attempts"] = max_attempts
        return last

    def _place_once(self, symbol, side, qty, order_type, price, stop_price, flag, dry_run,
                    attempt=1) -> ActionResult:
        result = ActionResult(action="ticket.place")
        result.meta.update({"symbol": symbol, "side": side, "qty": qty,
                            "order_type": order_type, "price": price,
                            "stop_price": stop_price,
                            "flag": flag, "dry_run": dry_run, "attempt": attempt,
                            "submitted": False})
        try:
            self._precheck(result)
            want_type = order_type.strip().upper()
            needs_price = want_type in ("LIMIT", "STOP", "STOP LIMIT",
                                        "TRL STOP", "TRL STP")
            needs_stop = want_type == "STOP LIMIT"   # the second (trigger) price
            if needs_price and price is None:
                raise ValueError(f"{want_type} needs a price")
            if needs_stop and stop_price is None:
                raise ValueError(f"{want_type} needs a stop price")

            # warm the ticket into entry mode ONCE (so per-field _scope calls
            # are fast instead of each burning the recovery timeout)
            result.enter("ensure_entry")
            if not self.reset_to_entry():
                raise RuntimeError("could not bring ticket to entry mode")

            # snapshot working-order ids before, to detect the new one
            before_ids = set()
            if not dry_run and needs_price:
                snap = OrdersPanel(self.driver).report()
                if snap.ok:
                    before_ids = {o["id"] for o in snap.data["orders"]}

            # order_type FIRST: it selects reliably on a fresh ticket, and doing
            # it before symbol/side avoids an interaction that made it miss
            # (verified 2026-08-18).
            for stage, call in (
                ("order_type", lambda: self.set_order_type(order_type)),
                ("symbol", lambda: self.set_symbol(symbol)),
                ("side", lambda: self.set_side(side)),
                ("qty", lambda: self.set_qty(qty)),
            ):
                result.enter(f"set_{stage}")
                sub = call()
                if not sub.ok:
                    raise RuntimeError(f"set {stage} failed: {sub.error}")
                self.tick()
            # re-assert order type in case setting symbol/side reset it
            result.enter("reassert_order_type")
            if self.set_order_type(order_type).ok is False:
                raise RuntimeError("order type did not hold after symbol/side")

            if needs_price:
                result.enter("set_price")
                sub = self.set_price(price)            # 'Price' field = limit cap
                if not sub.ok:
                    raise RuntimeError(f"set price failed: {sub.error}")
            if needs_stop:
                result.enter("set_stop_price")
                sub = self.set_stop_price(stop_price)  # 'Stop Price' field = trigger
                if not sub.ok:
                    raise RuntimeError(f"set stop price failed: {sub.error}")

            result.enter("set_flag")
            self.set_flag(flag)

            if dry_run:
                self.send(dry_run=True)       # locate Send, don't click
                return self._finish(result.succeed(
                    {"dry_run": True, "note": "ticket built & validated, not sent"}))

            result.enter("send")
            sent = self.send(dry_run=False)
            if not sent.ok:
                # send() raises on an error popover -> nothing was submitted
                raise RuntimeError(f"send failed: {sent.error}")
            # confirmation accepted -> an order is now live. From here, NO retry.
            result.meta["submitted"] = True

            if needs_price:
                result.enter("verify_order")
                import time as _t
                # The Orders grid labels a stop-limit row 'Limit' (the 'Stop' part is
                # in a separate column), so matching on the type string misses it.
                # For these types, verify by "a NEW working order appeared on this
                # contract" (id not present before the send) instead.
                verify_type = (None if want_type in ("STOP LIMIT", "TRL STOP", "TRL STP")
                               else want_type)
                new_orders = []
                deadline = _t.time() + 8
                while _t.time() < deadline:
                    self.settle(0.6, 1.0)
                    found = OrdersPanel(self.driver).find(
                        contract=symbol, order_type=verify_type,
                        ids_to_exclude=before_ids)
                    new_orders = found.data.get("matches", []) if found.ok else []
                    if new_orders:
                        break
                if not new_orders:
                    raise RuntimeError("order sent but no matching working order "
                                       "appeared in the Orders panel")
                return self._finish(result.succeed(
                    {"sent": True, "working_order": new_orders[0]}))

            return self._finish(result.succeed(
                {"sent": True, "note": "market order - verify via Positions"}))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)
