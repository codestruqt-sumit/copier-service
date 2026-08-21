"""Account selector on the top bar: read, open, close - and later, switch.

=============================================================================
 LEARNINGS (live probe of the logged-in terminal, 2026-08-18)
=============================================================================
 * The selector is `div.pane.account-selector.dropdown` in the top bar.
   - toggle/header : DIV.account (child .name holds the ACTIVE account id)
   - menu entries  : A.account, one per switchable account
   - selected      : the wrapping LI carries class 'selected'
 * DANGER: the same menu holds 'Manage Groups', 'Go to Replay' and 'Logout'
   as A.account.logout. Same class family as real accounts. Every read
   filters :not(.logout); every click is checked against the denylist in
   config/locators.FORBIDDEN_CLICK_TEXTS. A careless click here ends the
   session or locks the account ('Manual Lockout' sits nearby too).
 * Menu entries exist in the DOM even while the dropdown is CLOSED - they
   can be READ without any click. Clicking is only needed to make them
   visible for a real (human-visible) switch. list_accounts() therefore
   works in both modes; the click path exists to rehearse the mechanics
   we will need for switch_to().
 * OPEN/CLOSE MECHANICS (diagnosed empirically, 5 rounds, 2026-08-18):
   - open: click anywhere on the toggle header -> menu opens. Handler is
     OPEN-ONLY: re-clicking the toggle, clicking the .caret chevron, ESC
     (trusted and synthetic), outside clicks (real and synthetic), hover-out
     and synthetic window blur ALL leave it open. It closed spontaneously
     exactly once between sessions - suspected real-user mouse or focus
     change; never reproduced synthetically.
   - the open state is React inline-style driven: NO class change on the
     pane ('pane account-selector dropdown' both states); ul.dropdown-menu
     just gets display:block.
   - close: we VEIL it - set ul.dropdown-menu.style.display='none'. React
     state is untouched and the veil is fully reversible (lifting it brings
     the menu straight back). open_menu() always lifts the veil first, so
     the two operations stay consistent no matter who closed what.
   - offsetParent/bounding-rect are USELESS for open-detection here; the
     hit-test (elementFromPoint at item center) is the reliable signal.
 * Account ids look like 'TDFYSL00000000000'; menu label text also carries
   flags like 'Demo & Active'.
=============================================================================

Every public method returns an ActionResult and never raises. On failure a
screenshot lands in logs/failures/ and the result carries its path, so a
notifier can attach it to a Telegram/dashboard alert.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys

from browser import actions, humanize
from config import locators
from config.settings import LOG_DIR
from core import logging_setup
from core.notify import Notifier
from core.results import ActionResult
from trading.base import TerminalModule

log = logging_setup.get("trading.accounts")

# 'TDFYSL00000000000' and the like: letters then a long digit run
_ACCOUNT_ID = re.compile(r"^[A-Z]{2,10}[A-Z0-9]*\d{5,}$")

_JS_READ_MENU = """
/* truly_visible: computed-style walk + elementFromPoint hit-test.
   LEARNING 2026-08-18: offsetParent was USELESS here - this menu keeps its
   entries laid out even when closed, so offsetParent/bounding-rect said
   'visible' while a human saw nothing. The hit-test asks the browser what is
   actually on top at the element's center - that matches human sight. */
function truly_visible(el) {
  var r = el.getBoundingClientRect();
  if (r.width === 0 || r.height === 0) return false;
  var node = el;
  while (node && node.nodeType === 1) {
    var cs = getComputedStyle(node);
    if (cs.display === 'none' || cs.visibility === 'hidden' ||
        parseFloat(cs.opacity) === 0) return false;
    node = node.parentElement;
  }
  var cx = Math.max(0, Math.min(window.innerWidth - 1, r.x + r.width / 2));
  var cy = Math.max(0, Math.min(window.innerHeight - 1, r.y + r.height / 2));
  var hit = document.elementFromPoint(cx, cy);
  return !!hit && (el === hit || el.contains(hit) || hit.contains(el));
}
var items = [].slice.call(
    document.querySelectorAll('.pane.account-selector a.account'));
return items.map(function (a) {
  var li = a.closest('li');
  var cls = (typeof a.className === 'string') ? a.className : '';
  return {
    text: (a.textContent || '').trim(),
    cls: cls,
    is_logout_group: cls.indexOf('logout') !== -1,
    selected: !!(li && li.className.indexOf('selected') !== -1),
    displayed: truly_visible(a)
  };
});
"""


class AccountPanel(TerminalModule):
    """Read and drive the top-bar account selector.

    Inherits plumbing (screenshot/finish/fail/settle/tick/assert_safe_to_click)
    from TerminalModule; keeps its own _precheck (also checks the selector pane
    exists) and its dropdown-specific helpers.
    """

    def __init__(self, driver, notifier: Optional[Notifier] = None,
                 humanized: bool = True):
        super().__init__(driver, notifier)
        self.humanized = humanized

    # keep the denylist helper under the name this module already calls
    def _assert_safe_to_click(self, text: str) -> None:
        self.assert_safe_to_click(text)

    # -- stages ----------------------------------------------------------------
    def _precheck(self, result: ActionResult) -> bool:
        """We are on the logged-in terminal and the selector exists."""
        result.enter("precheck")
        url = self.driver.current_url
        if "trader.tradovate.com" not in url:
            raise RuntimeError(f"not on the terminal (url={url})")
        if "/welcome" in url:
            raise RuntimeError("on the LOGIN screen - session is logged out")
        pane = actions.find(self.driver, locators.ACCOUNT_SELECTOR, timeout=5)
        if pane is None:
            raise RuntimeError("account selector pane not found in top bar")
        return True

    def _read_menu(self) -> list[dict]:
        """Parse menu entries from the DOM (works with the menu closed)."""
        raw = self.driver.execute_script(_JS_READ_MENU) or []
        accounts = []
        for item in raw:
            if item.get("is_logout_group"):
                continue                      # Manage Groups / Replay / Logout
            text = " ".join((item.get("text") or "").split())
            token = text.split(" ")[0] if text else ""
            accounts.append({
                "id": token if _ACCOUNT_ID.match(token) else text,
                "label": text,
                "selected": bool(item.get("selected")),
                "visible": bool(item.get("displayed")),
            })
        return accounts

    def _menu_open(self) -> bool:
        """The menu is open when its entries are actually displayed."""
        try:
            raw = self.driver.execute_script(_JS_READ_MENU) or []
        except WebDriverException:
            return False
        entries = [r for r in raw if not r.get("is_logout_group")]
        return bool(entries) and all(r.get("displayed") for r in entries)

    def _veil(self, hide: bool) -> None:
        """Show/hide the dropdown list via inline style. See LEARNINGS: the
        app offers no human gesture that closes this menu, so we veil it.
        Reversible; React state untouched."""
        self.driver.execute_script(
            "var m=document.querySelector("
            "'.pane.account-selector ul.dropdown-menu');"
            "if(m) m.style.display = arguments[0] ? 'none' : '';",
            bool(hide))

    # -- public actions ---------------------------------------------------------
    def active_account(self) -> ActionResult:
        """Read the account id shown on the toggle. No clicks."""
        result = ActionResult(action="accounts.active")
        try:
            self._precheck(result)
            result.enter("read_toggle")
            text = actions.read_text(self.driver, locators.ACCOUNT_ACTIVE_NAME,
                                     timeout=5)
            if not text:
                raise RuntimeError("toggle found but active-account text is empty")
            active = " ".join(text.split())
            return self._finish(result.succeed({"active": active}))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)

    def open_menu(self) -> ActionResult:
        """Click the toggle until the menu entries are visible."""
        result = ActionResult(action="accounts.open_menu")
        try:
            self._precheck(result)

            # lift any veil a previous close left in place; if React still has
            # the menu open underneath, this alone makes it visible - no click
            result.enter("lift_veil")
            self._veil(False)
            humanize.pause(0.1, 0.2)

            result.enter("locate_toggle")
            toggle = actions.require(self.driver, locators.ACCOUNT_SELECTOR_TOGGLE,
                                     "account selector toggle", timeout=5)
            self._assert_safe_to_click(toggle.text)

            result.enter("click_toggle")
            already = self._menu_open()
            result.meta["menu_was_open"] = already
            if not already:
                clicker = humanize.human_click if self.humanized else actions.click
                if not clicker(self.driver, toggle, "account selector toggle"):
                    raise RuntimeError("could not click the account toggle")
                humanize.settle()

            result.enter("verify_open")
            if not self._menu_open():
                raise RuntimeError("clicked the toggle but menu entries did not "
                                   "become visible")
            return self._finish(result.succeed({"menu_open": True}))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)

    def close_menu(self) -> ActionResult:
        """ESC first (harmless); re-click the toggle only if ESC failed."""
        result = ActionResult(action="accounts.close_menu")
        try:
            self._precheck(result)

            # LEARNING 2026-08-18: this dropdown ignores Escape AND a second
            # toggle click. The only working close gesture is an OUTSIDE
            # click on a neutral element (we use the top-bar clock). Escape
            # stays first because it is free and harmless if they ever add it.
            result.enter("escape")
            if self._menu_open():
                try:
                    ActionChains(self.driver).send_keys(Keys.ESCAPE).perform()
                    humanize.pause()
                except WebDriverException:
                    pass

            if self._menu_open():
                result.enter("outside_click")
                neutral = actions.find(self.driver,
                                       locators.NEUTRAL_CLICK_TARGET, timeout=3)
                if neutral is not None:
                    humanize.human_click(self.driver, neutral,
                                         "neutral area (clock)")
                    humanize.settle()

            if self._menu_open():
                # LEARNING 2026-08-18: the handler is OPEN-ONLY; no human
                # gesture closes this menu (re-click, caret, hover-out, blur
                # all tested and failed). The veil is the reliable close.
                result.enter("veil")
                self._veil(True)
                humanize.pause(0.2, 0.4)
                result.meta["method"] = "js_veil"
            else:
                result.meta["method"] = "gesture"

            result.enter("verify_closed")
            still_open = self._menu_open()
            if still_open:
                raise RuntimeError("menu still visible even after the veil")
            return self._finish(result.succeed({"menu_open": False}))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)

    def switch_to(self, account_id: str, dry_run: bool = False) -> ActionResult:
        """Switch the active account to `account_id` (full or unique prefix).

        Opens the menu, clicks the matching entry (denylist-guarded so a stray
        Logout/Manage Groups can never be hit), then verifies the toggle now
        shows the target. Not found -> FAILED listing what IS available.
        """
        result = ActionResult(action="accounts.switch")
        target = (account_id or "").strip().upper()
        result.meta["target"] = target
        result.meta["dry_run"] = dry_run
        try:
            if not target:
                raise ValueError("no account id given")
            self._precheck(result)

            result.enter("read_active")
            active_before = actions.read_text(self.driver,
                                              locators.ACCOUNT_ACTIVE_NAME, timeout=3)
            active_before = " ".join(active_before.split()) if active_before else None
            if active_before and (active_before.upper() == target
                                  or active_before.upper().startswith(target)):
                return self._finish(result.succeed(
                    {"active": active_before, "already_active": True}))

            result.enter("open_menu")
            opened = self.open_menu()
            if not opened.ok:
                raise RuntimeError(f"open_menu failed: {opened.error}")

            result.enter("match")
            accounts = self._read_menu()
            matches = [a for a in accounts
                       if a["id"].upper() == target or a["id"].upper().startswith(target)]
            if not matches:
                self._veil(True)
                result.data = {"available": [a["id"] for a in accounts]}
                raise LookupError(
                    f"account {target!r} not found. Available: "
                    f"{[a['id'] for a in accounts]}")
            if len(matches) > 1:
                self._veil(True)
                raise LookupError(f"{target!r} is ambiguous: "
                                  f"{[a['id'] for a in matches]}")

            chosen = matches[0]
            result.meta["chosen"] = chosen["id"]
            self._assert_safe_to_click(chosen["label"])

            if dry_run:
                self._veil(True)
                return self._finish(result.succeed(
                    {"dry_run": True, "would_switch_to": chosen["id"]}))

            result.enter("click_account")
            # click the menu entry by exact id text (scoped to the selector menu)
            xpath = (f"xpath=//div[contains(@class,'account-selector')]"
                     f"//a[contains(@class,'account')]"
                     f"[not(contains(@class,'logout'))]"
                     f"[contains(normalize-space(.),'{chosen['id']}')]")
            entry = actions.find(self.driver, [xpath], timeout=4)
            if entry is None:
                raise RuntimeError(f"account entry {chosen['id']} vanished")
            if not humanize.human_click(self.driver, entry, f"account {chosen['id']}"):
                raise RuntimeError("could not click account entry")
            self.settle(0.8, 1.4)

            result.enter("verify")
            active_after = actions.read_text(self.driver,
                                             locators.ACCOUNT_ACTIVE_NAME, timeout=4)
            active_after = " ".join(active_after.split()) if active_after else None
            # menu usually closes on select; veil to be certain
            self._veil(True)
            if not active_after or not (active_after.upper() == chosen["id"].upper()
                                        or active_after.upper().startswith(target)):
                raise RuntimeError(f"switch not confirmed: toggle shows "
                                   f"{active_after!r}, wanted {chosen['id']!r}")
            return self._finish(result.succeed({
                "switched_from": active_before,
                "active": active_after,
            }))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)

    def switch_to_direct(self, account_id: str, dry_run: bool = False) -> ActionResult:
        """Switch account WITHOUT opening the dropdown menu.

        The menu entries live in the DOM even while the selector is CLOSED (see
        LEARNINGS), and the app's own onClick on the entry performs the switch. So we
        click the target entry directly and the menu never opens. This avoids the
        open-toggle + veil-close dance that switch_to() uses - the veil hides the menu
        without syncing React's open state (React still thinks it is open), and across
        the constant acct-to-acct switching that desync accumulates and eventually wedges
        the selector. Same guards as switch_to: denylist, exact-id match, not-found /
        ambiguous handling, and toggle-text verification after. Verified live 2026-08-19:
        the switch happens with the dropdown staying closed the whole time.
        """
        result = ActionResult(action="accounts.switch_direct")
        target = (account_id or "").strip().upper()
        result.meta["target"] = target
        result.meta["dry_run"] = dry_run
        try:
            if not target:
                raise ValueError("no account id given")
            self._precheck(result)

            result.enter("read_active")
            active_before = actions.read_text(self.driver,
                                              locators.ACCOUNT_ACTIVE_NAME, timeout=3)
            active_before = " ".join(active_before.split()) if active_before else None
            if active_before and (active_before.upper() == target
                                  or active_before.upper().startswith(target)):
                return self._finish(result.succeed(
                    {"active": active_before, "already_active": True}))

            result.enter("match")            # reads the DOM with the menu CLOSED
            accounts = self._read_menu()
            matches = [a for a in accounts
                       if a["id"].upper() == target or a["id"].upper().startswith(target)]
            if not matches:
                result.data = {"available": [a["id"] for a in accounts]}
                raise LookupError(f"account {target!r} not found. Available: "
                                  f"{[a['id'] for a in accounts]}")
            if len(matches) > 1:
                raise LookupError(f"{target!r} is ambiguous: "
                                  f"{[a['id'] for a in matches]}")
            chosen = matches[0]
            result.meta["chosen"] = chosen["id"]
            self._assert_safe_to_click(chosen["label"])   # denylist guard

            if dry_run:
                return self._finish(result.succeed(
                    {"dry_run": True, "would_switch_to": chosen["id"]}))

            result.enter("click_entry")
            # Match the entry whose FIRST token == the exact account id, and never a
            # .logout entry (Logout / Manage Groups / Replay share the class family).
            clicked = self.driver.execute_script(
                "var t=arguments[0];"
                "var links=[].slice.call(document.querySelectorAll("
                "  '.pane.account-selector a.account'));"
                "var el=links.find(function(a){"
                "  if((a.className+'').indexOf('logout')!==-1) return false;"
                "  var tok=((a.textContent||'').trim().split(/\\s+/)[0]||'');"
                "  return tok===t;});"
                "if(!el) return false; el.click(); return true;",
                chosen["id"])
            if not clicked:
                raise RuntimeError(f"account entry {chosen['id']} not found in DOM")
            self.settle(0.8, 1.4)

            result.enter("verify")
            active_after = actions.read_text(self.driver,
                                             locators.ACCOUNT_ACTIVE_NAME, timeout=4)
            active_after = " ".join(active_after.split()) if active_after else None
            if not active_after or not (active_after.upper() == chosen["id"].upper()
                                        or active_after.upper().startswith(target)):
                raise RuntimeError(f"switch not confirmed: toggle shows "
                                   f"{active_after!r}, wanted {chosen['id']!r}")
            return self._finish(result.succeed({
                "switched_from": active_before, "active": active_after,
                "via": "direct_entry_click"}))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)

    def list_accounts(self, via_click: bool = True) -> ActionResult:
        """Enumerate switchable accounts. NEVER switches.

        via_click=True  - open the menu like a human would, read, close again.
        via_click=False - silent DOM read; menu never becomes visible.
        """
        result = ActionResult(action="accounts.list")
        result.meta["via_click"] = via_click
        try:
            self._precheck(result)

            if via_click:
                result.enter("open_menu")
                opened = self.open_menu()
                if not opened.ok:
                    raise RuntimeError(f"open_menu failed: {opened.error}")
                result.meta["menu_was_open"] = opened.meta.get("menu_was_open")

            result.enter("read_accounts")
            accounts = self._read_menu()
            if not accounts:
                raise RuntimeError("no account entries found in the selector menu")

            result.enter("read_active")
            active = actions.read_text(self.driver, locators.ACCOUNT_ACTIVE_NAME,
                                       timeout=3)
            active = " ".join(active.split()) if active else None

            if via_click:
                # we made it visible (click or veil-lift), so we put it away
                result.enter("close_menu")
                closed = self.close_menu()
                result.meta["menu_open_after"] = (
                    closed.data.get("menu_open") if closed.ok and closed.data
                    else "unknown")
                result.meta["close_method"] = closed.meta.get("method")

            return self._finish(result.succeed({
                "active": active,
                "count": len(accounts),
                "accounts": accounts,
            }))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)
