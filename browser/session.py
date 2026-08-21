"""Browser lifecycle: locate the binary, launch or attach, hand back a driver.

The bot NEVER handles credentials. You log into Tradovate by hand once, in the
window this module opens; the persistent profile keeps that session alive across
runs, and the bot only ever observes whether it is still valid.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Optional

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.edge.options import Options as EdgeOptions

from core import logging_setup

log = logging_setup.get("browser.session")

BROWSERS = {
    "chrome": {
        "label": "Google Chrome",
        "paths": [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ],
        "options_cls": ChromeOptions,
        "driver_cls": webdriver.Chrome,
    },
    "edge": {
        "label": "Microsoft Edge",
        "paths": [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ],
        "options_cls": EdgeOptions,
        "driver_cls": webdriver.Edge,
    },
}

# Without CREATE_BREAKAWAY_FROM_JOB the browser dies with the parent shell.
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


class BrowserLaunchError(RuntimeError):
    pass


class BrowserSession:
    def __init__(self, settings):
        self.cfg = settings
        if self.cfg.browser not in BROWSERS:
            raise ValueError(f"unknown browser {self.cfg.browser!r}")
        self.spec = BROWSERS[self.cfg.browser]
        self.driver: Optional[webdriver.Remote] = None
        self.launched_by_us = False

    # -- discovery ---------------------------------------------------------
    def _binary(self) -> Optional[str]:
        for path in self.spec["paths"]:
            if os.path.isfile(path):
                return path
        return None

    def _port_open(self) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        try:
            return sock.connect_ex((self.cfg.host, self.cfg.port)) == 0
        finally:
            sock.close()

    def cdp_version(self) -> Optional[dict]:
        try:
            url = f"http://{self.cfg.host}:{self.cfg.port}/json/version"
            with urllib.request.urlopen(url, timeout=3) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - purely informational
            return None

    # -- launch ------------------------------------------------------------
    def _launch(self) -> None:
        binary = self._binary()
        if binary is None:
            raise BrowserLaunchError(
                f"{self.spec['label']} not found in: {self.spec['paths']}"
            )

        profile = Path(self.cfg.profile_dir)
        profile.mkdir(parents=True, exist_ok=True)
        log.info("Launching %s", self.spec["label"])
        log.info("  binary : %s", binary)
        log.info("  profile: %s", profile)

        cmd = [
            binary,
            f"--remote-debugging-port={self.cfg.port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--restore-last-session=false",
            "about:blank",
        ]

        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
        try:
            subprocess.Popen(cmd, creationflags=flags, close_fds=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            log.warning("breakaway flag rejected; launching without it")
            subprocess.Popen(cmd,
                             creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                             close_fds=True, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)

        for attempt in range(40):
            if self._port_open():
                log.info("Debug port live after %.1fs", attempt * 0.5)
                self.launched_by_us = True
                return
            time.sleep(0.5)

        raise BrowserLaunchError(
            f"{self.spec['label']} did not open port {self.cfg.port} within 20s"
        )

    # -- connect -----------------------------------------------------------
    def start(self, attach_only: bool = False) -> webdriver.Remote:
        """Attach to a debug browser, launching one first if needed.

        attach_only=True refuses to launch. Read-only tools use this: silently
        starting a fresh (logged-OUT) browser would look like a successful run
        while every probe reported MISS for the wrong reason.
        """
        if self._port_open():
            # An open port is NOT proof of a usable browser. A window that was
            # closed without the process exiting keeps the port listening with
            # ZERO tabs; the driver then fails with an opaque "unable to
            # discover open pages" stacktrace. Catch it here and say so plainly.
            if not self.list_tabs():
                raise BrowserLaunchError(
                    f"STALE browser on {self.cfg.host}:{self.cfg.port} - the port "
                    "is open but there are ZERO tabs (zombie process).\n"
                    "  Clean it up and relaunch:\n"
                    "      python -m tools.open_terminal --force"
                )
            log.info("Debug browser already listening on %s:%s - attaching",
                     self.cfg.host, self.cfg.port)
        elif attach_only:
            raise BrowserLaunchError(
                f"No debug browser on {self.cfg.host}:{self.cfg.port}.\n"
                "  Run script 1 first, and log in:\n"
                "      python -m tools.open_terminal"
            )
        else:
            self._launch()

        version = self.cdp_version()
        if version:
            log.info("Browser: %s (CDP %s)",
                     version.get("Browser"), version.get("Protocol-Version"))

        options = self.spec["options_cls"]()
        options.debugger_address = f"{self.cfg.host}:{self.cfg.port}"
        self.driver = self.spec["driver_cls"](options=options)
        self.driver.set_page_load_timeout(self.cfg.page_load_timeout)

        log.info("Attached. %d tab(s) available.", len(self.driver.window_handles))
        return self.driver

    def stop(self) -> None:
        if self.driver is None:
            return
        if self.cfg.keep_open or not self.launched_by_us:
            log.info("Detaching - browser left open (your session stays alive)")
        else:
            log.info("Closing the browser we launched")
            try:
                self.driver.quit()
            except WebDriverException as exc:
                log.warning("quit() failed: %s", type(exc).__name__)
        self.driver = None

    # -- tab helpers -------------------------------------------------------
    def list_tabs(self) -> list[dict]:
        """Every open tab with its real url and title, WITHOUT switching.

        Reads the CDP /json endpoint directly. Selenium cannot do this - it can
        only report opaque handles, and learning what a handle holds requires
        switching to it, which moves the visible tab. Here we look passively.
        """
        try:
            url = f"http://{self.cfg.host}:{self.cfg.port}/json"
            with urllib.request.urlopen(url, timeout=4) as resp:
                targets = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.debug("CDP tab listing unavailable: %s", exc)
            return []
        return [t for t in targets if t.get("type") == "page"]

    def _handle_for(self, target_id: str) -> Optional[str]:
        """Selenium handle for a CDP target id.

        The format is driver-dependent: Selenium 4.47 + Edge 151 reports the raw
        target id, older builds prefix it with "CDwindow-". Verified empirically
        rather than assumed - probe against the handles the driver reports.
        """
        assert self.driver is not None
        available = set(self.driver.window_handles)
        for candidate in (target_id, f"CDwindow-{target_id}"):
            if candidate in available:
                return candidate
        return None

    def focus_tab(self, needle: str) -> bool:
        """Switch to the first tab whose URL contains `needle`.

        Identifies the tab passively via CDP so no other tab is disturbed, then
        switches straight to it. Falls back to switch-and-check if CDP is
        unavailable (e.g. the endpoint is firewalled).
        """
        assert self.driver is not None
        needle = needle.lower()

        for target in self.list_tabs():
            if needle not in (target.get("url") or "").lower():
                continue
            handle = self._handle_for(target["id"])
            if handle is None:
                log.debug("tab %s is not driver-visible", target.get("url"))
                continue
            try:
                self.driver.switch_to.window(handle)
                log.info("Focused tab: %s", self.driver.current_url)
                return True
            except WebDriverException as exc:
                log.debug("direct switch failed (%s) - falling back",
                          type(exc).__name__)
                break

        # Fallback: walk the handles. This moves the active tab, so restore it
        # if nothing matched.
        log.debug("using switch-and-check fallback")
        try:
            original = self.driver.current_window_handle
        except WebDriverException:
            original = None

        for handle in self.driver.window_handles:
            try:
                self.driver.switch_to.window(handle)
            except WebDriverException:
                continue
            if needle in self.driver.current_url.lower():
                log.info("Focused tab: %s", self.driver.current_url)
                return True

        if original:
            try:
                self.driver.switch_to.window(original)
            except WebDriverException:
                pass
        return False

    def ensure_url(self, url: str, needle: str) -> None:
        """Make sure some tab is on `url`; navigate the current one if not."""
        assert self.driver is not None
        if self.focus_tab(needle):
            return
        log.info("No tab on %s - navigating", needle)
        self.driver.get(url)

    def __enter__(self) -> "BrowserSession":
        self.start()
        return self

    def __exit__(self, *_exc) -> bool:
        self.stop()
        return False
