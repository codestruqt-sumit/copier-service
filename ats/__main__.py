"""Run the ATS.

    python -m ats --duration 2h --sender https://YOUR-SENDER.azurewebsites.net \
        --group Medium --copier http://127.0.0.1:8100

The ATS key comes from --ats-key or the ATS_KEY env var (issue it in the Signal
Dashboard's Config tab). Symbols default to whatever /api/ats/config reports.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from ats.api import CopierObserver, SenderAts
from ats.runner import Ats, log


def parse_duration(text: str) -> float:
    t = text.strip().lower()
    try:
        if t.endswith("h"):
            return float(t[:-1]) * 3600
        if t.endswith("m"):
            return float(t[:-1]) * 60
        if t.endswith("s"):
            return float(t[:-1])
        return float(t)
    except ValueError:
        raise SystemExit(f"bad --duration {text!r} (use e.g. 2h, 30m, 90s, or seconds)")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ats", description="Signal Provider -> Copier ATS")
    p.add_argument("--duration", required=True, help="run length, e.g. 2h / 30m / 900s / 3600")
    p.add_argument("--sender", required=True, help="Signal Provider base URL")
    p.add_argument("--ats-key", default=os.environ.get("ATS_KEY"),
                   help="ATS API key (or set ATS_KEY env var)")
    p.add_argument("--group", required=True, help="target group name for generated signals")
    p.add_argument("--copier", action="append", default=[],
                   help="copier base URL to observe (repeatable), e.g. http://127.0.0.1:8100")
    p.add_argument("--symbols", default="",
                   help="comma-separated symbols (default: discover from /api/ats/config)")
    p.add_argument("--report", default="", help="report file path (default: ats-report-<ts>.json)")
    p.add_argument("--monitor-sec", type=int, default=90, help="per-cycle downstream observation window")
    p.add_argument("--grace-sec", type=int, default=180,
                   help="end-of-run window to wait for every signal to reach a terminal status")
    p.add_argument("--inter-min", type=int, default=300)
    p.add_argument("--inter-max", type=int, default=900)
    p.add_argument("--exit-min", type=int, default=60)
    p.add_argument("--exit-max", type=int, default=180)
    p.add_argument("--seed", type=int, default=None, help="RNG seed (reproducible runs)")
    args = p.parse_args(argv)

    if not args.ats_key:
        raise SystemExit("no ATS key: pass --ats-key or set ATS_KEY")

    duration = parse_duration(args.duration)
    sender = SenderAts(args.sender, args.ats_key)

    try:
        cfg = sender.config()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"cannot reach the Signal Provider ATS API: {exc!r}")

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or cfg.get("symbols", [])
    if not symbols:
        raise SystemExit("no symbols to test (none configured on the sender, none passed)")
    group_names = {g["name"] for g in cfg.get("groups", [])}
    if args.group not in group_names:
        log(f"WARN group {args.group!r} not in the sender's groups {sorted(group_names)} "
            f"- signals may reach no copier")

    copiers = [CopierObserver(url) for url in args.copier]
    if not copiers:
        log("WARN no --copier given: signals will be generated but not observed downstream")

    report = Path(args.report) if args.report else Path(
        f"ats-report-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json")

    ats = Ats(sender=sender, copiers=copiers, group=args.group, symbols=symbols,
              report_path=report, monitor_sec=args.monitor_sec, grace_sec=args.grace_sec,
              inter_range=(args.inter_min, args.inter_max),
              exit_range=(args.exit_min, args.exit_max), seed=args.seed)
    try:
        ats.run(duration)
    finally:
        sender.close()
        for c in copiers:
            c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
