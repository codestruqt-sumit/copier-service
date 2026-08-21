"""(c) symbol switching.

Switching is cached: re-selecting a symbol that is already active is a no-op,
because every avoidable UI interaction is an avoidable failure.
"""

from __future__ import annotations

import time
from typing import Optional

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.keys import Keys

from browser import actions
from browser.actions import ElementNotFound
from config import locators
from core import logging_setup

log = logging_setup.get("trading.symbols")


class SymbolSwitcher:
    def __init__(self, driver, settings):
        self.driver = driver
        self.cfg = settings
        self._active: Optional[str] = None

    def active_symbol(self, refresh: bool = False) -> Optional[str]:
        if self._active and not refresh:
            return self._active
        text = actions.read_text(self.driver, locators.ACTIVE_SYMBOL_LABEL, timeout=3)
        if text:
            self._active = text.strip().upper()
        return self._active

    def switch_to(self, symbol: str, verify: bool = True) -> bool:
        symbol = symbol.strip().upper()

        current = self.active_symbol()
        if current and symbol in current:
            log.info("symbol %s already active - no switch needed", symbol)
            return True

        log.info("switching symbol: %s -> %s", current or "?", symbol)

        try:
            search = actions.require(self.driver, locators.SYMBOL_SEARCH_INPUT,
                                     "symbol search input",
                                     timeout=self.cfg.default_wait)
        except ElementNotFound as exc:
            log.error("%s", exc)
            return False

        if not actions.click(self.driver, search, "symbol search"):
            return False
        if not actions.type_into(self.driver, search, symbol):
            return False

        time.sleep(0.8)          # let the suggestion list populate

        result = actions.find(self.driver, locators.SYMBOL_SEARCH_RESULT,
                              timeout=5, symbol=symbol)
        if result is not None:
            actions.click(self.driver, result, f"search result {symbol}")
        else:
            log.debug("no suggestion matched %s - falling back to ENTER", symbol)
            try:
                search.send_keys(Keys.ENTER)
            except WebDriverException as exc:
                log.error("could not submit symbol search: %s", type(exc).__name__)
                return False

        time.sleep(1.2)          # chart/DOM reload

        if not verify:
            self._active = symbol
            return True

        confirmed = self.active_symbol(refresh=True)
        if confirmed and symbol in confirmed:
            log.info("symbol switch confirmed: %s", confirmed)
            return True

        log.error("symbol switch NOT confirmed - wanted %s, UI shows %r",
                  symbol, confirmed)
        self._active = None
        return False
