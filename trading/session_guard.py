"""(a) keep-alive  +  (b) login/logout monitoring.

Both concerns live together because they share one question: "is this session
still usable?" The guard answers it, nudges the UI to keep it that way, and
raises a notification the moment the answer changes.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from selenium.common.exceptions import WebDriverException

from browser import actions
from config import locators
from core import logging_setup
from core.models import SessionStatus, utcnow

log = logging_setup.get("trading.session")

NotifyFn = Callable[[str, str], None]        # (level, message)


class SessionGuard:
    def __init__(self, driver, settings, notify: Optional[NotifyFn] = None):
        self.driver = driver
        self.cfg = settings
        self.notify = notify or (lambda level, msg: None)

        self._last_keepalive = 0.0
        self._last_check = 0.0
        self._consecutive_logouts = 0
        self._last_status: Optional[SessionStatus] = None

    # -- (b) login state ---------------------------------------------------
    def check_status(self, force: bool = False) -> SessionStatus:
        """Determine whether we still have an authenticated session.

        Logged-OUT markers are checked first and treated as authoritative: a
        password field on screen is unambiguous, whereas 'logged in' markers can
        linger in the DOM behind a session-expired overlay.
        """
        now = time.time()
        if not force and (now - self._last_check) < self.cfg.login_check_interval_sec:
            return self._last_status or SessionStatus(True, "not yet checked")

        self._last_check = now

        try:
            if actions.present(self.driver, locators.SESSION_TIMEOUT_DIALOG, timeout=0.5):
                status = SessionStatus(False, "session-timeout dialog visible")
            elif actions.present(self.driver, locators.LOGGED_OUT_MARKERS, timeout=0.5):
                status = SessionStatus(False, "login screen detected")
            elif actions.present(self.driver, locators.LOGGED_IN_MARKERS, timeout=1.0):
                status = SessionStatus(True, "trading UI present")
            else:
                # neither set matched - inconclusive, not proof of logout
                status = SessionStatus(True, "inconclusive (no markers matched)")
        except WebDriverException as exc:
            status = SessionStatus(False, f"browser unreachable: {type(exc).__name__}")

        self._transition(status)
        return status

    def _transition(self, status: SessionStatus) -> None:
        """Debounce and emit notifications only on genuine state changes."""
        previous = self._last_status

        if not status.logged_in:
            self._consecutive_logouts += 1
            if self._consecutive_logouts < self.cfg.logout_confirm_checks:
                log.debug("possible logout (%d/%d): %s", self._consecutive_logouts,
                          self.cfg.logout_confirm_checks, status.detail)
                return                      # not confirmed yet - hold the old status
        else:
            if self._consecutive_logouts:
                log.debug("logout suspicion cleared")
            self._consecutive_logouts = 0

        changed = previous is None or previous.logged_in != status.logged_in
        self._last_status = status

        if not changed:
            return

        if status.logged_in:
            log.info("SESSION ACTIVE - %s", status.detail)
            self.notify("info", f"Tradovate session is active ({status.detail})")
        else:
            log.error("SESSION LOST - %s", status.detail)
            if self.cfg.notify_on_logout:
                self.notify(
                    "critical",
                    f"Tradovate session LOST at {utcnow():%H:%M:%S} UTC - "
                    f"{status.detail}. Log back in manually; trading is paused."
                )

    @property
    def is_logged_in(self) -> bool:
        return self._last_status is None or self._last_status.logged_in

    def wait_for_login(self, timeout_sec: int = 600, poll: int = 5) -> bool:
        """Block until a human logs in. Used at startup and after a logout."""
        log.warning("Waiting up to %ds for manual login in the browser window...",
                    timeout_sec)
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if self.check_status(force=True).logged_in:
                log.info("Login detected - resuming")
                return True
            time.sleep(poll)
        log.error("No login within %ds", timeout_sec)
        return False

    # -- (a) keep-alive ----------------------------------------------------
    def keepalive(self) -> bool:
        """Periodically nudge the UI so idle-timeout never fires.

        Deliberately harmless: a mouse move over a neutral region plus a
        scroll-by-zero. It never clicks a control that could submit anything.
        """
        now = time.time()
        if (now - self._last_keepalive) < self.cfg.keepalive_interval_sec:
            return False

        self._last_keepalive = now

        try:
            actions.dismiss_any(self.driver, locators.SESSION_TIMEOUT_DIALOG,
                                "session-timeout dialog")

            element = actions.find(self.driver, locators.KEEPALIVE_TARGETS, timeout=2)
            if element is not None:
                # move + zero-scroll: registers activity, mutates nothing
                self.driver.execute_script(
                    "arguments[0].dispatchEvent(new MouseEvent('mousemove',"
                    "{bubbles:true, clientX:5, clientY:5}));", element)
            self.driver.execute_script("window.scrollBy(0, 0);")
            log.debug("keep-alive nudge sent")
            return True
        except WebDriverException as exc:
            log.warning("keep-alive failed: %s", type(exc).__name__)
            return False
