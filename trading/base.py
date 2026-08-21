"""Shared base for every terminal module.

One place for the plumbing every action needs: precheck, screenshots on
failure, the notifier hand-off, human pacing, and stack scoping. Modules
subclass this so behaviour (and safety) stays uniform, and the engine can
treat them all identically later.

CONTRACT every public action honours:
  * returns an ActionResult, NEVER raises to the caller
  * records the stage it reached, so a failure alert can be specific
  * captures a screenshot on failure
  * routes the result through the notifier (console now; Telegram/dashboard
    later) without the action caring which
  * paces itself with small human-like pauses (rate-limit friendly)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from selenium.common.exceptions import WebDriverException

from browser import actions, humanize
from config import locators
from config.settings import LOG_DIR
from core import logging_setup
from core.notify import Notifier
from core.results import ActionResult


class TerminalModule:
    """Base for AccountPanel, WorkspaceTabs, ChartTradePanel, OrderTicket, ..."""

    log = logging_setup.get("trading.base")

    def __init__(self, driver, notifier: Optional[Notifier] = None):
        self.driver = driver
        self.notifier = notifier
        self.failure_dir = Path(LOG_DIR) / "failures"

    # -- pacing --------------------------------------------------------------
    # Deliberately small: enough to look human and stay under activity
    # monitors, not enough to hurt throughput.
    def tick(self, lo: float = 0.15, hi: float = 0.45) -> None:
        humanize.pause(lo, hi)

    def settle(self, lo: float = 0.4, hi: float = 0.9) -> None:
        humanize.settle(lo, hi)

    # -- plumbing ------------------------------------------------------------
    def _screenshot(self, tag: str) -> Optional[str]:
        try:
            self.failure_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            name = f"{stamp}-{type(self).__name__}-{tag}.png"
            path = self.failure_dir / name
            self.driver.save_screenshot(str(path))
            return str(path)
        except WebDriverException:
            return None

    def _finish(self, result: ActionResult) -> ActionResult:
        if self.notifier is not None:
            try:
                self.notifier.send(result)
            except Exception:  # noqa: BLE001 - notifier must never break an action
                self.log.warning("notifier failed for %s", result.action)
        return result

    def _fail(self, result: ActionResult, message: str,
              exc: Optional[BaseException] = None) -> ActionResult:
        shot = self._screenshot(result.stage)
        self.log.error("%s failed at stage '%s': %s",
                       result.action, result.stage, message or exc)
        return self._finish(result.fail(message, exc=exc, screenshot=shot))

    def _precheck(self, result: ActionResult) -> None:
        """Confirm we are on the logged-in terminal. Raises if not."""
        result.enter("precheck")
        url = self.driver.current_url
        if "trader.tradovate.com" not in url:
            raise RuntimeError(f"not on the terminal (url={url})")
        if "/welcome" in url:
            raise RuntimeError("on the LOGIN screen - session is logged out")

    # -- safety --------------------------------------------------------------
    def assert_safe_to_click(self, text: str) -> None:
        """Refuse to click anything on the never-click denylist."""
        cleaned = " ".join((text or "").split())
        for forbidden in locators.FORBIDDEN_CLICK_TEXTS:
            if forbidden.lower() in cleaned.lower():
                raise PermissionError(
                    f"refusing to click {cleaned!r} - denylist match {forbidden!r}")

    # -- confirmation / error dialogs ----------------------------------------
    # Tradovate raises a '.popover-content' after order actions. Two kinds:
    #   confirmation -> 'Buy 1 MKT?' [Buy|Sell]+Cancel, or Exit's Yes/No
    #   error        -> 'Symbol should be specified' etc. with an OK button
    _AFFIRMATIVE = ("buy", "sell", "yes", "confirm", "ok", "place", "submit")
    _NEGATIVE = ("cancel", "no")
    _ERROR_HINTS = ("should be", "must be", "invalid", "not enough", "rejected",
                    "cannot", "required", "specified", "insufficient", "error")

    def _popover_buttons(self, popover):
        out = []
        for el in actions.find_all(
                popover,
                ["xpath=.//div[contains(@class,'btn')]", "xpath=.//button",
                 "xpath=.//label[contains(@class,'btn')]",
                 "xpath=.//*[@role='button']"]):
            try:
                out.append((el, " ".join((el.text or "").split())))
            except Exception:  # noqa: BLE001
                out.append((el, ""))
        return out

    def handle_confirm_dialog(self, affirmatives=None, verify_substrings=None,
                              timeout: float = 4.0):
        """Poll for a popover and resolve it. Returns (status, text) where
        status is 'none' | 'confirmed' | 'error'.

        - error popover (text matches _ERROR_HINTS): dismiss via OK, status
          'error' - caller should treat as failure.
        - otherwise: verify substrings if given (cancel+raise on mismatch),
          click the affirmative button, status 'confirmed'.
        """
        import time
        from browser import humanize
        popover = None
        for _ in range(int(timeout / 0.3) + 1):
            popover = actions.find(self.driver, locators.ORDER_CONFIRM_POPOVER,
                                   timeout=0.3, visible_only=True)
            if popover is not None:
                break
            time.sleep(0.3)
        if popover is None:
            return ("none", "")

        # CRITICAL: the message (e.g. 'Symbol should be specified') lives in the
        # popover's HOST, OUTSIDE .popover-content - reading only .popover-content
        # yields just 'OK' and mis-classifies errors as confirmations. Read the
        # host text for classification.
        try:
            text = " ".join((self.driver.execute_script(
                "var p=arguments[0];var h=p.closest('[class*=popover]')||p.parentElement||p;"
                "return h.textContent;", popover) or "").split())
        except Exception:  # noqa: BLE001
            try:
                text = " ".join((popover.text or "").split())
            except Exception:  # noqa: BLE001
                text = ""
        buttons = self._popover_buttons(popover)

        # error dialog -> dismiss and report
        if any(h in text.lower() for h in self._ERROR_HINTS):
            for el, t in buttons:
                if t.strip().lower() in ("ok", "close", "dismiss"):
                    humanize.human_click(self.driver, el, "error OK")
                    break
            return ("error", text)

        if verify_substrings:
            for sub in verify_substrings:
                if sub and sub.lower() not in text.lower():
                    for el, t in buttons:
                        if t.strip().lower() in self._NEGATIVE:
                            humanize.human_click(self.driver, el, "Cancel")
                            break
                    raise RuntimeError(f"confirm mismatch: {sub!r} not in {text!r}")

        wanted = [w.lower() for w in (affirmatives or self._AFFIRMATIVE)]
        submit = None
        for want in wanted:
            for el, t in buttons:
                if t.strip().lower() == want:
                    submit = el
                    break
            if submit is not None:
                break
        if submit is None:
            for el, t in buttons:
                if t.strip() and t.strip().lower() not in self._NEGATIVE:
                    submit = el
                    break
        if submit is None:
            raise RuntimeError(f"popover present ({[t for _, t in buttons]}) "
                               "but no affirmative button")
        humanize.human_click(self.driver, submit, "confirm")
        self.settle(0.6, 1.0)
        return ("confirmed", text)

    # -- trusted input (CDP) -------------------------------------------------
    # LEARNING 2026-08-18: Tradovate's React comboboxes (Order Ticket qty /
    # order-type dropdowns) reject synthetic JS/elementFromPoint clicks
    # (isTrusted=false) but ACCEPT CDP Input.dispatchMouseEvent, which the
    # browser reports as trusted. CDP coords are CSS pixels == getBoundingClientRect.
    def cdp_click(self, x: float, y: float) -> None:
        self.driver.execute_cdp_cmd("Input.dispatchMouseEvent",
                                    {"type": "mouseMoved", "x": float(x),
                                     "y": float(y), "buttons": 0})
        for kind in ("mousePressed", "mouseReleased"):
            self.driver.execute_cdp_cmd("Input.dispatchMouseEvent",
                                        {"type": kind, "x": float(x), "y": float(y),
                                         "button": "left", "buttons": 1,
                                         "clickCount": 1})

    def cdp_click_element(self, element) -> None:
        xy = self.driver.execute_script(
            "var b=arguments[0].getBoundingClientRect();"
            "return [b.x+b.width/2, b.y+b.height/2];", element)
        self.cdp_click(xy[0], xy[1])

    # -- scoping -------------------------------------------------------------
    def find_click(self, selectors, what, timeout: float = 6, scope=None,
                   **fmt):
        """Locate (optionally within `scope`), denylist-check, human-click."""
        root = scope if scope is not None else self.driver
        element = actions.find(root, selectors, timeout=timeout, **fmt)
        if element is None:
            raise RuntimeError(f"{what}: not found")
        try:
            self.assert_safe_to_click(element.text)
        except WebDriverException:
            pass   # unreadable text -> not a denylist string
        if not humanize.human_click(self.driver, element, what):
            raise RuntimeError(f"{what}: click failed")
        return element
