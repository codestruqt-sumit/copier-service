"""Part 3: the TerminalWorker - the ONLY thread that touches the trading terminal.

Serial by design: pick the oldest queued action, run the mandatory checks, execute,
validate, then report the outcome everywhere it matters (the local Action row +
events for the dashboard, and an execution report back to the Sender).

Mandatory chain before EVERY action:
    connected -> logged in -> not stale -> kill switch -> right account -> execute
(The symbol check lives inside every validated execution flow: tab switch with
expect_symbol verification, or the order ticket's own set-and-verify.)

Failure philosophy:
  * TRANSIENT doubt (not logged in, kill-switch state unknown) -> the action is HELD
    (stays queued) and retried; the staleness guard ultimately fails it loudly.
  * EXPLICIT stop (kill switch thrown, action too old) -> SKIPPED, reported, never run.
  * EXECUTION error -> FAILED with the terminal's own error, reported.
The worker itself can never die; problems degrade health and log events.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Action
from app.poller import kv_set
from app.processor import log_event

log = logging.getLogger("copier.executor")

TERMINAL_STATE_KEY = "terminal_state"

# action outcome -> the Sender's execution-report status
SENDER_STATUS = {"filled": "filled", "executing": "executing", "failed": "failed"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TerminalWorker(threading.Thread):
    def __init__(self, gateway, client, session_factory: sessionmaker, settings,
                 notifier=None):
        super().__init__(name="copier-executor", daemon=True)
        self.gateway = gateway
        self.client = client
        self.session_factory = session_factory
        self.settings = settings
        self._notifier = notifier                 # best-effort Telegram (non-blocking)
        # "terminal needs attention" (browser down OR logged out) alert tracking: fire on
        # entry + re-fire every login_alert_repeat_sec while it stays down.
        self._terminal_down_since: float | None = None
        self._last_terminal_alert: float = 0.0
        self.stop_event = threading.Event()

        self._lock = threading.Lock()
        self._health: dict = {
            "enabled": bool(getattr(settings, "executor_enabled", False)),
            "connected": False,
            "detail": "not started",
            "logged_in": None,
            "active_account": None,
            "last_action_at": None,
            "last_monitor_at": None,
            "last_refresh_at": None,
            "last_recycle_at": None,
            "last_restart_at": None,
            "last_accounts_at": None,
            "killswitch_age_sec": None,
            "current_action": None,     # {id, kind, symbol, account, started_at} while executing
            "done": 0, "failed": 0, "skipped": 0, "held": 0,
        }
        self._killswitch: dict[str, bool] = {}
        self._killswitch_at: float = 0.0
        self._last_monitor: float = 0.0
        # Cooperative abort of the CURRENT action: the dashboard sets the event; the
        # gateway polls it (plus the per-action deadline) at its checkpoints.
        self._abort_event = threading.Event()
        # periodic maintenance timers (first fire one interval from now)
        self._last_refresh: float = time.monotonic()
        self._last_driver_recycle: float = time.monotonic()
        self._last_browser_restart: float = time.monotonic()
        self._last_accounts: float = 0.0              # first accounts read is due at once
        self._forced_maint: str | None = None         # dashboard-requested recycle/restart
        self._maint_lock = threading.Lock()
        self._was_disconnected = False

    # --- health for the dashboard ---------------------------------------------

    def health(self) -> dict:
        with self._lock:
            snapshot = dict(self._health)
        age = (time.monotonic() - self._killswitch_at) if self._killswitch_at else None
        snapshot["killswitch_age_sec"] = round(age, 1) if age is not None else None
        return snapshot

    def _set(self, **fields) -> None:
        with self._lock:
            self._health.update(fields)

    def _set_logged_in(self, logged_in, detail: str = "") -> None:
        """Track login state. Logged-out routes to the unified terminal-down alert (fires
        immediately + repeats while down); login clears it."""
        self._set(logged_in=logged_in)
        if logged_in is False:
            self._log("warn", "terminal",
                      f"TERMINAL LOGGED OUT - needs login ({detail})".strip(), dedupe_last=True)
            self._warn_terminal(f"terminal LOGGED OUT — needs login ({detail})".strip())
        elif logged_in:
            self._clear_terminal_down()

    def _warn_terminal(self, reason: str) -> None:
        """Telegram 'warnings' alert that the terminal needs attention - browser closed/
        down OR logged out. Fires the MOMENT the executor detects it, then re-fires every
        login_alert_repeat_sec while it stays down, so a single missed/dropped alert is
        always followed up. Non-blocking + swallowed. Called from the login check and the
        connect check, so BOTH 'browser closed' and 'logged out' reliably alert."""
        now = time.monotonic()
        first = self._terminal_down_since is None
        if first:
            self._terminal_down_since = now
        repeat = float(getattr(self.settings, "login_alert_repeat_sec", 300.0) or 0)
        due = first or (repeat > 0 and (now - self._last_terminal_alert) >= repeat)
        if not due or self._notifier is None:
            return
        self._last_terminal_alert = now
        name = getattr(self.settings, "copier_name", "copier")
        tail = "" if first else f" (still down ~{int((now - self._terminal_down_since) / 60)}m)"
        self._notifier.send("warnings", f"🔐 {name}: {reason}{tail} — log in / check the VM.")

    def _clear_terminal_down(self) -> None:
        """Terminal usable again - reset the alert state (silent; no all-clear spam)."""
        self._terminal_down_since = None

    def _bump(self, counter: str) -> None:
        with self._lock:
            self._health[counter] += 1

    # --- loop -------------------------------------------------------------------

    def run(self) -> None:  # pragma: no cover - loop shell; step() is tested
        log.info("terminal worker starting (enabled=%s)", self.settings.executor_enabled)
        try:
            n = self.reconcile_interrupted()
            if n:
                log.warning("reconciled %d interrupted action(s) from a previous run", n)
        except Exception:  # noqa: BLE001 - never block startup on reconciliation
            log.exception("startup reconciliation failed - continuing")
        while not self.stop_event.is_set():
            try:
                outcome = self.step()
            except Exception:  # noqa: BLE001 - the worker must never die
                log.exception("executor step raised unexpectedly - continuing")
                outcome = "error"
            self.stop_event.wait(self._delay(outcome))
        log.info("terminal worker stopped")

    def stop(self) -> None:
        self.stop_event.set()

    def _delay(self, outcome: str) -> float:
        """Pace the loop by what just happened. 'acted'/'idle'/'refreshed' are the hot
        path and are tunable (exec_acted_sec / exec_idle_sec) so a VM can trade off
        snappiness vs CPU; 'held'/'disabled' back off hard (nothing useful to do),
        'error' waits 1s."""
        acted = float(getattr(self.settings, "exec_acted_sec", 0.2))
        idle = float(getattr(self.settings, "exec_idle_sec", 0.25))
        return {"acted": acted, "refreshed": acted, "recycled": acted, "restarted": acted,
                "idle": idle, "held": 5.0, "disabled": 5.0}.get(outcome, 1.0)

    # --- terminal maintenance: forced (dashboard) + periodic (timers) -----------------

    def request_abort(self) -> tuple[bool, str]:
        """Operator control: stop the CURRENTLY EXECUTING action at its next gateway
        checkpoint (between operations - never mid-click). The action finishes as
        FAILED with a verify-manually note and is never re-sent. No effect when
        nothing is executing (queued actions are handled by /api/queue/flush)."""
        current = self.health().get("current_action")
        if not current:
            return False, "no action is executing right now (use 'Clear queue' for queued ones)"
        self._abort_event.set()
        return True, (f"abort requested for action #{current.get('id')} "
                      f"{current.get('kind')} {current.get('symbol')} - it stops at the "
                      f"next safe checkpoint and fails loudly (verify the terminal)")

    def _abort_reason(self, deadline: float | None) -> str | None:
        """The gateway checkpoint callback for the current action: operator abort wins,
        then the per-action timeout."""
        if self._abort_event.is_set():
            return "aborted by operator"
        if deadline is not None and time.monotonic() > deadline:
            timeout = float(getattr(self.settings, "action_timeout_sec", 0) or 0)
            return f"action timeout ({int(timeout)}s) exceeded"
        return None

    def request_maintenance(self, kind: str) -> bool:
        """Queue a forced 'recycle' or 'restart' from the dashboard; the worker performs
        it on its own thread, between actions. Returns False for an unknown kind."""
        if kind not in ("recycle", "restart"):
            return False
        with self._maint_lock:
            self._forced_maint = kind
        return True

    def _take_forced(self) -> str | None:
        with self._maint_lock:
            kind, self._forced_maint = self._forced_maint, None
        return kind

    def _maybe_maintain(self) -> str | None:
        """Run at most one DUE periodic maintenance, heaviest first (a heavier op
        subsumes the lighter ones). Returns the outcome, or None if nothing was due."""
        now = time.monotonic()

        def due(last: float, key: str) -> bool:
            sec = float(getattr(self.settings, key, 0) or 0)
            return sec > 0 and (now - last) >= sec

        if due(self._last_browser_restart, "browser_restart_sec"):
            return self._do_browser_restart()
        if due(self._last_driver_recycle, "driver_recycle_sec"):
            return self._do_driver_recycle()
        if due(self._last_refresh, "tab_refresh_sec"):
            return self._do_refresh()
        return None

    def _do_driver_recycle(self) -> str:
        self._last_driver_recycle = time.monotonic()
        ok, detail = self.gateway.recycle_driver()
        self._set(last_recycle_at=_utcnow().isoformat())
        self._log("info" if ok else "warn", "terminal",
                  f"Driver recycle: {detail}", dedupe_last=not ok)
        if not ok:
            self._set(connected=False)
        return "recycled"

    def _do_browser_restart(self, forced: bool = False) -> str:
        # a full restart also freshens the driver + page, so reset those timers too
        now = time.monotonic()
        self._last_browser_restart = now
        self._last_driver_recycle = now
        self._last_refresh = now
        ok, detail = self.gateway.restart_browser()
        self._set(last_restart_at=_utcnow().isoformat())
        self._log("info" if ok else "error", "terminal",
                  f"{'Forced browser' if forced else 'Browser'} restart: {detail}")
        if not ok:
            self._set(connected=False)
        return "restarted"

    def _do_refresh(self) -> str:
        """Reload the terminal tab to clear UI wedging (e.g. a stuck Account widget).
        Runs as its own serialized step - no action executes during it."""
        self._last_refresh = time.monotonic()
        ok, detail = self.gateway.refresh_tab()
        self._set(last_refresh_at=_utcnow().isoformat())
        if ok:
            self._log("info", "terminal", f"Periodic tab refresh: {detail}")
        else:
            self._log("warn", "terminal", f"Tab refresh failed: {detail}", dedupe_last=True)
            self._set(connected=False)   # force a clean re-attach on the next step
        return "refreshed"

    def reconcile_interrupted(self) -> int:
        """A process kill can leave an action stuck in 'executing' - we committed that
        status (step 167) then died before recording the outcome. step() only ever
        picks up 'queued', so such an action would hang forever, neither retried nor
        failed. On startup, FAIL those loudly. We deliberately do NOT auto-retry: the
        order may or may not have reached the terminal, and a blind retry could
        double-place - so a human must verify on the terminal."""
        db = self.session_factory()
        count = 0
        try:
            orphans = list(db.scalars(select(Action).where(Action.status == "executing")))
            for a in orphans:
                a.status = "failed"
                a.note = ("interrupted by a copier restart - the order may or may not "
                          "have been placed; VERIFY on the terminal (not retried)")[:600]
                self._log("error", "executor",
                          f"action #{a.id} {a.kind} {a.symbol} on "
                          f"{a.account_alias or a.account_ref}: INTERRUPTED by restart - "
                          "VERIFY on the terminal (not retried)", db=db, commit=False)
                self._report(a, "failed")
                count += 1
            if count:
                db.commit()
        finally:
            db.close()
        return count

    def step(self) -> str:
        """One iteration. Returns what happened (drives the loop's pacing)."""
        # Forced maintenance (dashboard buttons) runs regardless of the enabled gate, so
        # it works even with the executor off. A restart relaunches the browser itself; a
        # recycle needs an attached driver first.
        forced = self._take_forced()
        if forced == "restart":
            return self._do_browser_restart(forced=True)
        if forced == "recycle":
            ok, detail = self.gateway.ensure_connected()
            self._set(connected=ok, detail=detail)
            if not ok:
                self._log("warn", "terminal", f"forced recycle - terminal unavailable: {detail}")
                return "held"
            return self._do_driver_recycle()

        if not self.settings.executor_enabled:
            self._set(enabled=False, detail="executor disabled (EXECUTOR_ENABLED=false)")
            return "disabled"
        self._set(enabled=True)

        ok, detail = self.gateway.ensure_connected()
        self._set(connected=ok, detail=detail)
        if not ok:
            self._note_disconnect(detail)
            return "held"
        if self._was_disconnected:
            self._log("info", "terminal", f"Terminal connected - {detail}")
            self._was_disconnected = False

        # Periodic maintenance (refresh / driver recycle / browser restart) is a
        # serialized step of its own, so it only ever runs BETWEEN actions (never
        # mid-order) and no action executes while it happens.
        maint = self._maybe_maintain()
        if maint:
            return maint

        db = self.session_factory()
        try:
            action = db.scalar(
                select(Action).where(Action.status == "queued").order_by(Action.id).limit(1)
            )
            if action is not None:
                return self._run_action(db, action)
            self._idle_duties(db)
            return "idle"
        finally:
            db.close()

    # --- one action -----------------------------------------------------------------

    def _run_action(self, db: Session, action: Action) -> str:
        label = (f"action #{action.id} {action.kind} {action.symbol} "
                 f"x{action.qty or '-'} on {action.account_alias or action.account_ref}")

        # 1) staleness guard - old actions must never fire late
        age = (_utcnow() - self._aware(action.created_at)).total_seconds()
        if age > self.settings.max_action_age_sec:
            return self._finish(db, action, "skipped",
                                f"stale: queued {int(age)}s ago (limit "
                                f"{int(self.settings.max_action_age_sec)}s) - never executed",
                                sender_status="skipped")

        # 2) login check (forced, every action)
        logged_in, login_detail = self.gateway.login_check()
        self._set_logged_in(logged_in, login_detail)
        if not logged_in:
            self._hold(db, f"{label} HELD - terminal not logged in ({login_detail})")
            return "held"

        # 3) kill switch - explicit block skips, unknown state holds
        allowed = self._killswitch_state(db, action.account_ref)
        if allowed is None:
            self._hold(db, f"{label} HELD - kill-switch state unknown (Sender unreachable)")
            return "held"
        if not allowed:
            return self._finish(db, action, "skipped",
                                "kill switch is thrown for this account - not executed",
                                sender_status="skipped")

        # mark executing only after the gates that do not consume the action
        action.status = "executing"
        db.commit()
        self._log("info", "executor", f"Executing {label}")
        self._report(action, "executing")

        # Arm the cooperative abort/timeout for THIS action: the gateway polls
        # _abort_reason at its checkpoints (loop boundaries, never mid-click).
        timeout = float(getattr(self.settings, "action_timeout_sec", 0) or 0)
        deadline = (time.monotonic() + timeout) if timeout > 0 else None
        self._abort_event.clear()
        self._set(current_action={"id": action.id, "kind": action.kind,
                                  "symbol": action.symbol,
                                  "account": action.account_alias or action.account_ref,
                                  "started_at": _utcnow().isoformat()})
        self.gateway.abort_check = lambda: self._abort_reason(deadline)

        try:
            # 4) account check (switch if needed)
            ok, detail = self.gateway.ensure_account(action.account_ref)
            if not ok:
                return self._finish(db, action, "failed", detail, sender_status="failed")

            # 5) execute (symbol check + validation live inside the validated flows)
            result = self.gateway.execute(self._action_dict(action))
        finally:
            self.gateway.abort_check = None
            self._abort_event.clear()
            self._set(current_action=None)
        outcome = result.get("outcome", "failed")
        note = result.get("detail") or ""
        action.order_ref = result.get("order_ref")

        status = "done" if outcome in ("filled", "executing") else "failed"
        self._finish(db, action, status, note, sender_status=SENDER_STATUS.get(outcome, "failed"),
                     fill_like=(outcome == "filled"))

        # opportunistic state capture - this account is active right now
        self._capture_state(db)
        self._maybe_report_accounts()   # keep the consolidated PnL fresh during trading
        self._set(last_action_at=_utcnow().isoformat(),
                  active_account=self.gateway.active_account() if status == "done" else
                  self._health.get("active_account"))
        return "acted"

    # --- helpers -----------------------------------------------------------------------

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def _action_dict(self, action: Action) -> dict:
        return {"kind": action.kind, "symbol": action.symbol, "side": action.side,
                "qty": action.qty, "limit_price": action.limit_price,
                "stop_price": action.stop_price, "tif": action.tif}

    def _finish(self, db: Session, action: Action, status: str, note: str,
                *, sender_status: str, fill_like: bool = False) -> str:
        action.status = status
        action.note = (note or "")[:600]
        level = "info" if status == "done" else ("warn" if status == "skipped" else "error")
        self._log(level, "executor",
                  f"action #{action.id} {action.kind} {action.symbol} on "
                  f"{action.account_alias or action.account_ref}: {status.upper()} - {note}",
                  db=db, commit=False)
        db.commit()
        self._bump({"done": "done", "failed": "failed", "skipped": "skipped"}.get(status, "failed"))
        self._report(action, sender_status)
        if status == "failed" and self._notifier is not None:
            name = getattr(self.settings, "copier_name", "copier")
            self._notifier.send(
                "errors",
                f"❌ {name}: {action.kind} {action.symbol} on "
                f"{action.account_alias or action.account_ref} FAILED — {note}")
        return "acted"

    def _hold(self, db: Session, message: str) -> None:
        """Leave the action queued (retry later) - loud once per condition change."""
        self._bump("held")
        self._log("warn", "executor", message, db=db, dedupe_last=True)

    def _post(self, fn, what: str) -> None:
        """Run a Sender POST without blocking the executor: fire-and-forget on a daemon
        thread (sender_post_async, default on). Payloads are ALWAYS built on the caller
        thread first - the thread only performs the HTTP call (httpx.Client is
        thread-safe). Failures are logged, never raised."""
        def run() -> None:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                log.warning("%s post to sender failed (%s) - continuing", what, exc)
        if getattr(self.settings, "sender_post_async", True):
            threading.Thread(target=run, name=f"post-{what}", daemon=True).start()
        else:
            run()

    def _report(self, action: Action, status: str) -> None:
        """Execution feedback to the Sender - must never break or DELAY the worker.
        The payload is snapshotted here (caller thread) so the async post never touches
        the ORM object."""
        payload = {
            "signal_id": action.signal_id,
            "revision": action.revision,
            "account_ref": action.account_ref,
            "status": status,
            "resolved_qty": action.qty,
            "order_ref": action.order_ref,
            "error": action.note if status in ("failed", "skipped") else None,
        }
        self._post(lambda: self.client.post_reports([payload]), "report")

    def _killswitch_state(self, db: Session, account_ref: str) -> bool | None:
        """Fresh-enough kill-switch state. None = unknown (caller must HOLD)."""
        now = time.monotonic()
        if (now - self._killswitch_at) > self.settings.killswitch_ttl_sec:
            try:
                payload = self.client.killswitch()
                self._killswitch = dict(payload.get("accounts") or {})
                self._killswitch_at = now
            except Exception as exc:  # noqa: BLE001
                if (now - self._killswitch_at) > self.settings.killswitch_stale_block_sec:
                    log.warning("kill-switch fetch failed and cache is stale: %s", exc)
                    return None
        # unknown account: default BLOCKED - never trade an account the Sender
        # does not currently confirm as allowed.
        return bool(self._killswitch.get(account_ref, False)) if self._killswitch else None

    # --- idle duties: monitoring + keep-alive ----------------------------------------

    def _queue_has_work(self, db: Session) -> bool:
        try:
            return db.scalar(
                select(Action.id).where(Action.status == "queued").limit(1)) is not None
        except Exception:  # noqa: BLE001 - a read hiccup must not break the loop
            return False

    def _idle_duties(self, db: Session) -> None:
        """Monitoring between actions. Each sub-step re-checks the queue and BAILS if a
        signal arrived, so a fresh action is never stuck behind a monitoring read - at
        1s cadences this was adding up to ~1.5s to pickup latency."""
        interval = float(self.settings.state_poll_sec or 15)
        if (time.monotonic() - self._last_monitor) >= interval:
            logged_in, login_detail = self.gateway.login_check()
            self._set_logged_in(logged_in, login_detail)
            if logged_in:
                self._capture_state(db)
            self._last_monitor = time.monotonic()
            self._set(last_monitor_at=_utcnow().isoformat())
            # STAGGER (2026-08-25): never run both heavy DOM reads (state + accounts) in
            # one pass - back-to-back reads at 1s cadences kept the SPA busy enough to
            # slow order clicks (+5s per action, measured). The accounts read runs on the
            # next pass, ~exec_idle_sec later.
            return
        if self._queue_has_work(db):
            return
        self._maybe_report_accounts()   # all-accounts PnL (own cadence; cheap DOM read)
        if self._queue_has_work(db):
            return
        self.gateway.keepalive()  # self-throttled; harmless mouse-move + zero scroll

    def _maybe_report_accounts(self) -> None:
        """Read the 'Accounts' widget (all accounts' Open/Total P/L + Net Liq) and report
        it to the Sender, on its own cadence (accounts_pnl_sec). Self-throttled so it is
        safe to call after every action too. Pure DOM read - never disturbs the terminal,
        never a network call, so it can't hit rate limits."""
        interval = float(getattr(self.settings, "accounts_pnl_sec", 0) or 0)
        if interval <= 0 or (time.monotonic() - self._last_accounts) < interval:
            return
        self._last_accounts = time.monotonic()
        try:
            rows = self.gateway.read_accounts_summary()
        except Exception as exc:  # noqa: BLE001
            log.warning("accounts read failed: %s", exc)
            return
        if not rows:
            return
        self._set(last_accounts_at=_utcnow().isoformat())
        self._post(lambda: self.client.post_accounts(rows), "accounts")

    def _capture_state(self, db: Session) -> None:
        try:
            state = self.gateway.read_state()
            state["captured_at"] = _utcnow().isoformat()
            kv_set(db, TERMINAL_STATE_KEY, json.dumps(state))
            db.commit()
            self._set(active_account=state.get("account"))
            account_ref = state.get("account")
            if account_ref:
                payload = {
                    "account_ref": account_ref,
                    "positions": state.get("positions") or [],
                    "working_orders": state.get("working_orders") or [],
                }
                self._post(lambda: self.client.post_state([payload]), "state")
        except Exception as exc:  # noqa: BLE001
            log.warning("terminal state capture failed: %s", exc)

    # --- event logging with its own session safety -------------------------------------

    _last_warn: str = ""

    def _note_disconnect(self, detail: str) -> None:
        if not self._was_disconnected:
            self._log("error", "terminal", f"Terminal unavailable: {detail}")
            self._was_disconnected = True
        # Browser closed / not attached also needs the operator - same reliable alert path
        # (fires now + repeats while down), so a closed browser is never silent.
        self._warn_terminal(f"terminal not reachable — {detail} (browser closed?)")

    def _log(self, level: str, category: str, message: str,
             db: Session | None = None, commit: bool = True, dedupe_last: bool = False) -> None:
        if dedupe_last and message == self._last_warn:
            return
        self._last_warn = message if dedupe_last else self._last_warn
        own = db is None
        session = db or self.session_factory()
        try:
            log_event(session, level, category, message)
            if commit or own:
                session.commit()
        except Exception:  # noqa: BLE001 - logging must never break execution
            try:
                session.rollback()
            except Exception:  # noqa: BLE001
                pass
        finally:
            if own:
                session.close()
