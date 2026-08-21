"""Composite trading actions - the layered functions built from the widget
building blocks. This is where "place a buy-market on MNQ qty 1" becomes one
call: switch symbol -> set qty -> click -> verify via Positions.

Everything here reuses the tested single-purpose modules and adds
orchestration + end-to-end verification. These are the shapes an external
signal will eventually drive.
"""

from __future__ import annotations

from typing import Optional

from core.results import ActionResult
from trading.base import TerminalModule
from trading.order_ticket import OrderTicket
from trading.positions import PositionsPanel
from trading.trade_panel import ChartTradePanel
from trading.workspace import WorkspaceTabs


class CompositeTrader(TerminalModule):

    # -- market order via the chart trade panel ------------------------------
    def market_order(self, symbol: str, side: str, qty: int = 1,
                     dry_run: bool = True) -> ActionResult:
        """Switch to `symbol`, set qty, click Buy/Sell Mkt, verify position.

        Verification: the symbol's net position must move by the expected
        signed amount versus the pre-trade snapshot.
        """
        result = ActionResult(action="composite.market_order")
        side = side.strip().lower()
        result.meta.update({"symbol": symbol, "side": side, "qty": qty,
                            "dry_run": dry_run})
        try:
            if side not in ("buy", "sell"):
                raise ValueError(f"side must be buy/sell, got {side!r}")
            self._precheck(result)

            positions = PositionsPanel(self.driver)
            panel = ChartTradePanel(self.driver)

            result.enter("snapshot_before")
            before = positions.get(symbol)
            net_before = 0
            if before.ok and before.data.get("found") and before.data["position"]:
                net_before = before.data["position"]["net_pos"] or 0
            result.meta["net_before"] = net_before

            result.enter("switch_symbol")
            sw = WorkspaceTabs(self.driver).switch_to(symbol)
            if not sw.ok:
                raise RuntimeError(f"symbol switch failed: {sw.error}")
            self.settle()

            result.enter("set_qty")
            sq = panel.set_qty(qty)
            if not sq.ok:
                raise RuntimeError(f"set qty failed: {sq.error}")
            self.tick()

            result.enter("click_order")
            fire = (panel.buy_market if side == "buy" else panel.sell_market)
            fired = fire(expect_symbol=symbol, dry_run=dry_run, expect_qty=qty)
            if not fired.ok:
                raise RuntimeError(f"{side} market failed: {fired.error}")

            if dry_run:
                return self._finish(result.succeed(
                    {"dry_run": True, "note": f"would {side} {qty} {symbol}"}))

            result.enter("verify_position")
            self.settle(1.2, 1.8)
            expected = net_before + (qty if side == "buy" else -qty)
            after = positions.get(symbol)
            net_after = 0
            if after.ok and after.data.get("found") and after.data["position"]:
                net_after = after.data["position"]["net_pos"] or 0
            result.meta["net_after"] = net_after
            result.meta["net_expected"] = expected
            if net_after != expected:
                raise RuntimeError(
                    f"position did not move as expected: before={net_before} "
                    f"after={net_after} expected={expected}")
            return self._finish(result.succeed({
                "symbol": symbol, "side": side, "qty": qty,
                "net_before": net_before, "net_after": net_after,
            }))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)

    # -- close / flatten -----------------------------------------------------
    def close_symbol(self, symbol: str, dry_run: bool = True) -> ActionResult:
        return PositionsPanel(self.driver, self.notifier).close_position(
            symbol, dry_run=dry_run)

    def flatten_all(self, dry_run: bool = True) -> ActionResult:
        """Close every open position. Reports which it closed."""
        result = ActionResult(action="composite.flatten_all")
        result.meta["dry_run"] = dry_run
        try:
            self._precheck(result)
            positions = PositionsPanel(self.driver)
            report = positions.report()
            if not report.ok:
                raise RuntimeError(f"positions.report failed: {report.error}")
            targets = [p["symbol"] for p in report.data["positions"]]
            result.meta["targets"] = targets
            outcomes = []
            for symbol in targets:
                self.settle()
                closed = positions.close_position(symbol, dry_run=dry_run)
                outcomes.append({"symbol": symbol, "ok": closed.ok,
                                 "error": closed.error})
            return self._finish(result.succeed({"count": len(targets),
                                                "outcomes": outcomes}))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)

    # -- limit / stop via the order ticket -----------------------------------
    def working_order(self, symbol: str, side: str, qty: int, order_type: str,
                      price=None, flag: str = "DAY",
                      dry_run: bool = True) -> ActionResult:
        """Place a resting order (LIMIT/STOP/...) through the order ticket and
        verify it lands in the Orders panel."""
        return OrderTicket(self.driver, self.notifier).place(
            symbol=symbol, side=side, qty=qty, order_type=order_type,
            price=price, flag=flag, dry_run=dry_run)

    # -- reverse (experimental building block) -------------------------------
    def reverse_position(self, symbol: str, dry_run: bool = True) -> ActionResult:
        """Flip a position's sign, keeping size (long N -> short N).

        Fires a market order of the opposite side sized 2x the current net.
        EXPERIMENTAL - defaults to dry_run.
        """
        result = ActionResult(action="composite.reverse")
        result.meta["symbol"] = symbol
        try:
            self._precheck(result)
            pos = PositionsPanel(self.driver).get(symbol)
            if not (pos.ok and pos.data.get("found") and pos.data["position"]):
                raise RuntimeError(f"no open position on {symbol} to reverse")
            net = pos.data["position"]["net_pos"] or 0
            if net == 0:
                raise RuntimeError(f"{symbol} net position is zero")
            side = "sell" if net > 0 else "buy"
            qty = abs(net) * 2
            result.meta.update({"current_net": net, "reverse_side": side,
                               "reverse_qty": qty})
            return self._finish(
                self.market_order(symbol, side, qty, dry_run=dry_run))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)
