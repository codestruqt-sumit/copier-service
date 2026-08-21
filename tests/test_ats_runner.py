"""ATS runner: cycles generate entry+exit, observe the copier, and write a report.
Uses fake clients - no network, no terminal."""

from __future__ import annotations

import json

import ats.runner as R
from ats.__main__ import parse_duration


class FakeSender:
    base = "http://sender"

    def __init__(self):
        self._id = 0
        self.sent: list[dict] = []

    def config(self):
        return {"symbols": ["ES", "NQ"], "groups": [{"id": 1, "name": "G"}],
                "order_kinds": ["market", "bid", "ask", "exit"], "sides": ["buy", "sell"]}

    def send_signal(self, **body):
        self._id += 1
        self.sent.append(body)
        return {"id": self._id, "status": "published", **body}

    def status(self):
        return {"recent_commands": [{"id": self._id, "reaches_count": 1, "orphaned": False,
                                     "reaches": [{"account_ref": "REF1"}]}]}

    def close(self):
        pass


class FakeCopier:
    def __init__(self, base="http://copier"):
        self.base = base
        self.events: list[dict] = []

    def init_cursor(self):
        pass

    def drain(self):
        # every asked-about signal already resolved to a terminal 'done' on REF1
        actions = [{"signal_id": i, "account_ref": "REF1", "status": "done",
                    "kind": "place_market", "note": "net 0 -> 1"} for i in range(1, 50)]
        return actions, []

    def overview(self):
        return {"executor": {"enabled": True, "connected": True}}

    def close(self):
        pass


def test_ats_runner_records_full_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(R.time, "sleep", lambda *_a, **_k: None)   # no real waits
    report = tmp_path / "report.json"
    ats = R.Ats(sender=FakeSender(), copiers=[FakeCopier()], group="G",
                symbols=["ES", "NQ"], report_path=report,
                inter_range=(0, 0), exit_range=(0, 0), monitor_sec=1, seed=1)

    out = ats.run(0.01)

    assert out["cycles"], "expected at least one cycle"
    c = out["cycles"][0]
    assert c["entry"]["api_ok"] and c["entry"]["command_id"]      # reached the signal API
    assert c["exit"]["api_ok"] and c["exit"]["command_id"]
    assert c["entry"]["request"]["qty"] == 1                      # always qty 1
    assert c["exit"]["request"]["order_kind"] == "exit"
    assert c["entry"]["delivery"]["reaches_count"] == 1           # signal-system view
    obs = c["observed"]["http://copier"]
    assert any(a["status"] == "done" for a in obs["actions"])     # copier downstream

    # end-of-run reconciliation attaches the DEFINITIVE final status per signal
    assert c["entry"]["final_status"] == "done"
    assert c["exit"]["final_status"] == "done"
    assert c["entry"]["final_actions"] and c["entry"]["final_actions"][0]["status"] == "done"

    # summary rolls it up for the verdict
    s = out["summary"]
    assert s["cycles"] == len(out["cycles"])
    assert s["entries_api_ok"] == len(out["cycles"])
    assert s["entry_final_status"].get("done") == len(out["cycles"])
    assert s["orphaned_signals"] == 0

    # report written + self-contained
    assert report.exists()
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["cycles"] and data["meta"]["group"] == "G"
    assert data["summary"]["cycles"] >= 1 and "copier_event_audit" in data


def test_parse_duration():
    assert parse_duration("2h") == 7200
    assert parse_duration("30m") == 1800
    assert parse_duration("90s") == 90
    assert parse_duration("3600") == 3600
