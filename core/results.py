"""ActionResult - the uniform envelope every terminal action returns.

Every function that touches the terminal returns one of these instead of
raising. The caller (lab runner today; engine/notifiers tomorrow) can then
decide what to do: log it, push it to a dashboard, send it to Telegram.

Design rules:
  * an action NEVER lets an exception escape - it captures type+message+stage
  * `stage` records exactly how far we got, so a failure notification can say
    "accounts.list failed at stage 'open_menu'" instead of just "failed"
  * on failure a screenshot is captured when possible - post-mortems on a UI
    you cannot replay are otherwise guesswork
  * `to_dict()` is JSON-safe so results can go over the wire as-is
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ActionResult:
    action: str                          # e.g. "accounts.list"
    ok: bool = False
    stage: str = "init"                  # last stage reached
    stages_done: list = field(default_factory=list)
    data: Any = None                     # action-specific payload
    error: Optional[str] = None
    error_type: Optional[str] = None
    screenshot: Optional[str] = None     # path of failure screenshot, if taken
    started_at: datetime = field(default_factory=_now)
    finished_at: Optional[datetime] = None
    meta: dict = field(default_factory=dict)

    # -- lifecycle helpers ---------------------------------------------------
    def enter(self, stage: str) -> None:
        """Mark that we are now attempting `stage`."""
        if self.stage != "init":
            self.stages_done.append(self.stage)
        self.stage = stage

    def succeed(self, data: Any = None) -> "ActionResult":
        self.ok = True
        if data is not None:
            self.data = data
        self.stages_done.append(self.stage)
        self.finished_at = _now()
        return self

    def fail(self, error: str, exc: Optional[BaseException] = None,
             screenshot: Optional[str] = None) -> "ActionResult":
        self.ok = False
        self.error = error
        if exc is not None:
            self.error_type = type(exc).__name__
            if not error:
                self.error = str(exc).splitlines()[0][:300]
        self.screenshot = screenshot
        self.finished_at = _now()
        return self

    # -- reporting -------------------------------------------------------------
    @property
    def duration_ms(self) -> Optional[int]:
        if self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    def summary(self) -> str:
        mark = "OK " if self.ok else "FAIL"
        base = f"[{mark}] {self.action} (stage={self.stage}, {self.duration_ms}ms)"
        if not self.ok:
            base += f" - {self.error_type or 'Error'}: {self.error}"
        return base

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "ok": self.ok,
            "stage": self.stage,
            "stages_done": list(self.stages_done),
            "data": self.data,
            "error": self.error,
            "error_type": self.error_type,
            "screenshot": self.screenshot,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_ms": self.duration_ms,
            "meta": self.meta,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)
