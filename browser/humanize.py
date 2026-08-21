"""Human-like pacing for UI interactions.

Not stealth - just natural rhythm: no two actions land back-to-back at
machine speed, clicks are preceded by a cursor move onto the element, and
UI-changing actions get a settle pause afterwards so the app can react.
"""

from __future__ import annotations

import random
import time

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.action_chains import ActionChains

from browser import actions
from core import logging_setup

log = logging_setup.get("browser.humanize")


def pause(lo: float = 0.12, hi: float = 0.40) -> None:
    """Short think-pause before an interaction."""
    time.sleep(random.uniform(lo, hi))


def settle(lo: float = 0.35, hi: float = 0.90) -> None:
    """Longer pause after an interaction that changes the UI."""
    time.sleep(random.uniform(lo, hi))


def human_click(driver, element, what: str = "element") -> bool:
    """Move onto the element, pause like a human, then click.

    Falls back to the standard 3-strategy click if the ActionChains path
    fails (overlays, stale elements, ...). Returns True on any success.
    """
    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center', inline:'center'});",
            element)
    except WebDriverException:
        pass

    pause()
    try:
        (ActionChains(driver)
            .move_to_element(element)
            .pause(random.uniform(0.08, 0.25))
            .click()
            .perform())
        log.debug("human click on %s", what)
        return True
    except WebDriverException as exc:
        log.debug("human click on %s failed (%s) - falling back",
                  what, type(exc).__name__)
        return actions.click(driver, element, what)
