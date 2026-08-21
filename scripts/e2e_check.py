"""E2E step 4 - assert the copier received, processed and queued everything correctly.

Run with the COPIER venv (any cwd):

    .venv\\Scripts\\python.exe scripts\\e2e_check.py [http://127.0.0.1:8100]
"""

from __future__ import annotations

import sys
import time
from decimal import Decimal, InvalidOperation

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8100"
FAILURES: list[str] = []


def deq(raw: str | None, expected: str) -> bool:
    """Numeric compare - the real Sender serializes prices as '2400.50000000'."""
    try:
        return raw is not None and Decimal(raw) == Decimal(expected)
    except (InvalidOperation, TypeError):
        return False


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  -> {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(label)


def fetch() -> dict:
    return httpx.get(f"{BASE}/api/overview", timeout=10).json()


print(f"Checking copier at {BASE}")

# Wait for the copier to drain the sender (7 signal rows: 6 sends + 1 cancel revision).
deadline = time.time() + 60
data = fetch()
while time.time() < deadline and data.get("counts", {}).get("signals_total", 0) < 7:
    time.sleep(1)
    data = fetch()

health, counts, queue = data["health"], data["counts"], data["queue"]

print("\nHealth")
check(health.get("registered") is True, "registered with the Sender")
check(health.get("last_poll_ok") is True, "polling ok", str(health.get("last_error")))
check(health.get("accounts") == 2, "owns the 2 E2E accounts", str(health.get("accounts")))
check(bool(health.get("last_heartbeat_at")), "heartbeat sent")
check(bool(health.get("cursor")), "cursor persisted")

print("\nReception")
check(counts.get("signals_total", 0) >= 7, "all 7 signal deliveries received",
      f"got {counts.get('signals_total')}")

by_kind: dict[str, list[dict]] = {}
for action in queue:
    by_kind.setdefault(action["kind"], []).append(action)

print("\nQueue")
market = by_kind.get("place_market", [])
check(sorted(a["qty"] for a in market) == [2, 4],
      "market fan-out with scaled qty (base 2 -> 2 and 4)",
      str([(a['account'], a['qty']) for a in market]))

limits = by_kind.get("place_limit", [])
cancelled = [a for a in limits if a["status"] == "cancelled"]
still_queued = [a for a in limits if a["status"] == "queued"]
check(len(limits) == 4, "both limits fanned out (2 accounts each)", f"got {len(limits)}")
check(len(cancelled) == 2 and all(deq(a["limit_price"], "2400.5") for a in cancelled),
      "cancelled limit's actions are cancelled", str([(a['limit_price'], a['status']) for a in limits]))
check(len(still_queued) == 2 and all(deq(a["limit_price"], "2410.0") for a in still_queued),
      "the second limit coexists and stays queued")

stop_limits = by_kind.get("place_stop_limit", [])
check(len(stop_limits) == 2 and all(
    deq(a["stop_price"], "28100") and deq(a["limit_price"], "28105") and a["status"] == "queued"
    for a in stop_limits), "stop-limit carries both prices")

exits = by_kind.get("exit_symbol", [])
check(len(exits) == 2 and all(a["symbol"] == "MNQU6" and a["status"] == "queued" for a in exits),
      "exit-symbol queued per account")

flattens = by_kind.get("flatten_all", [])
check(len(flattens) == 2 and all(a["symbol"] == "*" and a["status"] == "queued" for a in flattens),
      "flatten-all sentinel queued per account")

print("\nDashboard")
page = httpx.get(f"{BASE}/", timeout=10)
check(page.status_code == 200 and "Copier" in page.text, "dashboard page serves")
check(httpx.get(f"{BASE}/healthz", timeout=10).text == "ok", "healthz ok")
check(any("Registered with Sender" in e["message"] for e in data["events"]),
      "activity log recorded the registration")
check(any("queued" in e["message"] for e in data["events"]),
      "activity log recorded queueing")

print()
if FAILURES:
    print(f"E2E FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
    sys.exit(1)
print("E2E: ALL CHECKS PASSED")
