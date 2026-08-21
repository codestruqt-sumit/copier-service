"""Publish ONE signal from the Sender - the live-validation driver.

Run with the SENDER repo's venv, cwd = the sender repo:

    .venv\\Scripts\\python.exe D:\\Codes\\copier-service\\scripts\\send_one.py \
        <master_id> <group_id> <kind> <symbol> [side] [qty] [limit_price] [stop_price]

kinds: market bid ask limit stop stop_limit exit flatten
"""

import sys
from decimal import Decimal

from app.db import SessionLocal
from app.models import CommandKind, CommandSide
from app.services import trading as svc

master_id, group_id, kind, symbol = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4]
side = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] not in ("-", "") else None
qty = int(sys.argv[6]) if len(sys.argv) > 6 else 1
limit_price = Decimal(sys.argv[7]) if len(sys.argv) > 7 and sys.argv[7] != "-" else None
stop_price = Decimal(sys.argv[8]) if len(sys.argv) > 8 and sys.argv[8] != "-" else None

if kind == "flatten":
    kind, symbol = "exit", svc.FLATTEN_ALL

db = SessionLocal()
cmd = svc.create_command(
    db,
    author_id=master_id,
    group_ids=[group_id],
    symbol=symbol,
    order_kind=CommandKind(kind),
    side=CommandSide(side) if side else None,
    base_qty=qty,
    limit_price=limit_price,
    stop_price=stop_price,
)
print(f"SENT signal #{cmd.id} {cmd.order_kind.value} {cmd.symbol} "
      f"{cmd.side.value if cmd.side else '-'} x{cmd.base_qty}")
