"""(d) place a trade with quantity   +   (e) close a position for a symbol.

Every method takes `dry_run`. In dry-run the module walks the entire UI path -
locating the qty box, the side button, the confirm dialog - and stops at the
final click. That way a rehearsal exercises the same selectors a live order
would, and a missing selector surfaces before real money is involved.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from selenium.common.exceptions import WebDriverException

from browser import actions
from browser.actions import ElementNotFound
from config import locators
from core import logging_setup
from core.models import Direction

log = logging_setup.get("trading.orders")


@dataclass
class OrderOutcome:
    ok: bool
    message: str
    dry_run: bool = True

    def __bool__(self) -> bool:
        return self.ok


class OrderManager:
    def __init__(self, driver, settings):
        self.driver = driver
        self.cfg = settings

    # -- (d) entry ---------------------------------------------------------
    def place_market_order(self, symbol: str, direction: Direction, qty: int,
                           dry_run: bool = True) -> OrderOutcome:
        if direction is Direction.FLAT:
            raise ValueError("use close_position() for FLAT signals")

        if qty < 1:
            return OrderOutcome(False, f"refusing qty={qty}", dry_run)

        if qty > self.cfg.max_qty_per_order:
            return OrderOutcome(
                False,
                f"qty {qty} exceeds max_qty_per_order {self.cfg.max_qty_per_order}",
                dry_run,
            )

        side = "BUY" if direction is Direction.LONG else "SELL"
        log.info("%s %d %s (dry_run=%s)", side, qty, symbol, dry_run)

        # 1. quantity
        try:
            qty_input = actions.require(self.driver, locators.QTY_INPUT,
                                        "order quantity input", timeout=8)
        except ElementNotFound as exc:
            return OrderOutcome(False, str(exc), dry_run)

        if not actions.type_into(self.driver, qty_input, str(qty)):
            return OrderOutcome(False, "could not set quantity", dry_run)

        entered = (qty_input.get_attribute("value") or "").strip()
        if entered != str(qty):
            return OrderOutcome(
                False, f"quantity verification failed: wanted {qty}, box shows {entered!r}",
                dry_run,
            )
        log.debug("quantity box confirmed at %s", entered)

        # 2. side button
        side_locator = (locators.BUY_BUTTON if direction is Direction.LONG
                        else locators.SELL_BUTTON)
        side_button = actions.find(self.driver, side_locator, timeout=8)
        if side_button is None:
            return OrderOutcome(False, f"{side} button not found", dry_run)

        # ---- the point of no return -------------------------------------
        if dry_run:
            log.warning("DRY RUN - would now click %s for %d %s. No order sent.",
                        side, qty, symbol)
            return OrderOutcome(True, f"dry-run: {side} {qty} {symbol} validated", True)

        if not actions.click(self.driver, side_button, f"{side} button"):
            return OrderOutcome(False, f"could not click {side}", False)

        # 3. confirmation dialog, if the account has one enabled
        confirm = actions.find(self.driver, locators.ORDER_CONFIRM_BUTTON, timeout=3)
        if confirm is not None:
            log.info("confirmation dialog present - confirming")
            if not actions.click(self.driver, confirm, "order confirm"):
                return OrderOutcome(False, "could not click confirm", False)
        else:
            log.debug("no confirmation dialog appeared")

        time.sleep(1.0)

        # 4. look for a rejection toast
        rejection = actions.read_text(self.driver, locators.ORDER_REJECT_TOAST, timeout=2)
        if rejection:
            return OrderOutcome(False, f"broker rejected order: {rejection}", False)

        return OrderOutcome(True, f"{side} {qty} {symbol} submitted", False)

    # -- (e) exit ----------------------------------------------------------
    def close_position(self, symbol: str, dry_run: bool = True) -> OrderOutcome:
        log.info("closing position on %s (dry_run=%s)", symbol, dry_run)

        actions.click_first(self.driver, locators.POSITIONS_TAB, "Positions tab", timeout=3)
        time.sleep(0.5)

        row = actions.find(self.driver, locators.POSITION_ROW, timeout=6, symbol=symbol)
        if row is None:
            # not an error: flat is the desired end state either way
            log.info("no open position row for %s - already flat", symbol)
            return OrderOutcome(True, f"no open position on {symbol}", dry_run)

        close_button = actions.find(row, locators.CLOSE_POSITION_BUTTON, timeout=4)
        if close_button is None:
            return OrderOutcome(
                False, f"found {symbol} position row but no Close control", dry_run
            )

        if dry_run:
            log.warning("DRY RUN - would now close the %s position. Nothing sent.", symbol)
            return OrderOutcome(True, f"dry-run: close {symbol} validated", True)

        if not actions.click(self.driver, close_button, f"close {symbol}"):
            return OrderOutcome(False, f"could not click close for {symbol}", False)

        confirm = actions.find(self.driver, locators.CLOSE_CONFIRM_BUTTON, timeout=3)
        if confirm is not None:
            actions.click(self.driver, confirm, "close confirm")

        time.sleep(1.0)

        still_open = actions.find(self.driver, locators.POSITION_ROW, timeout=3, symbol=symbol)
        if still_open is not None:
            return OrderOutcome(
                False, f"close submitted but {symbol} row is still present", False
            )

        return OrderOutcome(True, f"{symbol} position closed", False)

    # -- diagnostics -------------------------------------------------------
    def has_position(self, symbol: str) -> Optional[bool]:
        """True/False, or None if the positions UI could not be read."""
        try:
            actions.click_first(self.driver, locators.POSITIONS_TAB, "Positions tab",
                                timeout=2)
            row = actions.find(self.driver, locators.POSITION_ROW, timeout=3, symbol=symbol)
            return row is not None
        except WebDriverException:
            return None
