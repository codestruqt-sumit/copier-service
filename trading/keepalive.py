"""Harmless activity to keep the session alive without tripping anything.

Registers genuine but side-effect-free interaction: a cursor move over a
neutral element, a read of the equity value, and an optional click on the
top-bar clock (confirmed inert). NEVER touches an order control - the DOM
ladder's one-click Buy/Sell live nearby, so keepalive stays well clear.
"""

from __future__ import annotations

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.action_chains import ActionChains

from browser import actions, humanize
from config import locators
from core.results import ActionResult
from trading.base import TerminalModule


class KeepAlive(TerminalModule):

    def nudge(self, do_click: bool = False) -> ActionResult:
        """One keep-alive beat. do_click also clicks the inert clock."""
        result = ActionResult(action="keepalive.nudge")
        result.meta["do_click"] = do_click
        try:
            self._precheck(result)

            result.enter("mouse_move")
            neutral = actions.find(self.driver, locators.NEUTRAL_CLICK_TARGET,
                                   timeout=3)
            if neutral is not None:
                try:
                    (ActionChains(self.driver)
                        .move_to_element(neutral)
                        .pause(0.2).perform())
                except WebDriverException:
                    pass

            result.enter("read_equity")
            equity = actions.read_text(self.driver, locators.ACCOUNT_BALANCE,
                                       timeout=3)

            if do_click and neutral is not None:
                result.enter("neutral_click")
                self.assert_safe_to_click("clock")   # trivially safe
                humanize.human_click(self.driver, neutral, "neutral clock")

            self.tick()
            return self._finish(result.succeed({"equity_seen": equity,
                                                "clicked": do_click}))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)
