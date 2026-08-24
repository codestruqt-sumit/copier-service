"""Part 1: the local dashboard - open http://<vm>:8100/ in a browser.

One static page + one JSON endpoint the page polls every couple of seconds. Everything
is self-contained (no external assets), so it works on an offline VM.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy import func, select

from app.models import Action, Event, SignalSeen

router = APIRouter()

_TEMPLATE = Path(__file__).parent / "templates" / "dashboard.html"


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        # Everything we store is UTC, but SQLite keeps no offset - restore it, or the
        # browser parses the bare string as LOCAL time and every age is off by the
        # viewer's UTC offset (the "every signal is 5 hours old" bug).
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _action_row(a: Action) -> dict:
    return {
        "id": a.id, "signal_id": a.signal_id, "revision": a.revision,
        "account": a.account_alias or a.account_ref, "account_ref": a.account_ref,
        "kind": a.kind, "symbol": a.symbol, "side": a.side, "qty": a.qty,
        "limit_price": a.limit_price, "stop_price": a.stop_price, "tif": a.tif,
        "status": a.status, "note": a.note, "order_ref": a.order_ref,
        "created_at": _iso(a.created_at), "updated_at": _iso(a.updated_at),
    }


def _signal_row(s: SignalSeen) -> dict:
    return {
        "signal_id": s.signal_id, "revision": s.revision, "status": s.status,
        "symbol": s.symbol, "order_kind": s.order_kind, "side": s.side,
        "base_qty": s.base_qty, "limit_price": s.limit_price, "stop_price": s.stop_price,
        "tif": s.tif, "target_groups": s.target_groups, "received_at": _iso(s.received_at),
    }


def _event_row(e: Event) -> dict:
    return {"id": e.id, "ts": _iso(e.ts), "level": e.level, "category": e.category,
            "message": e.message, "data": e.data or {}}


@router.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(_TEMPLATE.read_text(encoding="utf-8"))


@router.post("/api/terminal/launch")
def terminal_launch() -> dict:
    """Start the Tradovate debug browser so the user can log in (then the executor
    attaches). The copier never handles credentials - this only opens the window."""
    try:
        from app.launcher import launch_terminal
    except Exception as exc:  # noqa: BLE001 - bot modules/selenium absent
        return {"ok": False, "detail": f"launcher unavailable: {exc}"}
    ok, detail = launch_terminal()
    return {"ok": ok, "detail": detail}


@router.post("/api/terminal/selftest")
def terminal_selftest(request: Request) -> dict:
    """Manually prove the Python script accesses the RIGHT tab: flash a visible banner
    in the Tradovate tab. Guarded so it never creates a second driver while the executor
    is already attached - in that case tab access is already proven live."""
    worker = getattr(request.app.state, "worker", None)
    health = worker.health() if worker else {}
    if health.get("connected"):
        return {
            "ok": True,
            "via": "executor",
            "active_account": health.get("active_account"),
            "detail": "The executor is attached and driving the terminal right now - tab "
                      "access is already proven live (see the Terminal snapshot below). "
                      "Turn the executor off first if you want the manual banner test.",
        }
    try:
        from app.launcher import selftest
    except Exception as exc:  # noqa: BLE001 - bot modules/selenium absent
        return {"ok": False, "detail": f"self-test unavailable: {exc}"}
    return selftest()


@router.post("/api/terminal/recycle")
def terminal_recycle(request: Request) -> dict:
    """Force a driver recycle (drop + re-attach a fresh Selenium driver to the same
    browser). Runs on the executor thread, between actions."""
    worker = getattr(request.app.state, "worker", None)
    if worker is None:
        return {"ok": False, "detail": "no executor worker"}
    worker.request_maintenance("recycle")
    return {"ok": True, "detail": "driver recycle requested - runs between actions"}


@router.post("/api/terminal/restart")
def terminal_restart(request: Request) -> dict:
    """Force a full browser restart (close + relaunch on the persistent profile, then
    re-attach). Runs on the executor thread, between actions. Your login persists via the
    profile; if it ever doesn't, log in again in the relaunched window."""
    worker = getattr(request.app.state, "worker", None)
    if worker is None:
        return {"ok": False, "detail": "no executor worker"}
    worker.request_maintenance("restart")
    return {"ok": True, "detail": "browser restart requested - runs between actions"}


@router.get("/healthz", response_class=PlainTextResponse)
def healthz(request: Request) -> PlainTextResponse:
    db = request.app.state.session_factory()
    try:
        db.execute(select(func.count(Event.id)))
    except Exception as exc:  # pragma: no cover - broken db only
        return PlainTextResponse(f"db error: {exc}", status_code=503)
    finally:
        db.close()
    poller = request.app.state.poller
    # ident is set once the thread starts; a started-then-dead poller is a real failure.
    if poller is not None and poller.ident is not None and not poller.is_alive():
        return PlainTextResponse("poller dead", status_code=503)
    return PlainTextResponse("ok")


@router.get("/api/overview")
def overview(request: Request) -> dict:
    import json as _json

    from app.executor import TERMINAL_STATE_KEY
    from app.models import KV

    poller = request.app.state.poller
    worker = getattr(request.app.state, "worker", None)
    settings = request.app.state.settings
    db = request.app.state.session_factory()
    try:
        state_row = db.get(KV, TERMINAL_STATE_KEY)
        try:
            terminal_state = _json.loads(state_row.value) if state_row else None
        except ValueError:
            terminal_state = None
        # The action table is paged separately via /api/actions (10 at a time), so the
        # 2s overview poll stays light - it only needs the counts, not the rows.
        signals = list(db.scalars(
            select(SignalSeen).order_by(SignalSeen.id.desc()).limit(60)
        ))
        events = list(db.scalars(select(Event).order_by(Event.id.desc()).limit(250)))
        counts = {
            "queued": db.scalar(
                select(func.count(Action.id)).where(Action.status == "queued")
            ) or 0,
            "actions_total": db.scalar(select(func.count(Action.id))) or 0,
            "signals_total": db.scalar(select(func.count(SignalSeen.id))) or 0,
            "errors_total": db.scalar(
                select(func.count(Event.id)).where(Event.level == "error")
            ) or 0,
        }
    finally:
        db.close()

    try:
        from app.launcher import terminal_status
        terminal_browser = terminal_status()
    except Exception:  # noqa: BLE001 - bot modules/selenium absent
        terminal_browser = {"browser_up": False, "tradovate_tab": False, "tradovate_tabs": 0,
                            "tab_count": 0, "zombie": False, "can_launch": False}

    registration = poller.registration() if poller else {}
    return {
        "copier_name": settings.copier_name,
        "sender_base_url": settings.sender_base_url,
        "health": poller.health() if poller else {},
        "executor": worker.health() if worker else {"enabled": False, "detail": "no worker"},
        "terminal": terminal_state,
        "terminal_browser": terminal_browser,
        "counts": counts,
        "accounts": registration.get("accounts", []),
        "sender_config": registration.get("config", {}),
        "signals": [_signal_row(s) for s in signals],
        "events": [_event_row(e) for e in events],
    }


@router.get("/api/activity")
def activity(request: Request, since: int = 0, limit: int = 300) -> dict:
    """Machine-readable activity feed for the ATS. `events` are cursor-paged (id > since,
    ascending) so a poller never misses or re-reads; `actions` + `signals` are the recent
    structured rows the ATS correlates to its sent signals by signal_id. Read-only."""
    limit = max(1, min(int(limit), 1000))
    since = max(0, int(since))
    db = request.app.state.session_factory()
    try:
        events = list(db.scalars(
            select(Event).where(Event.id > since).order_by(Event.id).limit(limit)
        ))
        cursor = events[-1].id if events else since
        latest = db.scalar(select(func.max(Event.id))) or 0
        acts = list(db.scalars(select(Action).order_by(Action.id.desc()).limit(300)))
        sigs = list(db.scalars(select(SignalSeen).order_by(SignalSeen.id.desc()).limit(300)))
    finally:
        db.close()
    return {
        "cursor": cursor,
        "latest": latest,        # max event id now — init a forward poller from here
        "events": [_event_row(e) for e in events],
        "actions": [_action_row(a) for a in acts],
        "signals": [_signal_row(s) for s in sigs],
    }


@router.post("/api/executor/abort")
def executor_abort(request: Request) -> dict:
    """Operator control: halt the CURRENTLY EXECUTING action at its next safe gateway
    checkpoint (between operations, never mid-click). The action fails loudly with a
    verify-manually note and is never re-sent. Queued actions are untouched (use
    /api/queue/flush for those)."""
    worker = getattr(request.app.state, "worker", None)
    if worker is None:
        return {"ok": False, "detail": "no executor worker"}
    ok, detail = worker.request_abort()
    return {"ok": ok, "detail": detail}


@router.post("/api/queue/flush")
def queue_flush(request: Request) -> dict:
    """Operator control: CANCEL all queued actions and DEFER the cursor to the Sender's
    current latest - so the copier drops everything already sent and acts only on NEWER
    signals from now on. Use to recover from a bad backlog or hard-reset reception."""
    from sqlalchemy import update

    from app.processor import log_event

    poller = getattr(request.app.state, "poller", None)
    db = request.app.state.session_factory()
    try:
        res = db.execute(
            update(Action).where(Action.status == "queued").values(
                status="cancelled", note="cleared by operator flush")
        )
        cancelled = res.rowcount or 0
        latest = None
        if poller is not None:
            latest, _ = poller.skip_to_latest(db)
        log_event(db, "warn", "executor",
                  f"Operator FLUSH: cancelled {cancelled} queued action(s); cursor deferred "
                  f"to {latest} - only newer signals from now.")
        db.commit()
        return {"ok": True, "cancelled": cancelled, "cursor": latest,
                "detail": f"cleared {cancelled} queued action(s); now listening from the "
                          f"latest signal only."}
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    finally:
        db.close()


@router.get("/api/actions")
def actions(request: Request, offset: int = 0, limit: int = 10) -> dict:
    """Paged action history, latest first - the dashboard shows 10 at a time so the
    table isn't a wall of rows."""
    limit = max(1, min(int(limit), 50))
    offset = max(0, int(offset))
    db = request.app.state.session_factory()
    try:
        total = db.scalar(select(func.count(Action.id))) or 0
        rows = list(db.scalars(
            select(Action).order_by(Action.id.desc()).offset(offset).limit(limit)
        ))
    finally:
        db.close()
    return {"items": [_action_row(a) for a in rows], "total": total,
            "offset": offset, "limit": limit}
