"""The long-running ATS.

Each cycle: pick a random symbol + a random (price-free) order type, send it with qty=1
through the Signal Provider API, wait a random 60-180s, send the matching exit, then
OBSERVE the copier downstream and record the whole ATS -> API -> system -> copier chain.
Between cycles it waits a random 300-900s. Runs until the operator's duration elapses,
then writes a JSON report for verdict analysis.

The ATS never touches the terminal: it only calls the signal API and reads copier/diag
machine endpoints.
"""

from __future__ import annotations

import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from ats.api import CopierObserver, SenderAts

# Price-free entry types (qty always 1). market/bid/ask carry NO price, so the ATS needs
# no market-data source and stays within its no-terminal-access boundary; each is closed
# by an EXIT, keeping the balance flat. (limit/stop/stop_limit need prices and are left
# out of the default set intentionally - they can be added via --entries with prices.)
DEFAULT_ENTRIES = [
    {"kind": "market", "side": "buy"},
    {"kind": "market", "side": "sell"},
    {"kind": "bid", "side": "buy"},
    {"kind": "ask", "side": "sell"},
]

TERMINAL_STATUSES = {"done", "failed", "skipped", "cancelled", "superseded", "expired"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    """Console line so the operator can watch the ATS while it runs."""
    print(f"[ATS {datetime.now(timezone.utc).strftime('%H:%M:%S')}] {message}", flush=True)


class Ats:
    def __init__(self, *, sender: SenderAts, copiers: list[CopierObserver], group: str,
                 symbols: list[str], report_path: Path, entries: list[dict] | None = None,
                 inter_range=(300, 900), exit_range=(60, 180), monitor_sec: int = 90,
                 grace_sec: int = 180, seed: int | None = None):
        self.sender = sender
        self.copiers = copiers
        self.group = group
        self.symbols = symbols
        self.entries = entries or DEFAULT_ENTRIES
        self.report_path = Path(report_path)
        self.inter_range = inter_range
        self.exit_range = exit_range
        self.monitor_sec = monitor_sec
        self.grace_sec = grace_sec
        self.rng = random.Random(seed)
        self.cycles: list[dict] = []
        self.meta: dict = {}
        self.summary: dict = {}

    # -- lifecycle ---------------------------------------------------------------

    def run(self, duration_sec: float) -> dict:
        for obs in self.copiers:
            try:
                obs.init_cursor()
            except Exception as exc:  # noqa: BLE001
                log(f"WARN could not reach copier {obs.base} at startup: {exc!r}")
        start = time.monotonic()
        self.meta = {
            "started_at": _now(),
            "duration_sec": duration_sec,
            "group": self.group,
            "symbols": self.symbols,
            "entries": self.entries,
            "sender": self.sender.base,
            "copiers": [o.base for o in self.copiers],
            "inter_range_sec": list(self.inter_range),
            "exit_range_sec": list(self.exit_range),
        }
        log(f"start: {duration_sec:.0f}s, group={self.group}, symbols={self.symbols}, "
            f"copiers={[o.base for o in self.copiers]}")
        n = 0
        try:
            while time.monotonic() - start < duration_sec:
                n += 1
                cyc = self._cycle(n)
                self.cycles.append(cyc)
                self._flush()
                wait = self.rng.randint(*self.inter_range)
                log(f"cycle {n} done; next in {wait}s")
                if not self._sleep_bounded(start, duration_sec, wait):
                    break
        except KeyboardInterrupt:
            log("interrupted by operator - reconciling + writing report")
        # DEFINITIVE outcomes: the per-cycle monitor is best-effort and can end while the
        # (serial) executor is still catching up, so before finishing we wait for every
        # generated signal to reach a terminal status on the copier - capturing the final
        # state the copier dashboard shows, not just the mid-flight snapshot.
        self._reconcile(self.grace_sec)
        self.meta["ended_at"] = _now()
        self.meta["cycles"] = len(self.cycles)
        self.summary = self._summary()
        self._flush(final=True)
        log(f"finished: {len(self.cycles)} cycle(s). summary={self.summary}")
        log(f"report: {self.report_path}")
        return {"meta": self.meta, "cycles": self.cycles, "summary": self.summary}

    def _reconcile(self, grace_sec: int) -> None:
        """Wait (bounded) for every generated signal to reach a terminal status on every
        copier, then attach the definitive final_status + final_actions per signal. Also
        drains remaining events into the audit so it is complete."""
        all_ids = set()
        for c in self.cycles:
            for k in ("entry", "exit"):
                cid = c.get(k, {}).get("command_id")
                if cid:
                    all_ids.add(cid)
        if not all_ids or not self.copiers:
            return
        log(f"reconciling final outcomes for {len(all_ids)} signal(s) (grace {grace_sec}s)...")
        found: dict[int, dict] = {}
        deadline = time.monotonic() + grace_sec
        while True:
            for obs in self.copiers:
                try:
                    actions, _ = obs.drain()
                except Exception as exc:  # noqa: BLE001
                    log(f"WARN reconcile drain failed ({obs.base}): {exc!r}")
                    continue
                for a in actions:
                    sid = a.get("signal_id")
                    if sid in all_ids:
                        found.setdefault(sid, {})[a.get("account_ref") or a.get("account")] = a
            if self._all_ids_terminal(found, all_ids) or time.monotonic() >= deadline:
                break
            time.sleep(4)
        for c in self.cycles:
            for k in ("entry", "exit"):
                cid = c.get(k, {}).get("command_id")
                if not cid:
                    continue
                acts = list(found.get(cid, {}).values())
                c[k]["final_actions"] = acts
                c[k]["final_status"] = self._roll_up(acts)

    @staticmethod
    def _all_ids_terminal(found: dict, all_ids: set) -> bool:
        for cid in all_ids:
            accts = found.get(cid)
            if not accts or not any(a.get("status") in TERMINAL_STATUSES for a in accts.values()):
                return False
        return True

    @staticmethod
    def _roll_up(actions: list[dict]) -> str:
        if not actions:
            return "no_action"          # copier never produced an action for this signal
        statuses = sorted({a.get("status") for a in actions})
        return statuses[0] if len(statuses) == 1 else "|".join(statuses)

    def _summary(self) -> dict:
        def tally(kind: str) -> dict:
            out: dict[str, int] = {}
            for c in self.cycles:
                s = c.get(kind, {}).get("final_status", "unobserved")
                out[s] = out.get(s, 0) + 1
            return out
        return {
            "cycles": len(self.cycles),
            "entries_api_ok": sum(1 for c in self.cycles if c.get("entry", {}).get("api_ok")),
            "exits_api_ok": sum(1 for c in self.cycles if c.get("exit", {}).get("api_ok")),
            "entry_final_status": tally("entry"),
            "exit_final_status": tally("exit"),
            "orphaned_signals": sum(
                1 for c in self.cycles for k in ("entry", "exit")
                if (c.get(k, {}).get("delivery") or {}).get("orphaned")),
        }

    # -- one cycle ---------------------------------------------------------------

    def _cycle(self, n: int) -> dict:
        symbol = self.rng.choice(self.symbols)
        spec = self.rng.choice(self.entries)
        rec: dict = {"n": n, "symbol": symbol, "entry_spec": spec, "started_at": _now()}

        rec["entry"] = self._send(kind=spec["kind"], side=spec.get("side"), symbol=symbol)
        log(f"cycle {n}: entry {spec['kind']} {spec.get('side') or ''} {symbol} "
            f"-> id={rec['entry'].get('command_id')} api_ok={rec['entry'].get('api_ok')}")

        delay = self.rng.randint(*self.exit_range)
        rec["exit_delay_sec"] = delay
        time.sleep(delay)

        rec["exit"] = self._send(kind="exit", side=None, symbol=symbol)
        log(f"cycle {n}: exit {symbol} -> id={rec['exit'].get('command_id')} "
            f"api_ok={rec['exit'].get('api_ok')}")
        if not rec["exit"].get("api_ok"):
            rec["exit_send_failed"] = True
            log(f"WARN cycle {n}: EXIT SEND FAILED for {symbol} after retries - the {symbol} "
                f"position from this cycle's entry may be LEFT OPEN. Flatten it manually.")

        ids = [i for i in (rec["entry"].get("command_id"), rec["exit"].get("command_id")) if i]
        rec["observed"] = self._monitor(ids)
        rec["ended_at"] = _now()
        return rec

    def _send(self, *, kind: str, side: str | None, symbol: str) -> dict:
        body = {"symbol": symbol, "order_kind": kind, "qty": 1, "groups": [self.group]}
        if side:
            body["side"] = side
        out: dict = {"sent_at": _now(), "request": body, "api_ok": False}
        try:
            resp = self.sender.send_signal(**body)
            out.update({"api_ok": True, "command_id": resp.get("id"), "response": resp})
        except Exception as exc:  # noqa: BLE001
            out["error"] = repr(exc)
            log(f"WARN send failed ({kind} {symbol}): {exc!r}")
        # signal-system view: which accounts/copiers this command resolves to
        try:
            st = self.sender.status()
            out["delivery"] = next(
                (c for c in st.get("recent_commands", []) if c.get("id") == out.get("command_id")),
                None)
        except Exception as exc:  # noqa: BLE001
            out["delivery_error"] = repr(exc)
        return out

    # -- downstream observation (read-only) --------------------------------------

    def _monitor(self, command_ids: list[int]) -> dict:
        ids = set(command_ids)
        deadline = time.monotonic() + self.monitor_sec
        per: dict[str, dict] = {o.base: {"actions": {}, "executor": None, "error": None}
                                for o in self.copiers}
        while True:
            for obs in self.copiers:
                slot = per[obs.base]
                try:
                    actions, _signals = obs.drain()      # also accumulates events
                    for a in actions:
                        if a.get("signal_id") in ids:
                            key = f"{a.get('signal_id')}:{a.get('account_ref') or a.get('account')}"
                            slot["actions"][key] = a
                    slot["executor"] = obs.overview().get("executor")
                except Exception as exc:  # noqa: BLE001
                    slot["error"] = repr(exc)
            if self._all_terminal(per, ids) or time.monotonic() >= deadline:
                break
            time.sleep(3)
        return {base: {"actions": list(v["actions"].values()),
                       "executor": v["executor"], "error": v["error"]}
                for base, v in per.items()}

    @staticmethod
    def _all_terminal(per: dict, ids: set) -> bool:
        """True once every command id has a terminal-status action on >=1 account for
        every copier - lets the monitor window end early when processing is complete."""
        if not ids:
            return True
        for slot in per.values():
            seen = {int(k.split(":")[0]): a for k, a in slot["actions"].items()}
            for cid in ids:
                a = seen.get(cid)
                if not a or a.get("status") not in TERMINAL_STATUSES:
                    return False
        return True

    # -- report ------------------------------------------------------------------

    def _flush(self, final: bool = False) -> None:
        payload = {
            "meta": {**self.meta, "cycles_completed": len(self.cycles), "final": final},
            "summary": self.summary,
            "cycles": self.cycles,
            "copier_event_audit": {o.base: o.events for o in self.copiers},
        }
        tmp = self.report_path.with_suffix(self.report_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.report_path)   # atomic - a crash never leaves a half-written report

    def _sleep_bounded(self, start: float, duration: float, wait: float) -> bool:
        """Sleep `wait`s but never past the run deadline. Returns False if the deadline
        is reached (so the loop should stop)."""
        target = min(time.monotonic() + wait, start + duration)
        while time.monotonic() < target:
            time.sleep(min(3.0, target - time.monotonic()))
        return (time.monotonic() - start) < duration
