"""Part 1: the local dashboard reflects reception, queue and health."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dashboard import router as dashboard_router


def make_app(session_factory, poller) -> TestClient:
    app = FastAPI()
    app.state.settings = SimpleNamespace(copier_name="VM-1", sender_base_url="http://fake")
    app.state.session_factory = session_factory
    app.state.poller = poller
    app.include_router(dashboard_router)
    return TestClient(app)


def test_index_serves_the_dashboard_page(session_factory, poller):
    client = make_app(session_factory, poller)
    response = client.get("/")
    assert response.status_code == 200
    assert "Copier" in response.text
    assert "/api/overview" in response.text  # the page polls the JSON endpoint


def test_healthz_ok(session_factory, poller):
    client = make_app(session_factory, poller)
    response = client.get("/healthz")
    assert response.status_code == 200 and response.text == "ok"


def test_index_has_launch_button(session_factory, poller):
    client = make_app(session_factory, poller)
    text = client.get("/").text
    assert "Start Tradovate terminal" in text
    assert "/api/terminal/launch" in text


def test_terminal_launch_route(session_factory, poller, monkeypatch):
    """POST launches the browser via the launcher; mock it so the test never
    spawns a real browser."""
    import app.launcher as launcher

    monkeypatch.setattr(launcher, "launch_terminal",
                        lambda: (True, "Browser launched - log in to Tradovate"))
    client = make_app(session_factory, poller)
    res = client.post("/api/terminal/launch").json()
    assert res["ok"] is True and "log in" in res["detail"].lower()


def test_overview_reports_terminal_browser_status(session_factory, poller):
    """The dashboard needs to know whether the debug browser is up (real wiring)."""
    client = make_app(session_factory, poller)
    tb = client.get("/api/overview").json()["terminal_browser"]
    assert {"browser_up", "tradovate_tab", "can_launch"} <= set(tb)
    assert isinstance(tb["browser_up"], bool)


def test_recycle_and_restart_routes_request_maintenance(session_factory, poller):
    client = make_app(session_factory, poller)
    calls = []
    client.app.state.worker = SimpleNamespace(
        request_maintenance=lambda k: calls.append(k) or True)
    assert client.post("/api/terminal/recycle").json()["ok"] is True
    assert client.post("/api/terminal/restart").json()["ok"] is True
    assert calls == ["recycle", "restart"]


def test_selftest_guarded_when_executor_attached(session_factory, poller):
    """When the executor already owns a driver, the self-test must NOT create a second
    one - it reports that access is already proven live."""
    client = make_app(session_factory, poller)
    client.app.state.worker = SimpleNamespace(
        health=lambda: {"connected": True, "active_account": "REF1"})
    res = client.post("/api/terminal/selftest").json()
    assert res["ok"] is True and res["via"] == "executor"


def test_selftest_runs_when_executor_idle(session_factory, poller, monkeypatch):
    """When the executor isn't attached, the route runs the launcher self-test."""
    import app.launcher as launcher

    monkeypatch.setattr(launcher, "selftest", lambda: {
        "ok": True, "url": "https://trader.tradovate.com/", "title": "Trader",
        "tradovate_tabs": 1, "detail": "flashed a banner"})
    client = make_app(session_factory, poller)
    client.app.state.worker = SimpleNamespace(health=lambda: {"connected": False})
    res = client.post("/api/terminal/selftest").json()
    assert res["ok"] is True and res.get("via") != "executor"
    assert "flashed" in res["detail"]


def test_overview_reflects_reception_and_queue(session_factory, poller, fake_sender):
    fake_sender.publish(base_qty=2, order_kind="limit", limit_price="28000.00")
    poller.cycle()

    client = make_app(session_factory, poller)
    data = client.get("/api/overview").json()

    assert data["copier_name"] == "VM-1"
    assert data["health"]["registered"] is True
    assert data["health"]["last_poll_ok"] is True
    assert data["counts"]["queued"] == 2
    assert data["counts"]["signals_total"] == 1
    assert {a["account_ref"] for a in data["accounts"]} == {"REF1", "REF2", "REF3"}
    assert data["signals"][0]["symbol"] == "MNQU6"
    assert any("queued" in e["message"] for e in data["events"])

    # The action rows live on the paged endpoint now, not in the overview payload.
    actions = client.get("/api/actions?limit=10").json()
    assert "queue" not in data
    assert actions["total"] == 2 and len(actions["items"]) == 2
    queue = actions["items"]
    assert all(q["kind"] == "place_limit" and q["status"] == "queued" for q in queue)
    by_ref = {q["account_ref"]: q for q in queue}
    assert by_ref["REF1"]["qty"] == 2 and by_ref["REF2"]["qty"] == 4


def test_actions_are_latest_first(session_factory, poller, fake_sender):
    first = fake_sender.publish(symbol="AAA")
    fake_sender.publish(symbol="BBB")
    poller.cycle()
    fake_sender.cancel(first)          # AAA's actions become cancelled
    poller.cycle()

    client = make_app(session_factory, poller)
    queue = client.get("/api/actions?limit=10").json()["items"]

    # Latest first: strictly descending id, newest activity at the top, regardless
    # of status - so a just-received signal never hides below older actions.
    ids = [q["id"] for q in queue]
    assert ids == sorted(ids, reverse=True)
    # The newer BBB (still queued) sits above the older, now-cancelled AAA.
    assert [q["symbol"] for q in queue] == ["BBB", "BBB", "AAA", "AAA"]
    assert [q["status"] for q in queue[:2]] == ["queued", "queued"]
    assert all(q["status"] == "cancelled" for q in queue[2:])


def test_actions_pagination(session_factory, poller, fake_sender):
    for i in range(7):
        fake_sender.publish(symbol="S%d" % i)
    poller.cycle()

    client = make_app(session_factory, poller)
    p1 = client.get("/api/actions?offset=0&limit=10").json()
    total = p1["total"]
    assert total > 10 and len(p1["items"]) == 10 and p1["offset"] == 0

    p2 = client.get("/api/actions?offset=10&limit=10").json()
    assert len(p2["items"]) == min(10, total - 10)
    ids1 = [a["id"] for a in p1["items"]]
    ids2 = [a["id"] for a in p2["items"]]
    assert ids1 == sorted(ids1, reverse=True)   # latest first within the page
    assert min(ids1) > max(ids2)                # page 1 strictly newer than page 2
    assert client.get("/api/actions?limit=999").json()["limit"] == 50   # cap enforced


def test_timestamps_carry_utc_offset(session_factory, poller, fake_sender):
    """SQLite drops tz offsets; serialization must restore +00:00, or browsers parse
    the bare string as LOCAL time and every age shows off by the viewer's offset."""
    fake_sender.publish()
    poller.cycle()
    client = make_app(session_factory, poller)
    data = client.get("/api/overview").json()
    assert data["signals"][0]["received_at"].endswith("+00:00")
    assert data["events"][0]["ts"].endswith("+00:00")
    action = client.get("/api/actions?limit=1").json()["items"][0]
    assert action["created_at"].endswith("+00:00")


def test_activity_feed_is_cursor_paged(session_factory, poller, fake_sender):
    """The ATS-facing /api/activity: cursor-paged events + correlatable actions/signals."""
    fake_sender.publish()
    poller.cycle()
    client = make_app(session_factory, poller)

    data = client.get("/api/activity").json()
    assert data["events"], "expected recorded events"
    ids = [e["id"] for e in data["events"]]
    assert ids == sorted(ids)                      # ascending → safe forward paging
    assert data["events"][0]["ts"].endswith("+00:00")
    assert "data" in data["events"][0]             # machine-readable payload field
    assert data["signals"] and data["actions"]     # correlation rows present

    tail = client.get("/api/activity?since=%d" % data["cursor"]).json()
    assert all(e["id"] > data["cursor"] for e in tail["events"])   # no re-read


def test_healthz_reports_dead_poller(session_factory, poller):
    """A poller that STARTED and then died must flip health to 503."""
    poller.stop()      # pre-set the stop event so the thread exits immediately
    poller.start()
    poller.join(timeout=5)
    assert not poller.is_alive()

    client = make_app(session_factory, poller)
    response = client.get("/healthz")
    assert response.status_code == 503
    assert "poller dead" in response.text
