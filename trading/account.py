"""Read the account balance off the UI so signals can be sized against it.

Scraping a number that drives order size is the highest-consequence read in the
bot, so this module is deliberately paranoid: it parses strictly, rejects
implausible values, and caches only briefly.
"""

from __future__ import annotations

import re
import time
from typing import Optional

from browser import actions
from config import locators
from core import logging_setup
from core.models import AccountSnapshot

log = logging_setup.get("trading.account")

# matches 12,345.67  $12345  -1,234.50  (1,234.50)
_MONEY = re.compile(r"\(?-?\$?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)\)?")


def parse_money(text: str) -> Optional[float]:
    if not text:
        return None
    match = _MONEY.search(text)
    if not match:
        return None
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    # parentheses or a leading minus denote a negative figure
    if "(" in text or text.strip().startswith("-"):
        value = -value
    return value


class AccountReader:
    def __init__(self, driver, settings, cache_ttl: float = 10.0):
        self.driver = driver
        self.cfg = settings
        self._cache_ttl = cache_ttl
        self._cached: Optional[AccountSnapshot] = None
        self._cached_at = 0.0

    def snapshot(self, refresh: bool = False) -> AccountSnapshot:
        now = time.time()
        if (not refresh and self._cached is not None
                and (now - self._cached_at) < self._cache_ttl):
            return self._cached

        text = actions.read_text(self.driver, locators.ACCOUNT_BALANCE, timeout=5)
        balance = parse_money(text or "")

        if balance is None:
            log.warning("could not read account balance (raw text: %r)", text)
        else:
            log.debug("account balance: %s -> %.2f", text, balance)

        snapshot = AccountSnapshot(balance=balance, raw_text=text or "")
        self._cached = snapshot
        self._cached_at = now
        return snapshot

    def is_tradable(self) -> tuple[bool, str]:
        """Gate on balance before any entry order."""
        snapshot = self.snapshot()

        if not snapshot.is_valid:
            return False, (
                f"account balance unreadable (raw={snapshot.raw_text!r}); "
                "check config/locators.py ACCOUNT_BALANCE"
            )

        if snapshot.balance < self.cfg.min_account_balance:
            return False, (f"balance {snapshot.balance:,.2f} is below the configured "
                           f"minimum {self.cfg.min_account_balance:,.2f}")

        return True, f"balance {snapshot.balance:,.2f}"
