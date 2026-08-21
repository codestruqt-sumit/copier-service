"""Telegram alerts: the notifier itself (disabled = no-op, payload shape, topic
routing) and the wiring on both workers (poller alerts caught signals; executor alerts
failed actions and warns ONCE when the terminal logs out). Everything here proves the
feature is additive and cannot break reception/execution: sends are fire-and-forget and
swallow all errors, and a RecordingNotifier stands in so no real HTTP ever happens.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import telegram
from app.executor import TerminalWorker
from app.telegram import TelegramNotifier
from tests.test_executor import FakeGateway, queue_action, worker_settings


@pytest.fixture
def gateway():
    return FakeGateway()


class RecordingNotifier:
    """Stand-in that records (category, text) synchronously - no threads, no HTTP."""

    enabled = True

    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, category: str, text: str) -> None:
        self.sent.append((category, text))


# --- the notifier itself ---------------------------------------------------------------

def test_disabled_notifier_is_a_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(telegram.urllib.request, "urlopen",
                        lambda *a, **k: calls.append(a))
    n = TelegramNotifier(token="", chat_id="", topics={"copier": 1})
    assert n.enabled is False
    n.send("copier", "should not send")
    assert calls == []            # no token/chat id -> never touches the network


def test_send_builds_payload_with_topic(monkeypatch):
    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = req.data.decode()
        captured["timeout"] = timeout
        return FakeResp()

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)
    n = TelegramNotifier(token="TOK", chat_id="-100999",
                         topics={"copier": 7, "errors": 8}, timeout=3.0)
    n._send("copier", "hello")     # call sync to avoid thread races in the test

    assert "botTOK/sendMessage" in captured["url"]
    assert "chat_id=-100999" in captured["data"]
    assert "message_thread_id=7" in captured["data"]
    assert "hello" in captured["data"]
    assert captured["timeout"] == 3.0


def test_send_without_a_topic_omits_thread_id(monkeypatch):
    captured = {}
    monkeypatch.setattr(telegram.urllib.request, "urlopen",
                        lambda req, timeout=None: captured.setdefault("data", req.data.decode())
                        or SimpleNamespace(__enter__=lambda s: s, __exit__=lambda *a: False,
                                           read=lambda: b"{}"))
    n = TelegramNotifier(token="T", chat_id="C", topics={})  # no topics configured
    n._send("copier", "no thread")
    assert "message_thread_id" not in captured["data"]


def test_from_settings_maps_categories_to_topics():
    s = SimpleNamespace(
        telegram_bot_token="T", telegram_chat_id="C", telegram_timeout_sec=4.0,
        telegram_topic_copier=1, telegram_topic_errors=2, telegram_topic_warnings=3)
    n = TelegramNotifier.from_settings(s)
    assert n.enabled is True
    assert n.topics == {"copier": 1, "errors": 2, "warnings": 3}
    assert n.timeout == 4.0


# --- poller wiring: signals caught -> "copier" topic -----------------------------------

def test_poller_alerts_when_it_catches_signals(poller, fake_sender):
    rec = RecordingNotifier()
    poller._notifier = rec
    fake_sender.publish(symbol="ESU6", side="buy", order_kind="market")
    poller.cycle()

    caught = [m for m in rec.sent if m[0] == "copier"]
    assert len(caught) == 1
    assert "VM-1" in caught[0][1] and "ESU6" in caught[0][1]


def test_poller_silent_when_no_signals(poller, fake_sender):
    rec = RecordingNotifier()
    poller._notifier = rec
    poller.cycle()                 # nothing published (but registration activity DOES fire)
    assert not any(c == "copier" for c, _ in rec.sent)


# --- activity feed: registration + sender online/offline transitions -------------------

def test_poller_alerts_activity_on_first_registration(poller, fake_sender):
    rec = RecordingNotifier()
    poller._notifier = rec
    poller.cycle()                 # first-time registration
    acts = [t for c, t in rec.sent if c == "activity"]
    assert any("registered" in t.lower() and "VM-1" in t for t in acts)


def test_poller_alerts_activity_on_connection_restored(poller, fake_sender):
    rec = RecordingNotifier()
    poller._notifier = rec
    poller._was_failing = True     # pretend the Sender had been down
    poller.cycle()                 # a healthy cycle -> "restored"
    assert any(c == "activity" and "restored" in t.lower() for c, t in rec.sent)


def test_poller_alerts_activity_when_sender_unreachable(bad_poller):
    rec = RecordingNotifier()
    bad_poller._notifier = rec
    bad_poller.cycle()             # bad key -> register 401 -> transition to failing
    assert any(c == "activity" and "unreachable" in t.lower() for c, t in rec.sent)


# --- TELEGRAM_ACTIVITY_ALL firehose: mirror every log_event to the activity topic -------

def test_log_event_mirrors_to_activity_when_flag_on(db, monkeypatch):
    from app import processor
    from app.config import settings as app_settings

    rec = RecordingNotifier()
    monkeypatch.setattr("app.telegram.notifier", rec)          # processor uses _tg.notifier
    monkeypatch.setattr(app_settings, "telegram_activity_all", True)

    processor.log_event(db, "warn", "terminal", "something happened")
    assert any(c == "activity" and "something happened" in t for c, t in rec.sent)


def test_log_event_no_mirror_when_flag_off(db, monkeypatch):
    from app import processor
    from app.config import settings as app_settings

    rec = RecordingNotifier()
    monkeypatch.setattr("app.telegram.notifier", rec)
    monkeypatch.setattr(app_settings, "telegram_activity_all", False)

    processor.log_event(db, "info", "test", "quiet")
    assert rec.sent == []


# --- executor wiring: failures -> "errors", logout -> "warnings" (deduped) --------------

def test_executor_alerts_on_failed_action(gateway, sender_client, session_factory):
    rec = RecordingNotifier()
    gateway.execute_result = {"outcome": "failed", "order_ref": None, "detail": "no move"}
    worker = TerminalWorker(gateway, sender_client, session_factory,
                            worker_settings(), notifier=rec)
    queue_action(session_factory)
    worker.step()

    errs = [m for m in rec.sent if m[0] == "errors"]
    assert len(errs) == 1
    assert "FAILED" in errs[0][1]


def test_executor_warns_once_on_logout_then_again_after_recovery(
        gateway, sender_client, session_factory):
    rec = RecordingNotifier()
    gateway.login_ok = False
    worker = TerminalWorker(gateway, sender_client, session_factory,
                            worker_settings(), notifier=rec)
    queue_action(session_factory)
    worker.step()                             # login fails -> HELD -> warn once
    worker._set_logged_in(False, "still out")  # repeat state -> NOT a second warning
    assert len([m for m in rec.sent if m[0] == "warnings"]) == 1

    worker._set_logged_in(True)               # recovered -> arm the warning again
    worker._set_logged_in(False, "out again")  # new logout -> warns again
    assert len([m for m in rec.sent if m[0] == "warnings"]) == 2


def test_executor_alerts_when_browser_down(gateway, sender_client, session_factory):
    # browser closed / not attachable -> reliable "needs attention" warning (not silent).
    rec = RecordingNotifier()
    gateway.connect_ok = False
    worker = TerminalWorker(gateway, sender_client, session_factory,
                            worker_settings(), notifier=rec)
    worker.step()                              # ensure_connected fails -> _warn_terminal
    warns = [t for c, t in rec.sent if c == "warnings"]
    assert warns, "a down browser must raise a warnings alert"
    assert "not reachable" in warns[0] or "browser" in warns[0].lower()


def test_executor_without_notifier_never_calls_send(gateway, sender_client, session_factory):
    # notifier defaults to None: the guards must skip cleanly (no AttributeError).
    gateway.execute_result = {"outcome": "failed", "order_ref": None, "detail": "x"}
    worker = TerminalWorker(gateway, sender_client, session_factory, worker_settings())
    queue_action(session_factory)
    assert worker.step() == "acted"           # ran to completion with notifier=None
