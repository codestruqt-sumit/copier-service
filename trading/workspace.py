"""Workspace (golden-layout) tabs: list stacks, switch the chart symbol tab.

=============================================================================
 LEARNINGS (live probe 2026-08-18, logged-in terminal)
=============================================================================
 * The workspace is golden-layout. Tab strips are `ul.lm_tabs`, one per
   stack; tabs are `li.lm_tab` with the visible text in `span.lm_title`;
   the active tab carries class `lm_active`.
 * Observed stacks: 0=charts ('MNQU6 1m','MGCZ6 1m','SIU6 1m'), 1=Accounts,
   2=Positions, 3=DOM ladder ('MNQU6','SIU6'), 4=Orders, 5=Order Ticket.
   Chart-stack titles carry a timeframe suffix ('MNQU6 1m'); the symbol is
   the FIRST whitespace token.
 * DANGER: every stack ends with an EMPTY-TITLED `li.lm_tab.tab_add` - the
   "+" button that CREATES a new tab. Never click a tab with class tab_add
   or an empty title. Close buttons (lm_close_tab) exist but render 0-width
   here; we click the lm_title span, never the li edges, to stay clear.
 * The pane content of a symbol stack exposes the CURRENT symbol as
   `div.contract-symbol` (e.g. 'MNQU6') - that is the verification signal
   after a switch, polled because content swaps asynchronously.
 * Clicking a chart tab switches the whole pane (chart follows the tab).
   The DOM stack is independent - switching the chart does NOT switch the
   DOM ladder. scope='all' switches every symbol stack that has the tab.
=============================================================================
"""

from __future__ import annotations

import re
import time
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

log = logging_setup.get("trading.workspace")

# futures contract token: root + month code + year digit(s), e.g. MNQU6, SIU6
_CONTRACT = re.compile(r"^[A-Z]{1,4}[FGHJKMNQUVXZ]\d{1,2}$")

_JS_STACKS = """
var stacks = [].slice.call(document.querySelectorAll('.lm_tabs'));
return stacks.map(function (ul, si) {
  var stackEl = ul.closest('.lm_stack') || ul.parentElement;
  var contract = stackEl ? stackEl.querySelector('.contract-symbol') : null;
  var r = ul.getBoundingClientRect();
  return {
    index: si,
    x: Math.round(r.x), y: Math.round(r.y),
    contract: contract ? (contract.textContent || '').trim() : null,
    tabs: [].slice.call(ul.querySelectorAll('.lm_tab')).map(function (li, ti) {
      var t = li.querySelector('.lm_title');
      return {
        pos: ti,
        title: t ? (t.textContent || '').trim() : '',
        active: li.className.indexOf('lm_active') !== -1,
        is_add: li.className.indexOf('tab_add') !== -1
      };
    })
  };
});
"""


def _token(title: str) -> str:
    return title.split()[0].upper() if title.strip() else ""


class WorkspaceTabs:
    """List and switch golden-layout tabs. Never touches tab_add or close."""

    def __init__(self, driver, notifier: Optional[Notifier] = None):
        self.driver = driver
        self.notifier = notifier
        self.failure_dir = Path(LOG_DIR) / "failures"

    # -- plumbing (same contract as AccountPanel) -----------------------------
    def _screenshot(self, tag: str) -> Optional[str]:
        try:
            self.failure_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = self.failure_dir / f"{stamp}-workspace-{tag}.png"
            self.driver.save_screenshot(str(path))
            return str(path)
        except WebDriverException:
            return None

    def _finish(self, result: ActionResult) -> ActionResult:
        if self.notifier is not None:
            try:
                self.notifier.send(result)
            except Exception:  # noqa: BLE001
                log.warning("notifier failed for %s", result.action)
        return result

    def _fail(self, result: ActionResult, message: str,
              exc: Optional[BaseException] = None) -> ActionResult:
        shot = self._screenshot(result.stage)
        log.error("%s failed at %s: %s", result.action, result.stage,
                  message or exc)
        return self._finish(result.fail(message, exc=exc, screenshot=shot))

    def _precheck(self, result: ActionResult) -> None:
        result.enter("precheck")
        url = self.driver.current_url
        if "trader.tradovate.com" not in url or "/welcome" in url:
            raise RuntimeError(f"not on the logged-in terminal (url={url})")

    def _snapshot(self) -> list[dict]:
        stacks = self.driver.execute_script(_JS_STACKS) or []
        for stack in stacks:
            for tab in stack["tabs"]:
                tab["token"] = _token(tab["title"])
            stack["is_symbol_stack"] = any(
                _CONTRACT.match(t["token"]) for t in stack["tabs"] if t["token"])
        return stacks

    # -- public: read ------------------------------------------------------------
    def list_stacks(self) -> ActionResult:
        """Read-only: every stack, every tab, which is active."""
        result = ActionResult(action="tabs.list")
        try:
            self._precheck(result)
            result.enter("read_stacks")
            stacks = self._snapshot()
            if not stacks:
                raise RuntimeError("no lm_tabs stacks found - layout changed?")
            return self._finish(result.succeed({
                "stacks": stacks,
                "symbol_stacks": [s["index"] for s in stacks if s["is_symbol_stack"]],
            }))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)

    # -- public: switch ------------------------------------------------------------
    def switch_to(self, symbol: str, scope: str = "chart") -> ActionResult:
        """Switch the workspace tab(s) whose title token matches `symbol`.

        scope='chart' - first symbol stack only (the chart follows its tab)
        scope='all'   - every symbol stack that has a matching tab
        Not found -> FAILED result whose data lists what IS available.
        """
        result = ActionResult(action="tabs.switch")
        target = (symbol or "").strip().upper()
        result.meta["target"] = target
        result.meta["scope"] = scope
        try:
            if not target:
                raise ValueError("no target symbol given")
            self._precheck(result)

            result.enter("read_stacks")
            stacks = self._snapshot()
            symbol_stacks = [s for s in stacks if s["is_symbol_stack"]]
            if not symbol_stacks:
                raise RuntimeError("no symbol stacks found in the workspace")

            result.enter("match")
            matches = []          # (stack, tab)
            for stack in symbol_stacks:
                exact = [t for t in stack["tabs"]
                         if not t["is_add"] and t["token"] == target]
                prefix = [t for t in stack["tabs"]
                          if not t["is_add"] and t["token"].startswith(target)]
                if exact:
                    matches.append((stack, exact[0]))
                elif len(prefix) == 1:
                    matches.append((stack, prefix[0]))
                elif len(prefix) > 1:
                    raise LookupError(
                        f"'{target}' is ambiguous in stack {stack['index']}: "
                        + ", ".join(t["title"] for t in prefix))

            if not matches:
                available = {
                    f"stack{s['index']}": [t["title"] for t in s["tabs"]
                                           if t["title"]]
                    for s in symbol_stacks}
                result.data = {"available": available}
                raise LookupError(
                    f"symbol tab '{target}' not found. Available: {available}")

            if scope == "chart":
                matches = matches[:1]

            result.enter("click_tabs")
            switched = []
            for stack, tab in matches:
                if tab["active"]:
                    switched.append({"stack": stack["index"], "tab": tab["title"],
                                     "already_active": True})
                    continue
                # safety: refuse empty titles / tab_add / denylist text
                if tab["is_add"] or not tab["title"]:
                    raise PermissionError("refusing to click an add-tab button")
                for forbidden in locators.FORBIDDEN_CLICK_TEXTS:
                    if forbidden.lower() in tab["title"].lower():
                        raise PermissionError(
                            f"tab title {tab['title']!r} matches denylist")

                xpath = (f"xpath=(//ul[contains(@class,'lm_tabs')])"
                         f"[{stack['index'] + 1}]//li[contains(@class,'lm_tab')]"
                         f"[{tab['pos'] + 1}]//span[contains(@class,'lm_title')]")
                element = actions.find(self.driver, [xpath], timeout=4)
                if element is None:
                    raise RuntimeError(f"tab element vanished: {tab['title']!r}")
                if not humanize.human_click(self.driver, element,
                                            f"tab {tab['title']!r}"):
                    raise RuntimeError(f"could not click tab {tab['title']!r}")
                humanize.settle()
                switched.append({"stack": stack["index"], "tab": tab["title"],
                                 "already_active": False})

            result.enter("verify")
            deadline = time.time() + 5
            verified = {}
            while time.time() < deadline:
                snap = {s["index"]: s for s in self._snapshot()}
                verified = {}
                for stack, tab in matches:
                    now = snap.get(stack["index"], {})
                    active_ok = any(t["token"] == _token(tab["title"]) and t["active"]
                                    for t in now.get("tabs", []))
                    contract = (now.get("contract") or "").upper()
                    # symbol stacks MUST confirm via the pane's own contract
                    # header - tab class alone is not proof the content swapped
                    contract_ok = contract == _token(tab["title"])
                    verified[stack["index"]] = {"tab_active": active_ok,
                                                "contract": now.get("contract"),
                                                "contract_ok": contract_ok}
                if all(v["tab_active"] and v["contract_ok"]
                       for v in verified.values()):
                    break
                time.sleep(0.4)

            if not all(v["tab_active"] and v["contract_ok"]
                       for v in verified.values()):
                raise RuntimeError(f"switch not confirmed: {verified}")

            return self._finish(result.succeed({
                "switched": switched,
                "verified": verified,
            }))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)
