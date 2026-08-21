"""Validate the two 2026-08-20 fixes against the LIVE terminal - WITHOUT placing orders.

Exercises the exact classes the executor uses:
  (1) ticket.set_qty(1)  -> expects the qty SHORT-CIRCUIT (skips the fragile preset
      dropdown because the ticket already shows 1). Also checks set_qty(2) still commits
      via the dropdown, then restores 1. set_qty never submits an order.
  (2) tabs.switch_to(S) then panel.sell_market(expect_symbol=S, dry_run=True) for several
      symbols -> exercises the panel SYMBOL-LAG poll. dry_run LOCATES the button + verifies
      the panel header caught up to S, but NEVER clicks -> nothing is placed.

Run ONLY when the copier executor is NOT attached (stop the copier first): two Selenium
drivers on one tab conflict. The script refuses if it detects a live executor on :8100.

    python scripts/validate_fixes.py
"""

from __future__ import annotations

import pathlib
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

SYMBOLS = ["MNQU6", "SIU6", "MGCZ6", "SIU6"]   # repeat SIU6 to exercise a re-switch


def _executor_attached() -> bool:
    """True if the copier service is up AND its executor has a driver attached."""
    try:
        import json
        with urllib.request.urlopen("http://127.0.0.1:8100/api/overview", timeout=4) as r:
            d = json.loads(r.read().decode("utf-8"))
        return bool((d.get("executor") or {}).get("connected"))
    except Exception:  # noqa: BLE001 - service down/unreachable = safe to attach
        return False


def main() -> int:
    if _executor_attached():
        print("REFUSING: the copier executor is attached on :8100 (two drivers on one tab "
              "conflict).\nStop the copier first, then re-run this.")
        return 3

    from app.terminal import TerminalGateway
    g = TerminalGateway(fast_market=True)
    ok, detail = g.ensure_connected()
    if not ok:
        print(f"attach failed: {detail}")
        return 2
    try:
        li, ld = g.login_check()
        print(f"attached ({detail}); logged_in={li} ({ld})\n")
        if not li:
            print("not logged in - open/log into Tradovate and re-run")
            return 2

        print("== (1) ticket qty short-circuit ==")
        r1 = g.ticket.set_qty(1)
        sc = r1.meta.get("qty_shortcircuit")
        print(f"  set_qty(1): ok={r1.ok} shortcircuit={sc} "
              f"tries={r1.meta.get('qty_dropdown_tries')} err={r1.error}")
        if r1.ok and sc:
            print("  -> PASS: skipped the dropdown (ticket already showed 1)")
        elif r1.ok:
            print("  -> ok, but went via the dropdown (ticket wasn't at 1); "
                  "short-circuit not exercised this time")
        else:
            print("  -> FAIL")
        r2 = g.ticket.set_qty(2)
        print(f"  set_qty(2): ok={r2.ok} shortcircuit={r2.meta.get('qty_shortcircuit')} "
              f"tries={r2.meta.get('qty_dropdown_tries')} err={r2.error}  "
              f"(dropdown path still works?)")
        g.ticket.set_qty(1)   # restore default

        print("\n== (2) panel symbol-lag poll (dry_run - NO orders) ==")
        for sym in SYMBOLS:
            sw = g.tabs.switch_to(sym)
            fr = g.panel.sell_market(expect_symbol=sym, dry_run=True)
            verdict = "OK" if fr.ok else f"REFUSED/err: {fr.error}"
            print(f"  {sym:6}: switch_ok={sw.ok!s:5} dry_fire={verdict} "
                  f"panel_symbol={fr.meta.get('panel_symbol')!r}")
        print("\n(dry_run: no orders were placed; ticket left at qty 1)")
        return 0
    finally:
        try:
            g.session.stop()   # detach only - leaves your logged-in browser open
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
