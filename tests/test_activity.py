"""Machine-readable activity: JSONL file sink + console mirror via log_event."""

from __future__ import annotations

import json
import logging


def test_append_activity_writes_jsonl(tmp_path, monkeypatch):
    import app.activity_file as af

    monkeypatch.setattr(af.settings, "data_dir", str(tmp_path))
    af.append_activity("info", "exec", "placed order", {"signal_id": 7, "status": "done"})

    path = tmp_path / "logs" / "activity.jsonl"
    assert path.exists()
    rec = json.loads(path.read_text(encoding="utf-8").strip())
    assert rec["level"] == "info" and rec["category"] == "exec"
    assert rec["message"] == "placed order"
    assert rec["data"] == {"signal_id": 7, "status": "done"}
    assert "T" in rec["ts"]                                   # ISO timestamp


def test_log_event_mirrors_to_file_and_console(session_factory, tmp_path, monkeypatch, caplog):
    import app.activity_file as af
    from app.processor import log_event

    monkeypatch.setattr(af.settings, "data_dir", str(tmp_path))
    db = session_factory()
    try:
        with caplog.at_level(logging.INFO, logger="copier.activity"):
            log_event(db, "info", "executor", "executed action #5 done", {"action_id": 5})
            db.commit()
    finally:
        db.close()

    # console mirror
    assert any("executed action #5 done" in r.getMessage() for r in caplog.records)
    # file mirror
    rec = json.loads((tmp_path / "logs" / "activity.jsonl").read_text(encoding="utf-8").strip())
    assert rec["message"] == "executed action #5 done" and rec["data"]["action_id"] == 5
