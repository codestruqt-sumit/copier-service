"""E2E step 3 - publish a mixed batch of signals from the SENDER, exactly the way the
Send page does (same service calls the /trading/send route uses).

Run with the SENDER repo's venv, cwd = the sender repo:

    .venv\\Scripts\\python.exe D:\\Codes\\copier-service\\scripts\\e2e_send_signals.py <master_id> <group_id>

Sequence: market buy 2 -> limit sell -> a SECOND limit on the same symbol (must
coexist) -> stop-limit -> exit symbol -> flatten-all -> cancel the first limit.
"""

import sys
import time
from decimal import Decimal

from app.db import SessionLocal
from app.models import CommandKind, CommandSide, SignalCommand
from app.services import trading as svc

master_id, group_id = int(sys.argv[1]), int(sys.argv[2])
db = SessionLocal()


def send(**kwargs) -> SignalCommand:
    return svc.create_command(db, author_id=master_id, group_ids=[group_id], **kwargs)


pause = 1.5

c1 = send(symbol="MNQU6", order_kind=CommandKind.MARKET, side=CommandSide.BUY, base_qty=2)
print(f"sent #{c1.id} market buy 2 MNQU6"); time.sleep(pause)

c2 = send(symbol="MGCZ6", order_kind=CommandKind.LIMIT, side=CommandSide.SELL,
          base_qty=1, limit_price=Decimal("2400.5"))
print(f"sent #{c2.id} limit sell MGCZ6 @2400.5"); time.sleep(pause)

c3 = send(symbol="MGCZ6", order_kind=CommandKind.LIMIT, side=CommandSide.SELL,
          base_qty=1, limit_price=Decimal("2410.0"))
print(f"sent #{c3.id} second limit sell MGCZ6 @2410.0 (must coexist)"); time.sleep(pause)

c4 = send(symbol="MNQU6", order_kind=CommandKind.STOP_LIMIT, side=CommandSide.BUY,
          base_qty=1, stop_price=Decimal("28100"), limit_price=Decimal("28105"))
print(f"sent #{c4.id} stop-limit buy MNQU6 28100/28105"); time.sleep(pause)

c5 = send(symbol="MNQU6", order_kind=CommandKind.EXIT, side=None, base_qty=1)
print(f"sent #{c5.id} exit MNQU6"); time.sleep(pause)

c6 = send(symbol=svc.FLATTEN_ALL, order_kind=CommandKind.EXIT, side=None, base_qty=1)
print(f"sent #{c6.id} flatten-all"); time.sleep(pause)

svc.cancel_command(db, db.get(SignalCommand, c2.id))
print(f"cancelled #{c2.id} (the first limit)")
print(f"SIGNALS={','.join(str(c.id) for c in (c1, c2, c3, c4, c5, c6))}")
