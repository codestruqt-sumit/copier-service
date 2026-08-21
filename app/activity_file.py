"""Machine-readable activity log.

Every copier event is appended as ONE JSON line to DATA_DIR/logs/activity.jsonl, so the
ATS (and a human) can reconstruct copier activity from a file without the DB. Purely
additive observability - it never changes trading/copier behaviour and never raises into
the caller. Thread-safe: the poller and the executor both log.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

_lock = threading.Lock()


def activity_path() -> Path:
    directory = Path(settings.data_dir) / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "activity.jsonl"


def append_activity(level: str, category: str, message: str, data: dict | None = None) -> None:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "category": category,
        "message": message,
        "data": data or {},
    }
    try:
        line = json.dumps(record, default=str)
        with _lock, open(activity_path(), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001 - logging must never break the caller
        pass
