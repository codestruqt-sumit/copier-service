"""The reception loop: register -> heartbeat -> poll commands -> process -> save cursor.

Runs in a daemon thread so the dashboard stays responsive and, crucially, so signal
capture is never blocked by anything else. Every cycle is wrapped: a Sender outage
degrades health and logs a transition event, it never kills the loop. The cursor is
persisted after each successful batch, so a restart resumes exactly where it left off.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session, sessionmaker

from app.models import KV
from app.processor import log_event, process_batch
from app.sender_client import SenderClient

log = logging.getLogger("copier.poller")

CURSOR_KEY = "cursor"
REGISTRATION_KEY = "registration"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def kv_get(db: Session, key: str) -> str | None:
    row = db.get(KV, key)
    return row.value if row else None


def kv_set(db: Session, key: str, value: str) -> None:
    row = db.get(KV, key)
    if row is None:
        db.add(KV(key=key, value=value))
    else:
        row.value = value


class Poller(threading.Thread):
    def __init__(self, client: SenderClient, session_factory: sessionmaker, settings,
                 health_provider=None, notifier=None):
        super().__init__(name="copier-poller", daemon=True)
        self.client = client
        self.session_factory = session_factory
        self.settings = settings
        # A callable returning the executor's health dict (enabled/connected/logged_in),
        # so each heartbeat can report the terminal's health to the Sender.
        self._health_provider = health_provider
        # Optional Telegram notifier (best-effort, non-blocking).
        self._notifier = notifier
        self.stop_event = threading.Event()

        self._lock = threading.Lock()
        self._health: dict = {
            "started_at": _utcnow().isoformat(),
            "registered": False,
            "accounts": 0,
            "last_poll_at": None,
            "last_poll_ok": None,
            "last_heartbeat_at": None,
            "healthy": None,
            "health_detail": None,
            "consecutive_errors": 0,
            "last_error": None,
            "cursor": None,
            "poll_sec": None,
            "cycles": 0,
        }
        self._registration: dict = {}
        self._registered_at: float = 0.0
        self._last_heartbeat: float = 0.0
        self._was_failing = False

    # --- health for the dashboard --------------------------------------------

    def health(self) -> dict:
        with self._lock:
            return dict(self._health)

    def registration(self) -> dict:
        with self._lock:
            return dict(self._registration)

    def _set(self, **fields) -> None:
        with self._lock:
            self._health.update(fields)

    # --- cadence ---------------------------------------------------------------

    def _poll_interval(self) -> float:
        if self.settings.poll_sec:
            return float(self.settings.poll_sec)
        cfg = self._registration.get("config") or {}
        return float(cfg.get("command_poll_sec") or 3)

    def _heartbeat_interval(self) -> float:
        if self.settings.heartbeat_sec:
            return float(self.settings.heartbeat_sec)
        cfg = self._registration.get("config") or {}
        timeout = float(cfg.get("heartbeat_timeout_sec") or 30)
        # Capped at 60s: the Sender's health tab must hear from every copier at least
        # once a minute, so a silent copier is obvious.
        return min(60.0, max(5.0, timeout / 3))

    def _terminal_health(self) -> tuple[bool, str, dict]:
        """Derive the copier's health from the executor state for the heartbeat. Healthy
        = running with the terminal usable (or the executor intentionally off). Logged
        out / disconnected while armed = unhealthy, so the Sender can warn."""
        try:
            h = self._health_provider() if self._health_provider else {}
        except Exception:  # noqa: BLE001
            h = {}
        enabled = bool(h.get("enabled"))
        connected = bool(h.get("connected"))
        logged_in = h.get("logged_in")
        terminal = {"executor_enabled": enabled, "connected": connected, "logged_in": logged_in}
        if not enabled:
            return True, "executor off (reception only)", terminal
        if not connected:
            return False, "terminal not connected", terminal
        if logged_in is False:
            return False, "TERMINAL LOGGED OUT - log in to Tradovate", terminal
        return True, "terminal connected + logged in", terminal

    # --- loop -------------------------------------------------------------------

    def run(self) -> None:  # pragma: no cover - the loop shell; cycle() is tested
        log.info("poller starting against %s", self.settings.sender_base_url)
        while not self.stop_event.is_set():
            try:
                self.cycle()
            except Exception:  # noqa: BLE001 - last resort: the reception thread must never die
                log.exception("cycle raised unexpectedly - continuing")
            try:
                interval = self._poll_interval()
            except Exception:  # noqa: BLE001
                interval = 3.0
            self.stop_event.wait(interval)
        log.info("poller stopped")

    def stop(self) -> None:
        self.stop_event.set()

    def cycle(self) -> None:
        """One reception pass. Never raises."""
        db = None
        try:
            db = self.session_factory()
            self._ensure_registered(db)

            try:
                self._heartbeat_if_due()
            except Exception as exc:  # noqa: BLE001 - a heartbeat hiccup must never block reception
                log.warning("heartbeat failed (still polling): %s", exc)

            cursor = kv_get(db, CURSOR_KEY)
            payload = self.client.commands(cursor)
            commands = payload.get("commands") or []
            next_cursor = payload.get("cursor")

            stats = None
            if commands:
                stats = process_batch(db, commands, self._registration)
            if next_cursor and next_cursor != cursor:
                kv_set(db, CURSOR_KEY, str(next_cursor))
            db.commit()

            if commands and self._notifier is not None:
                self._alert_caught(commands)

            if self._was_failing:
                log_event(db, "info", "sender", "Sender connection restored")
                db.commit()
                self._was_failing = False
                if self._lifecycle_alerts_on():
                    self._notifier.send(
                        "activity", f"✅ {self.settings.copier_name} — Sender connection restored")

            with self._lock:
                self._health.update({
                    "last_poll_at": _utcnow().isoformat(),
                    "last_poll_ok": True,
                    "consecutive_errors": 0,
                    "last_error": None,
                    "cursor": next_cursor or cursor,
                    "poll_sec": self._poll_interval(),
                    "cycles": self._health["cycles"] + 1,
                })
            if stats and (stats["received"] or stats["duplicates"]):
                log.info("poll: %s", stats)
        except Exception as exc:  # noqa: BLE001 - the loop must survive anything
            message = f"{type(exc).__name__}: {exc}"
            with self._lock:
                self._health.update({
                    "last_poll_at": _utcnow().isoformat(),
                    "last_poll_ok": False,
                    "consecutive_errors": self._health["consecutive_errors"] + 1,
                    "last_error": message[:300],
                    "cycles": self._health["cycles"] + 1,
                })
            transition = not self._was_failing
            if transition and db is not None:
                try:
                    db.rollback()
                    log_event(db, "error", "sender", f"Poll failed: {message[:300]}")
                    db.commit()
                except Exception:  # noqa: BLE001 - even event logging must not kill us
                    pass
            if transition and self._lifecycle_alerts_on():
                self._notifier.send(
                    "activity", f"⚠️ {self.settings.copier_name} — Sender unreachable ({message[:120]})")
            self._was_failing = True
            log.warning("poll cycle failed: %s", message)
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:  # noqa: BLE001
                    pass

    # --- registration ------------------------------------------------------------

    def _ensure_registered(self, db: Session) -> None:
        fresh_enough = (time.monotonic() - self._registered_at) < self.settings.register_refresh_sec
        if self._registration and fresh_enough:
            return

        try:
            registration = self.client.register()
        except Exception:  # noqa: BLE001 - any register failure (4xx/5xx OR network) falls back
            if self._registration:  # keep working with the cached copy during an outage
                return
            cached = kv_get(db, REGISTRATION_KEY)
            if cached:
                self._registration = json.loads(cached)
                self._set(registered=True, accounts=len(self._registration.get("accounts", [])))
                return
            raise

        first_time = not self._registration
        with self._lock:
            self._registration = registration
            self._health["registered"] = True
            self._health["accounts"] = len(registration.get("accounts", []))
        self._registered_at = time.monotonic()
        kv_set(db, REGISTRATION_KEY, json.dumps(registration))
        if first_time:
            names = [a.get("alias") or a.get("account_ref") for a in registration.get("accounts", [])]
            log_event(db, "info", "sender",
                      f"Registered with Sender as copier #{registration.get('copier_id')} - "
                      f"{len(names)} account(s): {', '.join(names) or 'none'}")
            if self._lifecycle_alerts_on():
                self._notifier.send(
                    "activity",
                    f"🔌 {self.settings.copier_name} registered with Sender as copier "
                    f"#{registration.get('copier_id')} — {len(names)} account(s): "
                    f"{', '.join(names) or 'none'}")
        db.commit()

    def _heartbeat_if_due(self) -> None:
        if (time.monotonic() - self._last_heartbeat) < self._heartbeat_interval():
            return
        healthy, detail, terminal = self._terminal_health()
        self.client.heartbeat("online", healthy=healthy, detail=detail, terminal=terminal)
        self._last_heartbeat = time.monotonic()
        self._set(last_heartbeat_at=_utcnow().isoformat(), healthy=healthy, health_detail=detail)

    def _lifecycle_alerts_on(self) -> bool:
        """Send the nice lifecycle activity pings UNLESS the firehose (telegram_activity_all)
        is on - in that mode every log_event is already mirrored, so these would duplicate."""
        return (self._notifier is not None
                and not getattr(self.settings, "telegram_activity_all", False))

    def _alert_caught(self, commands: list[dict]) -> None:
        """Best-effort Telegram alert that this copier caught signal(s). Never raises."""
        try:
            lines = []
            for c in commands[:10]:
                side = c.get("side") or ""
                desc = (f"{side} " if side else "") + (c.get("order_kind") or "?")
                lines.append(f"• {desc} {c.get('symbol')} x{c.get('base_qty', 1)} (#{c.get('signal_id')})")
            more = f"\n… +{len(commands) - 10} more" if len(commands) > 10 else ""
            self._notifier.send(
                "copier",
                f"📥 {self.settings.copier_name} caught {len(commands)} signal(s):\n"
                + "\n".join(lines) + more)
        except Exception:  # noqa: BLE001
            pass
