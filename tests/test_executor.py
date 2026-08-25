"""Part 3 executor logic against a FAKE terminal gateway (no Selenium needed).

Covers the mandatory chain (connect -> login -> staleness -> kill switch -> account
-> execute), the hold-vs-skip-vs-fail philosophy, lifecycle reporting to the Sender,
serial ordering, and the idle duties (monitoring + keep-alive).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.executor import TerminalWorker
from app.models import Action


class FakeGateway:
    def __init__(self):
        self.available = True
        self.calls: list = []
        self.connect_ok = True
        self.login_ok = True
        self.fail_switch = False
        self.account = None
        self.execute_result = {"outcome": "filled", "order_ref": None, "detail": "net 0 -> 2"}

    def ensure_connected(self):
        self.calls.append("connect")
        return (self.connect_ok, "fake terminal")

    def login_check(self):
        self.calls.append("login")
        return (self.login_ok, "fake session")

    def active_account(self):
        return self.account

    def ensure_account(self, ref):
        self.calls.append(("account", ref))
        if self.fail_switch:
            return False, "account switch failed: not in menu"
        self.account = ref
        return True, f"switched -> {ref}"

    def execute(self, action):
        self.calls.append(("execute", action["kind"], action["symbol"]))
        return dict(self.execute_result)

    def read_state(self):
        self.calls.append("read_state")
        return {"account": self.account or "REF1", "positions": [], "working_orders": []}

    def keepalive(self):
        self.calls.append("keepalive")
        return True

    def refresh_tab(self, settle_timeout=25.0):
        self.calls.append("refresh_tab")
        return (True, "reloaded, logged in")

    def read_accounts_summary(self):
        self.calls.append("read_accounts_summary")
        return getattr(self, "accounts_rows", [])

    def recycle_driver(self):
        self.calls.append("recycle_driver")
        return (True, "re-attached")

    def restart_browser(self, settle_timeout=30.0):
        self.calls.append("restart_browser")
        return (True, "browser restarted")


def worker_settings(**overrides):
    base = dict(
        executor_enabled=True,
        max_action_age_sec=300.0,
        killswitch_ttl_sec=0.0,          # always fetch fresh - deterministic tests
        killswitch_stale_block_sec=60.0,
        state_poll_sec=0.0,              # monitor on every idle step
        sender_post_async=False,         # tests assert report ordering synchronously
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def gateway():
    return FakeGateway()


@pytest.fixture
def worker(gateway, sender_client, session_factory):
    return TerminalWorker(gateway, sender_client, session_factory, worker_settings())


def queue_action(session_factory, **overrides) -> int:
    row = dict(signal_id=1, revision=1, account_ref="REF1", account_alias="Acc-1",
               kind="place_market", symbol="MNQU6", side="buy", qty=2, tif="day",
               status="queued")
    row.update(overrides)
    db = session_factory()
    try:
        action = Action(**row)
        db.add(action)
        db.commit()
        return action.id
    finally:
        db.close()


def get_action(session_factory, action_id):
    db = session_factory()
    try:
        return db.get(Action, action_id)
    finally:
        db.close()


# --- basics -------------------------------------------------------------------------

def test_disabled_executor_touches_nothing(gateway, sender_client, session_factory):
    worker = TerminalWorker(gateway, sender_client, session_factory,
                            worker_settings(executor_enabled=False))
    assert worker.step() == "disabled"
    assert gateway.calls == []


def test_market_action_end_to_end(worker, gateway, fake_sender, session_factory):
    action_id = queue_action(session_factory)
    assert worker.step() == "acted"

    action = get_action(session_factory, action_id)
    assert action.status == "done"
    assert "net 0 -> 2" in action.note

    # mandatory order: connect -> login -> (killswitch via sender) -> account -> execute
    kinds = [c if isinstance(c, str) else c[0] for c in gateway.calls]
    assert kinds.index("login") < kinds.index("account") < kinds.index("execute")
    assert ("account", "REF1") in gateway.calls

    # lifecycle reported to the Sender: picked (executing) then the outcome (filled)
    statuses = [r["status"] for r in fake_sender.reports]
    assert statuses == ["executing", "filled"]
    assert fake_sender.reports[-1]["account_ref"] == "REF1"


def test_resting_order_reports_executing_with_ref(worker, gateway, fake_sender, session_factory):
    gateway.execute_result = {"outcome": "executing", "order_ref": "625128610250",
                              "detail": "LIMIT working @ 28000"}
    action_id = queue_action(session_factory, kind="place_limit", limit_price="28000.00")
    worker.step()
    action = get_action(session_factory, action_id)
    assert action.status == "done"
    assert action.order_ref == "625128610250"
    assert [r["status"] for r in fake_sender.reports] == ["executing", "executing"]
    assert fake_sender.reports[-1]["order_ref"] == "625128610250"


def test_execution_failure_is_loud(worker, gateway, fake_sender, session_factory):
    gateway.execute_result = {"outcome": "failed", "order_ref": None,
                              "detail": "position did not move as expected"}
    action_id = queue_action(session_factory)
    worker.step()
    action = get_action(session_factory, action_id)
    assert action.status == "failed"
    assert "did not move" in action.note
    assert [r["status"] for r in fake_sender.reports] == ["executing", "failed"]
    assert worker.health()["failed"] == 1


def test_account_switch_failure_fails_the_action(worker, gateway, fake_sender, session_factory):
    gateway.fail_switch = True
    action_id = queue_action(session_factory)
    worker.step()
    action = get_action(session_factory, action_id)
    assert action.status == "failed"
    assert "switch failed" in action.note
    assert not any(isinstance(c, tuple) and c[0] == "execute" for c in gateway.calls)


# --- the safety gates ------------------------------------------------------------------

def test_kill_switch_blocks_execution(worker, gateway, fake_sender, session_factory):
    fake_sender.killswitch_map["REF1"] = False
    action_id = queue_action(session_factory)
    worker.step()
    action = get_action(session_factory, action_id)
    assert action.status == "skipped"
    assert "kill switch" in action.note
    assert not any(isinstance(c, tuple) and c[0] == "execute" for c in gateway.calls)
    assert [r["status"] for r in fake_sender.reports] == ["skipped"]


def test_unknown_killswitch_state_holds_not_skips(worker, gateway, fake_sender, session_factory):
    fake_sender.fail_killswitch = True
    action_id = queue_action(session_factory)
    assert worker.step() == "held"
    action = get_action(session_factory, action_id)
    assert action.status == "queued"          # NOT consumed - retried when state is known
    assert fake_sender.reports == []
    assert not any(isinstance(c, tuple) and c[0] == "execute" for c in gateway.calls)


def test_not_logged_in_holds_the_action(worker, gateway, fake_sender, session_factory):
    gateway.login_ok = False
    action_id = queue_action(session_factory)
    assert worker.step() == "held"
    assert get_action(session_factory, action_id).status == "queued"
    assert worker.health()["logged_in"] is False


def test_stale_action_is_skipped_never_executed(worker, gateway, fake_sender, session_factory):
    old = datetime.now(timezone.utc) - timedelta(seconds=3600)
    action_id = queue_action(session_factory, created_at=old)
    worker.step()
    action = get_action(session_factory, action_id)
    assert action.status == "skipped"
    assert "stale" in action.note
    assert [r["status"] for r in fake_sender.reports] == ["skipped"]
    assert not any(isinstance(c, tuple) and c[0] == "execute" for c in gateway.calls)


def test_terminal_down_holds_everything(worker, gateway, fake_sender, session_factory):
    gateway.connect_ok = False
    action_id = queue_action(session_factory)
    assert worker.step() == "held"
    assert get_action(session_factory, action_id).status == "queued"
    assert worker.health()["connected"] is False


# --- ordering + idle duties ---------------------------------------------------------------

def test_actions_execute_serially_in_fifo_order(worker, gateway, fake_sender, session_factory):
    first = queue_action(session_factory, signal_id=1, symbol="MNQU6")
    second = queue_action(session_factory, signal_id=2, symbol="MGCZ6")
    worker.step()
    worker.step()
    executes = [c for c in gateway.calls if isinstance(c, tuple) and c[0] == "execute"]
    assert [c[2] for c in executes] == ["MNQU6", "MGCZ6"]
    assert get_action(session_factory, first).status == "done"
    assert get_action(session_factory, second).status == "done"


def test_idle_monitoring_and_keepalive(worker, gateway, fake_sender, session_factory):
    assert worker.step() == "idle"            # monitor pass (staggered: returns early)
    assert "read_state" in gateway.calls
    assert fake_sender.states and fake_sender.states[0]["account_ref"] == "REF1"
    assert worker.health()["last_monitor_at"] is not None
    # STAGGERED idle duties: accounts/keepalive run on a pass where the monitor is not
    # due (never both heavy DOM reads in one pass).
    assert "keepalive" not in gateway.calls
    worker.settings.state_poll_sec = 999.0    # monitor no longer due
    assert worker.step() == "idle"
    assert "keepalive" in gateway.calls


# --- crash recovery -----------------------------------------------------------------------

def test_reconcile_interrupted_fails_orphans_without_retry(
        worker, gateway, fake_sender, session_factory):
    """A process kill can leave an action stuck in 'executing'. Startup reconciliation
    fails it loudly and reports it - it must never be silently stuck, and never
    auto-retried (that could double-place). The terminal is not touched."""
    orphan = queue_action(session_factory, status="executing")
    still_queued = queue_action(session_factory, signal_id=2, status="queued")

    assert worker.reconcile_interrupted() == 1

    a = get_action(session_factory, orphan)
    assert a.status == "failed"
    assert "interrupted" in a.note.lower() and "verify" in a.note.lower()
    assert get_action(session_factory, still_queued).status == "queued"   # untouched
    assert [r["status"] for r in fake_sender.reports] == ["failed"]
    assert gateway.calls == []                                            # terminal never touched


def test_reconcile_interrupted_noop_when_clean(worker, gateway, fake_sender, session_factory):
    queue_action(session_factory, status="queued")
    assert worker.reconcile_interrupted() == 0
    assert fake_sender.reports == []


def test_delay_pacing_is_tunable(gateway, sender_client, session_factory):
    w = TerminalWorker(gateway, sender_client, session_factory,
                       worker_settings(exec_acted_sec=0.05, exec_idle_sec=0.1))
    assert w._delay("acted") == 0.05      # hot path: snappy after acting
    assert w._delay("refreshed") == 0.05  # resume promptly after a refresh
    assert w._delay("recycled") == 0.05 and w._delay("restarted") == 0.05
    assert w._delay("idle") == 0.1        # hot path: quick queue re-check
    assert w._delay("held") == 5.0        # back off hard
    assert w._delay("error") == 1.0       # unmapped -> 1s


# --- periodic tab refresh ---------------------------------------------------------------

def test_periodic_refresh_is_a_serialized_step(worker, gateway, fake_sender, session_factory):
    """When a refresh is due it runs as its OWN step - between actions, never mid-order,
    and no order executes that iteration. The next step resumes normally."""
    import time as _t

    action_id = queue_action(session_factory)
    worker.settings.tab_refresh_sec = 60.0
    worker._last_refresh = _t.monotonic() - 120.0        # last refresh 2 min ago -> due

    assert worker.step() == "refreshed"
    assert "refresh_tab" in gateway.calls
    assert not any(isinstance(c, tuple) and c[0] == "execute" for c in gateway.calls)
    assert get_action(session_factory, action_id).status == "queued"   # nothing ran
    assert worker.health()["last_refresh_at"] is not None

    # no longer due -> the waiting action now executes
    assert worker.step() == "acted"
    assert get_action(session_factory, action_id).status == "done"


def test_refresh_disabled_when_interval_zero(worker, gateway, session_factory):
    worker.settings.tab_refresh_sec = 0.0
    worker.settings.driver_recycle_sec = 0.0
    worker.settings.browser_restart_sec = 0.0
    worker._last_refresh = 0.0
    assert worker.step() == "idle"                       # empty queue, no maintenance
    assert "refresh_tab" not in gateway.calls


# --- driver recycle / browser restart maintenance --------------------------------------

def test_forced_recycle_runs_as_a_step(worker, gateway, session_factory):
    assert worker.request_maintenance("recycle") is True
    assert worker.step() == "recycled"
    assert "recycle_driver" in gateway.calls
    assert worker.health()["last_recycle_at"] is not None


def test_forced_restart_works_even_when_executor_off(gateway, sender_client, session_factory):
    w = TerminalWorker(gateway, sender_client, session_factory,
                       worker_settings(executor_enabled=False))
    w.request_maintenance("restart")
    assert w.step() == "restarted"                       # forced runs before the enabled gate
    assert "restart_browser" in gateway.calls


def test_request_maintenance_rejects_unknown(worker):
    assert worker.request_maintenance("nope") is False


def test_periodic_driver_recycle_when_due(worker, gateway, session_factory):
    import time as _t
    worker.settings.driver_recycle_sec = 60.0
    worker._last_driver_recycle = _t.monotonic() - 120.0
    assert worker.step() == "recycled"
    assert "recycle_driver" in gateway.calls


def test_maintenance_priority_restart_beats_recycle_and_refresh(worker, gateway, session_factory):
    import time as _t
    worker.settings.browser_restart_sec = 60.0
    worker.settings.driver_recycle_sec = 60.0
    worker.settings.tab_refresh_sec = 60.0
    old = _t.monotonic() - 120.0
    worker._last_browser_restart = worker._last_driver_recycle = worker._last_refresh = old
    assert worker.step() == "restarted"                  # heaviest wins
    assert "restart_browser" in gateway.calls and "recycle_driver" not in gateway.calls


# --- consolidated accounts PnL ----------------------------------------------------------

def test_accounts_pnl_reported_on_cadence(gateway, sender_client, session_factory, fake_sender):
    gateway.accounts_rows = [{"account": "REF1", "open_pl": "$0.00",
                              "total_pl": "$5.00", "net_liq": "$100.00"}]
    w = TerminalWorker(gateway, sender_client, session_factory,
                       worker_settings(accounts_pnl_sec=1.0))
    w._last_accounts = 0.0                              # due now
    w._maybe_report_accounts()
    assert [a["account"] for a in fake_sender.accounts] == ["REF1"]
    assert w.health()["last_accounts_at"] is not None
    # self-throttled: an immediate second call does nothing (cadence not elapsed)
    w._maybe_report_accounts()
    assert len(fake_sender.accounts) == 1


def test_accounts_pnl_disabled_when_zero(gateway, sender_client, session_factory, fake_sender):
    w = TerminalWorker(gateway, sender_client, session_factory,
                       worker_settings(accounts_pnl_sec=0.0))
    w._last_accounts = 0.0
    w._maybe_report_accounts()
    assert fake_sender.accounts == []
    assert "read_accounts_summary" not in gateway.calls


# --- abort + per-action timeout (cooperative, checkpoint-based) --------------------------

class CheckpointGateway(FakeGateway):
    """A gateway whose execute() honors abort_check the way the real one does: it polls
    at 'checkpoints' and returns the ABORTED failure dict when a reason appears."""

    def __init__(self, spin_sec: float = 3.0):
        super().__init__()
        self.abort_check = None
        self.spin_sec = spin_sec

    def execute(self, action):
        import time as _t
        deadline = _t.monotonic() + self.spin_sec
        while _t.monotonic() < deadline:
            reason = self.abort_check() if self.abort_check else None
            if reason:
                return {"outcome": "failed", "order_ref": None,
                        "detail": f"ABORTED: {reason} - an order may already be on the "
                                  f"terminal; VERIFY manually (not retried)"}
            _t.sleep(0.05)
        return dict(self.execute_result)


def test_action_timeout_aborts_a_looping_action(sender_client, session_factory, fake_sender):
    gw = CheckpointGateway(spin_sec=5.0)
    worker = TerminalWorker(gw, sender_client, session_factory,
                            worker_settings(action_timeout_sec=0.3))
    action_id = queue_action(session_factory)
    worker.step()
    action = get_action(session_factory, action_id)
    assert action.status == "failed"
    assert "timeout" in action.note and "VERIFY" in action.note
    assert [r["status"] for r in fake_sender.reports] == ["executing", "failed"]
    assert worker.health()["current_action"] is None        # cleared after the action


def test_operator_abort_stops_current_action(sender_client, session_factory, fake_sender):
    import threading as _th

    gw = CheckpointGateway(spin_sec=5.0)
    worker = TerminalWorker(gw, sender_client, session_factory,
                            worker_settings(action_timeout_sec=0.0))  # timeout off
    action_id = queue_action(session_factory)
    # the "operator" clicks Abort shortly after execution starts
    _th.Timer(0.2, worker.request_abort).start()
    worker.step()
    action = get_action(session_factory, action_id)
    assert action.status == "failed"
    assert "aborted by operator" in action.note
    assert not worker._abort_event.is_set()                 # re-armed for the next action


def test_abort_refused_when_nothing_executing(worker):
    ok, detail = worker.request_abort()
    assert ok is False and "no action" in detail


def test_timeout_zero_disables_the_deadline(sender_client, session_factory):
    gw = CheckpointGateway(spin_sec=0.4)                    # finishes before any deadline
    worker = TerminalWorker(gw, sender_client, session_factory,
                            worker_settings(action_timeout_sec=0.0))
    action_id = queue_action(session_factory)
    worker.step()
    assert get_action(session_factory, action_id).status == "done"


# --- speed package: idle duties never delay pickup + async posts -------------------------

def test_idle_duties_bail_when_a_signal_arrives(worker, gateway, session_factory):
    """A queued action must never wait behind the accounts read / keepalive."""
    queue_action(session_factory)                 # work is waiting
    db = session_factory()
    try:
        worker._idle_duties(db)
    finally:
        db.close()
    assert "read_accounts_summary" not in gateway.calls
    assert "keepalive" not in gateway.calls       # bailed to let step() pick the action


def test_async_posts_flag_defaults_on_and_still_delivers(gateway, sender_client,
                                                         session_factory, fake_sender):
    """sender_post_async=True posts on a thread; the report still arrives."""
    import time as _t

    worker = TerminalWorker(gateway, sender_client, session_factory,
                            worker_settings(sender_post_async=True))
    action_id = queue_action(session_factory)
    worker.step()
    for _ in range(40):                            # wait out the daemon threads
        if len(fake_sender.reports) >= 2:
            break
        _t.sleep(0.05)
    assert get_action(session_factory, action_id).status == "done"
    assert sorted(r["status"] for r in fake_sender.reports) == ["executing", "filled"]
