"""The reception loop against the fake Sender: registration, cursor, no-loss, outages."""

from __future__ import annotations

from sqlalchemy import select

from app.config import settings
from app.models import Action, Event, SignalSeen
from app.poller import CURSOR_KEY, Poller, kv_get


def _count(db, model) -> int:
    return len(list(db.scalars(select(model))))


# --- terminal-health heartbeat -----------------------------------------------------

def _hp(sender_client, session_factory, health):
    return Poller(sender_client, session_factory, settings, health_provider=lambda: health)


def test_terminal_health_logged_out_is_unhealthy(sender_client, session_factory):
    p = _hp(sender_client, session_factory,
            {"enabled": True, "connected": True, "logged_in": False})
    healthy, detail, terminal = p._terminal_health()
    assert healthy is False and "LOGGED OUT" in detail
    assert terminal == {"executor_enabled": True, "connected": True, "logged_in": False}


def test_terminal_health_logged_in_is_healthy(sender_client, session_factory):
    p = _hp(sender_client, session_factory,
            {"enabled": True, "connected": True, "logged_in": True})
    healthy, detail, _ = p._terminal_health()
    assert healthy is True and "logged in" in detail


def test_terminal_health_executor_off_is_healthy(sender_client, session_factory):
    p = _hp(sender_client, session_factory, {"enabled": False})
    healthy, detail, _ = p._terminal_health()
    assert healthy is True and "executor off" in detail


def test_terminal_health_disconnected_is_unhealthy(sender_client, session_factory):
    p = _hp(sender_client, session_factory, {"enabled": True, "connected": False})
    healthy, detail, _ = p._terminal_health()
    assert healthy is False and "not connected" in detail


def test_heartbeat_reports_health_to_sender(sender_client, session_factory, fake_sender):
    p = _hp(sender_client, session_factory,
            {"enabled": True, "connected": True, "logged_in": False})
    p._last_heartbeat = 0.0                        # force a heartbeat
    p._heartbeat_if_due()
    body = fake_sender.heartbeat_bodies[-1]
    assert body["healthy"] is False and "LOGGED OUT" in body["detail"]
    assert body["terminal"]["logged_in"] is False
    assert p.health()["healthy"] is False          # mirrored locally too


def test_heartbeat_interval_capped_at_60s(sender_client, session_factory):
    p = Poller(sender_client, session_factory, settings)
    p._registration = {"config": {"heartbeat_timeout_sec": 600}}   # 200s uncapped
    assert p._heartbeat_interval() <= 60.0


def test_first_cycle_registers_and_heartbeats(poller, fake_sender, session_factory):
    poller.cycle()
    assert fake_sender.registrations == 1
    assert fake_sender.heartbeats == ["online"]
    health = poller.health()
    assert health["registered"] is True
    assert health["accounts"] == 3
    assert health["last_poll_ok"] is True


def test_commands_become_actions_and_cursor_advances(poller, fake_sender, session_factory):
    fake_sender.publish(base_qty=2)                       # market -> Group 1
    poller.cycle()
    db = session_factory()
    try:
        assert _count(db, SignalSeen) == 1
        assert _count(db, Action) == 2                     # Acc-1 + Acc-2
        assert kv_get(db, CURSOR_KEY) == "1"
    finally:
        db.close()


def test_second_cycle_delivers_nothing_new(poller, fake_sender, session_factory):
    fake_sender.publish()
    poller.cycle()
    poller.cycle()  # same cursor -> no re-processing
    db = session_factory()
    try:
        assert _count(db, SignalSeen) == 1
        assert _count(db, Action) == 2
    finally:
        db.close()


def test_only_new_commands_after_cursor(poller, fake_sender, session_factory):
    fake_sender.publish(symbol="MNQU6")
    poller.cycle()
    fake_sender.publish(symbol="MGCZ6", order_kind="limit", limit_price="2400.00")
    poller.cycle()
    db = session_factory()
    try:
        symbols = sorted(s.symbol for s in db.scalars(select(SignalSeen)))
        assert symbols == ["MGCZ6", "MNQU6"]
        assert kv_get(db, CURSOR_KEY) == "2"
    finally:
        db.close()


def test_burst_is_captured_without_loss(poller, fake_sender, session_factory):
    for n in range(25):
        fake_sender.publish(symbol=f"SYM{n}")
    poller.cycle()
    db = session_factory()
    try:
        assert _count(db, SignalSeen) == 25
        assert _count(db, Action) == 50                    # 2 matching accounts each
    finally:
        db.close()


def test_cancel_arrives_on_a_later_poll(poller, fake_sender, session_factory):
    cmd = fake_sender.publish(order_kind="limit", limit_price="28000.00")
    poller.cycle()
    fake_sender.cancel(cmd)
    poller.cycle()
    db = session_factory()
    try:
        actions = list(db.scalars(select(Action)))
        assert actions and all(a.status == "cancelled" for a in actions)
    finally:
        db.close()


def test_revision_update_arrives_on_a_later_poll(poller, fake_sender, session_factory):
    cmd = fake_sender.publish(order_kind="limit", limit_price="28000.00")
    poller.cycle()
    fake_sender.revise(cmd, limit_price="27980.00")
    poller.cycle()
    db = session_factory()
    try:
        old = list(db.scalars(select(Action).where(Action.revision == 1)))
        new = list(db.scalars(select(Action).where(Action.revision == 2)))
        assert all(a.status == "superseded" for a in old)
        assert all(a.status == "queued" and a.limit_price == "27980.00" for a in new)
    finally:
        db.close()


def test_bad_key_degrades_health_without_crashing(bad_poller, session_factory):
    bad_poller.cycle()
    health = bad_poller.health()
    assert health["last_poll_ok"] is False
    assert health["consecutive_errors"] == 1
    assert "401" in (health["last_error"] or "")
    bad_poller.cycle()  # still alive on the next cycle
    assert bad_poller.health()["consecutive_errors"] == 2
    db = session_factory()
    try:
        errors = [e for e in db.scalars(select(Event)) if e.level == "error"]
        assert len(errors) == 1  # logged on the ok->fail transition, not every cycle
    finally:
        db.close()


def test_cycle_survives_broken_session_factory(sender_client):
    """Even a failing session factory must not raise out of cycle() (thread safety net)."""
    from types import SimpleNamespace

    from app.poller import Poller

    def broken_factory():
        raise RuntimeError("disk on fire")

    poller = Poller(sender_client, broken_factory, SimpleNamespace(
        copier_name="VM-1", sender_base_url="http://fake",
        poll_sec=None, heartbeat_sec=None, register_refresh_sec=60.0,
    ))
    poller.cycle()  # must not raise
    health = poller.health()
    assert health["last_poll_ok"] is False
    assert "disk on fire" in health["last_error"]


def test_heartbeat_failure_does_not_block_reception(poller, fake_sender, session_factory):
    fake_sender.fail_heartbeat = True
    fake_sender.publish()
    poller.cycle()
    db = session_factory()
    try:
        assert _count(db, SignalSeen) == 1   # commands still received and queued
        assert _count(db, Action) == 2
    finally:
        db.close()
    assert poller.health()["last_poll_ok"] is True


def test_register_outage_uses_cached_registration(poller, fake_sender, session_factory):
    poller.cycle()                                  # registers fine, caches the payload
    fake_sender.fail_register = True
    poller.settings.register_refresh_sec = 0.0      # force a re-register attempt
    fake_sender.publish()
    poller.cycle()                                  # register 500s; cached copy keeps us going
    db = session_factory()
    try:
        assert _count(db, Action) == 2
    finally:
        db.close()
    assert poller.health()["last_poll_ok"] is True


def test_cursor_survives_restart(poller, fake_sender, session_factory, sender_client):
    from types import SimpleNamespace

    from app.poller import Poller

    fake_sender.publish()
    poller.cycle()

    # A brand-new poller over the SAME database (a restart) must resume, not replay.
    reborn = Poller(sender_client, session_factory, SimpleNamespace(
        copier_name="VM-1", sender_base_url="http://fake",
        poll_sec=None, heartbeat_sec=None, register_refresh_sec=60.0,
    ))
    reborn.cycle()
    db = session_factory()
    try:
        assert _count(db, SignalSeen) == 1  # nothing re-delivered
    finally:
        db.close()


def test_fresh_copier_skips_backlog_and_listens_from_now(poller, fake_sender, session_factory):
    """A fresh copier (no cursor) must NOT replay history - it learns the current cursor
    and acts only on signals sent AFTER it started (the anti-backlog-replay guard)."""
    poller.settings.replay_backlog_on_start = False
    fake_sender.publish(symbol="ESU6", side="buy")     # backlog - already happened
    fake_sender.publish(symbol="NQU6", side="sell")
    poller.cycle()
    db = session_factory()
    try:
        assert _count(db, SignalSeen) == 0             # backlog SKIPPED, not replayed
        assert kv_get(db, CURSOR_KEY) is not None       # cursor advanced to "now"
    finally:
        db.close()

    fake_sender.publish(symbol="MNQU6", side="buy")     # a NEW signal, sent after start
    poller.cycle()
    db = session_factory()
    try:
        assert [s.symbol for s in db.scalars(select(SignalSeen))] == ["MNQU6"]
    finally:
        db.close()
