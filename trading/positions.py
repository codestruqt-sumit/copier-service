"""Positions widget: report open positions, and act on a symbol's position.

Columns (confirmed 2026-08-18): Symbol, Net Pos, Net Price, Open P/L, $.

report()  -> #6/#7: capture every open position and hand it back through the
             notifier (dashboard/Telegram later). This is the "position
             reporter that captures info and reports back".
get(sym)  -> one symbol's position (or None if flat).
close_position(sym) -> flatten via the chart trade panel's Exit at Mkt, which
             is symbol-scoped and the safest close path. Verified by re-report.

NOTE on close mechanics: the per-row close control inside the Positions grid
was not yet observed (no position was open during the read-only probe). Until
it is confirmed live, close_position() routes through ChartTradePanel.exit_at_mkt
after switching the chart to the symbol - a path we DO control and verify.
"""

from __future__ import annotations

import re
from typing import Optional

from core.results import ActionResult
from trading import tables
from trading.base import TerminalModule

_NUM = re.compile(r"-?\d[\d,]*\.?\d*")


def _to_int(text: str) -> Optional[int]:
    m = _NUM.search(text or "")
    if not m:
        return None
    try:
        return int(float(m.group(0).replace(",", "")))
    except ValueError:
        return None


class PositionsPanel(TerminalModule):

    def _read(self, scroll: bool = True) -> dict:
        # scroll=False reads only the VISIBLE rows and never drags the scrollbar - the
        # drag can move/close a golden-layout widget, so hot-path callers that trade a
        # handful of symbols (all visible) should pass scroll=False.
        return tables.read_table(self.driver, "Positions", scroll=scroll)

    def _normalise(self, records: list) -> list:
        out = []
        for rec in records:
            # header row sometimes re-appears; skip it
            if rec.get("Symbol", "").lower() == "symbol":
                continue
            symbol = rec.get("Symbol") or rec.get("col0") or ""
            if not symbol:
                continue
            net_pos_raw = (rec.get("Net Pos") or rec.get("col1") or "")
            out.append({
                "symbol": symbol,
                "net_pos": _to_int(net_pos_raw),
                "net_pos_raw": net_pos_raw,
                "net_price": rec.get("Net Price") or rec.get("col2"),
                "open_pl": rec.get("Open P/L, $") or rec.get("Open P/L")
                           or rec.get("col3"),
                "raw": rec,
            })
        return out

    # -- report --------------------------------------------------------------
    def report(self, scroll: bool = True) -> ActionResult:
        """Capture all open positions and report them back. scroll=False reads only the
        visible rows (no scrollbar drag) - complete when positions fit on screen."""
        result = ActionResult(action="positions.report")
        try:
            self._precheck(result)
            result.enter("read_table")
            table = self._read(scroll=scroll)
            if table.get("error"):
                raise RuntimeError(table["error"])
            positions = self._normalise(table.get("records", []))
            # keep only rows with a real net position
            open_positions = [p for p in positions if p["net_pos"]]
            return self._finish(result.succeed({
                "headers": table.get("headers"),
                "count": len(open_positions),
                "positions": open_positions,
                "all_rows": positions,
            }))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)

    def get(self, symbol: str, scroll: bool = True) -> ActionResult:
        result = ActionResult(action="positions.get")
        result.meta["symbol"] = symbol
        try:
            self._precheck(result)
            result.enter("read_table")
            table = self._read(scroll=scroll)
            if table.get("error"):
                raise RuntimeError(table["error"])
            want = symbol.strip().upper()
            for pos in self._normalise(table.get("records", [])):
                if pos["symbol"].upper().startswith(want) or want.startswith(pos["symbol"].upper()):
                    return self._finish(result.succeed({"found": True, "position": pos}))
            return self._finish(result.succeed({"found": False, "position": None}))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)

    # -- act -----------------------------------------------------------------
    def close_position(self, symbol: str, dry_run: bool = True) -> ActionResult:
        """Flatten `symbol` via the chart trade panel's Exit at Mkt.

        Switches the chart to `symbol` first so Exit acts on the right
        instrument, then verifies the position is gone.
        """
        result = ActionResult(action="positions.close")
        result.meta["symbol"] = symbol
        result.meta["dry_run"] = dry_run
        try:
            self._precheck(result)

            result.enter("check_open")
            before = self.get(symbol)
            if not (before.ok and before.data.get("found")
                    and before.data["position"]["net_pos"]):
                return self._finish(result.succeed(
                    {"already_flat": True, "note": f"no open position on {symbol}"}))

            # switch chart to the symbol, then Exit at Mkt
            from trading.workspace import WorkspaceTabs
            from trading.trade_panel import ChartTradePanel

            result.enter("switch_symbol")
            sw = WorkspaceTabs(self.driver).switch_to(symbol)
            if not sw.ok:
                raise RuntimeError(f"could not switch chart to {symbol}: {sw.error}")
            self.settle()

            result.enter("exit_at_mkt")
            ex = ChartTradePanel(self.driver).exit_at_mkt(expect_symbol=symbol,
                                                          dry_run=dry_run)
            if not ex.ok:
                raise RuntimeError(f"exit_at_mkt failed: {ex.error}")

            if dry_run:
                return self._finish(result.succeed(
                    {"dry_run": True, "would_close": symbol}))

            result.enter("verify_flat")
            self.settle(1.0, 1.6)
            after = self.get(symbol)
            still_open = (after.ok and after.data.get("found")
                          and after.data["position"]["net_pos"])
            if still_open:
                raise RuntimeError(f"{symbol} still shows a position after Exit")
            return self._finish(result.succeed({"closed": True, "symbol": symbol}))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)
