"""Generic, resilient UI primitives built on the locator-list convention.

Every function here accepts a LIST of candidate selectors and tries them in
order. That is what lets config/locators.py absorb Tradovate UI changes without
any edit to the trading logic.

Selector syntax: "xpath=//div[...]" for XPath, anything else is CSS.
"""

from __future__ import annotations

import time
from typing import Iterable, Optional, Sequence

from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    StaleElementReferenceException,
    WebDriverException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webelement import WebElement

from core import logging_setup

log = logging_setup.get("browser.actions")


class ElementNotFound(RuntimeError):
    """Raised when no candidate selector matched a visible element."""


def parse_selector(selector: str) -> tuple[str, str]:
    if selector.startswith("xpath="):
        return By.XPATH, selector[len("xpath="):]
    return By.CSS_SELECTOR, selector


def _format_all(selectors: Sequence[str], **fmt) -> list[str]:
    return [s.format(**fmt) if fmt else s for s in selectors]


def find(driver_or_element, selectors: Sequence[str], timeout: float = 10,
         visible_only: bool = True, **fmt) -> Optional[WebElement]:
    """First visible element matching any candidate selector, or None."""
    candidates = _format_all(selectors, **fmt)
    deadline = time.time() + timeout

    while True:
        for selector in candidates:
            by, value = parse_selector(selector)
            try:
                for element in driver_or_element.find_elements(by, value):
                    try:
                        if not visible_only or element.is_displayed():
                            log.debug("matched %r", selector)
                            return element
                    except StaleElementReferenceException:
                        continue
            except WebDriverException:
                continue
        if time.time() >= deadline:
            return None
        time.sleep(0.35)


def find_all(driver_or_element, selectors: Sequence[str], **fmt) -> list[WebElement]:
    """Every visible match across all candidate selectors (deduplicated)."""
    found: list[WebElement] = []
    for selector in _format_all(selectors, **fmt):
        by, value = parse_selector(selector)
        try:
            for element in driver_or_element.find_elements(by, value):
                try:
                    if element.is_displayed() and element not in found:
                        found.append(element)
                except StaleElementReferenceException:
                    continue
        except WebDriverException:
            continue
    return found


def require(driver_or_element, selectors: Sequence[str], what: str,
            timeout: float = 10, **fmt) -> WebElement:
    """find() but raises with a useful message instead of returning None."""
    element = find(driver_or_element, selectors, timeout=timeout, **fmt)
    if element is None:
        raise ElementNotFound(
            f"{what}: none of {len(selectors)} selector(s) matched a visible "
            f"element within {timeout}s. Re-run tools/discover_selectors.py."
        )
    return element


def present(driver, selectors: Sequence[str], timeout: float = 2, **fmt) -> bool:
    """Cheap existence probe - used for login/logout state detection."""
    return find(driver, selectors, timeout=timeout, **fmt) is not None


def click(driver, element: WebElement, what: str = "element") -> bool:
    """Click with three escalating strategies.

    Native clicks fail often on Tradovate because overlays and tooltips sit on
    top of controls; the JS fallback goes straight to the element's handler.
    """
    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center', inline:'center'});", element
        )
        time.sleep(0.2)
    except WebDriverException:
        pass

    attempts = (
        ("native", lambda: element.click()),
        ("javascript", lambda: driver.execute_script("arguments[0].click();", element)),
        ("actionchains",
         lambda: ActionChains(driver).move_to_element(element).pause(0.2).click().perform()),
    )

    for name, action in attempts:
        try:
            action()
            log.debug("clicked %s via %s", what, name)
            return True
        except (ElementClickInterceptedException, ElementNotInteractableException,
                StaleElementReferenceException, WebDriverException) as exc:
            log.debug("%s click on %s failed: %s", name, what, type(exc).__name__)

    log.error("all click strategies failed for %s", what)
    return False


def click_first(driver, selectors: Sequence[str], what: str,
                timeout: float = 10, **fmt) -> bool:
    element = find(driver, selectors, timeout=timeout, **fmt)
    if element is None:
        log.warning("%s: no matching element to click", what)
        return False
    return click(driver, element, what)


def type_into(driver, element: WebElement, text: str, clear: bool = True) -> bool:
    """Set an input's value. Falls back to a JS set + input event for React."""
    try:
        if clear:
            element.send_keys(Keys.CONTROL, "a")
            element.send_keys(Keys.DELETE)
        element.send_keys(text)
        return True
    except WebDriverException as exc:
        log.debug("send_keys failed (%s), trying JS value set", type(exc).__name__)

    # React ignores a raw .value assignment; the native setter + input event is
    # the standard workaround so the component state actually updates.
    script = """
        const el = arguments[0], value = arguments[1];
        const setter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value').set;
        setter.call(el, value);
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
    """
    try:
        driver.execute_script(script, element, text)
        return True
    except WebDriverException as exc:
        log.error("could not set value: %s", exc)
        return False


def read_text(driver, selectors: Sequence[str], timeout: float = 5, **fmt) -> Optional[str]:
    element = find(driver, selectors, timeout=timeout, **fmt)
    if element is None:
        return None
    try:
        return (element.text or "").strip()
    except WebDriverException:
        return None


def dismiss_any(driver, selectors: Sequence[str], what: str = "dialog") -> bool:
    """Best-effort close. Never raises."""
    element = find(driver, selectors, timeout=1.5)
    if element is None:
        return False
    log.info("dismissing %s", what)
    return click(driver, element, what)
