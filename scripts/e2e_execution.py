"""Part 3 full-flow E2E: signal (Sender) -> reception (Copier) -> terminal execution.

Runs with the SENDER repo's venv (needs app.services.trading to publish), and polls
the running executor-armed Copier's dashboard API for outcomes. Places REAL orders on
the attached terminal and flattens everything at the end.

    # sender on :8010, executor-armed copier on :8102, both accounts assigned to the copier
    cd D:\\Codes\\prop-dashboard
    .venv\\Scripts\\python.exe D:\\Codes\\copier-service\\scripts\\e2e_execution.py <master_id> <group_id> [copier_url]

Prints a PASS/FAIL matrix and exits non-zero on any failure.
"""

from __future__ import annotations

import sys
import time
import urllib.request
import json
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.models import CommandKind, CommandSide, ExecutionReport, SignalCommand, TradingAccount
from app.services import trading as svc

MASTER = int(sys.argv[1]) if len(sys.argv) > 1 else 1
GROUP = int(sys.argv[2]) if len(sys.argv) > 2 else 1
COPIER = sys.argv[3] if len(sys.argv) > 3 else "http://127.0.0.1:8102"
SYMBOL = "MNQU6"

FAILURES: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  -> {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(label)


def overview() -> dict:
    with urllib.request.urlopen(f"{COPIER}/api/overview", timeout=10) as r:
        return json.loads(r.read().decode())


def send(kind, side=None, qty=1, limit=None, stop=None) -> SignalCommand:
    symbol = SYMBOL
    if kind == "flatten":                       # flatten-all sentinel
        kind, symbol = "exit", svc.FLATTEN_ALL
    db = SessionLocal()
    try:
        cmd = svc.create_command(
            db, author_id=MASTER, group_ids=[GROUP], symbol=symbol,
            order_kind=CommandKind(kind),
            side=CommandSide(side) if side else None, base_qty=qty,
            limit_price=Decimal(str(limit)) if limit else None,
            stop_price=Decimal(str(stop)) if stop else None,
        )
        db.refresh(cmd)
        return cmd
    finally:
        db.close()


def set_kill(account_ref: str, allowed: bool) -> None:
    db = SessionLocal()
    try:
        acc = db.scalar(select(TradingAccount).where(TradingAccount.account_ref == account_ref))
        acc.trading_allowed = allowed
        db.commit()
    finally:
        db.close()


def wait_for(signal_id: int, expected: int, timeout: int = 180) -> list[dict]:
    """Wait until all `expected` actions for signal_id reach a terminal status."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        acts = [a for a in overview()["queue"] if a["signal_id"] == signal_id]
        pend = [a for a in acts if a["status"] in ("queued", "executing")]
        if len(acts) >= expected and not pend:
            return acts
        time.sleep(3)
    return [a for a in overview()["queue"] if a["signal_id"] == signal_id]


def sender_reports(signal_id: int) -> dict[str, str]:
    db = SessionLocal()
    try:
        rows = db.scalars(select(ExecutionReport).where(ExecutionReport.signal_command_id == signal_id))
        acc = {a.id: a.account_ref for a in db.scalars(select(TradingAccount))}
        return {acc.get(r.trading_account_id, str(r.trading_account_id)): r.status.value for r in rows}
    finally:
        db.close()


def net(term: dict, symbol: str) -> int:
    for p in (term or {}).get("positions", []):
        if symbol in (p.get("symbol") or ""):
            try:
                return int(float(p.get("net_pos") or 0))
            except (TypeError, ValueError):
                return 0
    return 0


print(f"Part 3 E2E  sender=master#{MASTER}/group#{GROUP}  copier={COPIER}  symbol={SYMBOL}")
ov = overview()
accs = [a["account_ref"] for a in ov["accounts"]]
check(ov["executor"]["enabled"] and ov["executor"]["connected"], "executor connected + armed",
      str(ov["executor"]))
check(len(accs) == 2, "copier owns 2 accounts", str(accs))
A1, A2 = accs[0], accs[1]

print("\n[1] MARKET buy 1 -> both accounts fill")
c = send("market", "buy", 1)
acts = wait_for(c.id, 2)
check(len(acts) == 2 and all(a["status"] == "done" for a in acts),
      "market: both actions done", str([(a["account"], a["status"]) for a in acts]))
check(all(r == "filled" for r in sender_reports(c.id).values()) and len(sender_reports(c.id)) == 2,
      "market: Sender got 'filled' reports", str(sender_reports(c.id)))

print("\n[2] LIMIT buy 1 far below -> both rest as working orders")
c = send("limit", "buy", 1, limit=25000)
acts = wait_for(c.id, 2)
check(len(acts) == 2 and all(a["status"] == "done" for a in acts),
      "limit: both actions done (resting)", str([(a["account"], a["status"]) for a in acts]))
check(all(a.get("order_ref") for a in acts), "limit: broker order refs captured",
      str([a.get("order_ref") for a in acts]))
check(all(r == "executing" for r in sender_reports(c.id).values()),
      "limit: Sender got 'executing' reports", str(sender_reports(c.id)))

print("\n[3] STOP buy 1 far above -> both rest as working orders")
c = send("stop", "buy", 1, stop=34000)
acts = wait_for(c.id, 2)
check(len(acts) == 2 and all(a["status"] == "done" for a in acts),
      "stop: both actions done (resting)", str([(a["account"], a["status"]) for a in acts]))

print("\n[4] KILL SWITCH on account 1 + MARKET -> acct1 skipped, acct2 fills")
set_kill(A1, False)
time.sleep(6)  # let the copier's kill-switch cache refresh
c = send("market", "buy", 1)
acts = wait_for(c.id, 2)
by_acc = {a["account_ref"]: a["status"] for a in acts}
check(by_acc.get(A1) == "skipped", "kill switch: acct1 SKIPPED (not executed)", str(by_acc))
check(by_acc.get(A2) == "done", "kill switch: acct2 executed", str(by_acc))
check(sender_reports(c.id).get(A1) == "skipped", "kill switch: Sender got 'skipped' for acct1",
      str(sender_reports(c.id)))
set_kill(A1, True)

print("\n[5] STOP-LIMIT -> both FAIL cleanly (not yet supported; must not misfire)")
c = send("stop_limit", "buy", 1, limit=27500, stop=27400)
acts = wait_for(c.id, 2)
check(len(acts) == 2 and all(a["status"] == "failed" for a in acts),
      "stop-limit: both failed cleanly", str([(a["account"], a["status"]) for a in acts]))
check(all("not yet supported" in (a.get("note") or "") for a in acts),
      "stop-limit: failure reason is explicit")

print("\n[6] EXIT symbol -> both accounts flatten (executor verifies net -> 0 per account)")
c = send("exit")
acts = wait_for(c.id, 2, timeout=240)
check(len(acts) == 2 and all(a["status"] == "done" for a in acts),
      "exit: both accounts flattened (done)", str([(a["account"], a["status"], a["note"]) for a in acts]))

print("\n[7] FLATTEN ALL -> both accounts end flat")
c = send("flatten")
acts = wait_for(c.id, 2, timeout=240)
check(len(acts) == 2 and all(a["status"] == "done" for a in acts),
      "flatten-all: both accounts flat (done)", str([(a["account"], a["status"], a["note"]) for a in acts]))
time.sleep(5)
term = overview().get("terminal") or {}
check(net(term, SYMBOL) == 0, "flatten: active-account snapshot is flat",
      f"net={net(term, SYMBOL)} positions={term.get('positions')}")

print("\n" + "=" * 60)
if FAILURES:
    print(f"E2E FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
    sys.exit(1)
print("PART 3 E2E: ALL CHECKS PASSED")
