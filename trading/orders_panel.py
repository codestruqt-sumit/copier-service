"""Orders widget: read working orders. Used to VERIFY order-ticket sends.

Columns (confirmed 2026-08-18): Id, Action, Order Qty, Contract, Order Type,
Price, Stop Price, Time In Force.

A market order fills immediately and shows as a position, not a working order;
a LIMIT/STOP rests here as a working order. So order_ticket.place() verifies:
  * market   -> a new position appears (positions.py)
  * non-market -> a working order appears here with matching contract/type/qty
"""

from __future__ import annotations

import re
from typing import Optional

from core.results import ActionResult
from trading import tables
from trading.base import TerminalModule

_ID = re.compile(r"\b(\d{10,})\b")
_CONTRACT = re.compile(r"^[A-Z]{1,4}[FGHJKMNQUVXZ]\d{1,2}$")
_PRICE = re.compile(r"^\d{2,7}\.\d{1,2}$")
_ORDER_TYPES = ("Stop Limit", "Trl Stop", "Limit", "Stop", "Market")
_TERMINAL_STATUS = ("filled", "cancelled", "canceled", "rejected", "expired")
_WORKING_STATUS = ("working", "pending", "suspended")


class OrdersPanel(TerminalModule):

    def _read(self, scroll: bool = True) -> dict:
        return tables.read_table(self.driver, "Orders", scroll=scroll)

    def _normalise(self, records: list) -> list:
        """Parse each order row by PATTERN, not column position.

        fixedDataTable columns shift when a cell is empty (market orders have no
        price), so column-name mapping is unreliable across rows. Instead we
        pull each field out by its shape from the row's values - robust to the
        column drift and to the locked-column duplication.
        """
        out = []
        seen = set()
        for rec in records:
            vals = [str(v).strip() for v in rec.values() if str(v).strip()]
            joined = " ".join(vals)
            low = joined.lower()

            idm = _ID.search(joined)
            if not idm:
                continue                       # header / date-only separator row
            oid = idm.group(1)
            if oid in seen:
                continue
            contract = next((v for v in vals if _CONTRACT.match(v)), None)
            if not contract:
                continue
            seen.add(oid)

            status = next((s.capitalize() for s in
                           (_WORKING_STATUS + _TERMINAL_STATUS) if s in low), None)
            out.append({
                "id": oid,
                "action": "Buy" if "buy" in low else ("Sell" if "sell" in low else None),
                "qty": next((v for v in vals if re.fullmatch(r"\d{1,3}", v)), None),
                "contract": contract,
                "order_type": next((t for t in _ORDER_TYPES if t.lower() in low), None),
                "price": next((v for v in vals if _PRICE.match(v)), None),
                "status": status,
                "is_working": any(s in low for s in _WORKING_STATUS),
                "row_text": low,
                "raw": rec,
            })
        return out

    def report(self, scroll: bool = True) -> ActionResult:
        """Capture every order (scrolling the virtualized grid), split into
        working vs historical for monitoring."""
        result = ActionResult(action="orders.report")
        try:
            self._precheck(result)
            result.enter("read_table")
            table = self._read(scroll=scroll)
            if table.get("error"):
                raise RuntimeError(table["error"])
            orders = self._normalise(table.get("records", []))
            working = [o for o in orders if o["is_working"]]
            return self._finish(result.succeed({
                "count": len(orders),
                "working_count": len(working),
                "working": working,
                "orders": orders,
            }))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)

    def working(self) -> ActionResult:
        """Just the live working/pending orders - the monitoring hot path."""
        rep = self.report()
        if rep.ok:
            rep.data = {"count": rep.data["working_count"],
                        "orders": rep.data["working"]}
        return rep

    def find(self, contract: str = None, order_type: str = None,
             ids_to_exclude=None) -> ActionResult:
        """Find working orders matching filters (for post-send verification)."""
        result = ActionResult(action="orders.find")
        result.meta.update({"contract": contract, "order_type": order_type})
        try:
            self._precheck(result)
            result.enter("read_table")
            table = self._read()
            if table.get("error"):
                raise RuntimeError(table["error"])
            exclude = set(ids_to_exclude or [])
            matches = []
            for order in self._normalise(table.get("records", [])):
                if order["id"] in exclude:
                    continue
                if not order["is_working"]:       # skip filled/cancelled history
                    continue
                # match against the whole row (columns can shift on empty cells)
                if contract and contract.upper() not in order["row_text"].upper():
                    continue
                if order_type and order_type.upper() not in order["row_text"].upper():
                    continue
                matches.append(order)
            return self._finish(result.succeed({"count": len(matches),
                                                "matches": matches}))
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, str(exc), exc)
