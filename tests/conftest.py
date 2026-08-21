"""Test harness: an in-memory SQLite copier + a FAKE Sender app.

The fake Sender replicates the real /api/copier/* contract (auth header, payload
shapes, opaque incremental cursor) so the whole reception path runs unchanged -
SenderClient simply gets a starlette TestClient pointed at the fake app instead of
a real network client.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.sender_client import SenderClient

KEY = "test-copier-key"

ACCOUNTS = [
    {"account_ref": "REF1", "alias": "Acc-1", "copy_ratio": "1", "groups": ["Group 1"]},
    {"account_ref": "REF2", "alias": "Acc-2", "copy_ratio": "2", "groups": ["Group 1", "Aggressive"]},
    {"account_ref": "REF3", "alias": "Acc-3", "copy_ratio": "1", "groups": ["Aggressive"]},
]

CONFIG = {"command_poll_sec": 1, "state_poll_sec": 15, "heartbeat_timeout_sec": 30}


class FakeSender:
    """Real contract, in-memory state. Commands carry an internal seq; the cursor is
    just the last seq as a string - opaque to the copier, exactly like production."""

    def __init__(self):
        self._seq = itertools.count(1)
        self._ids = itertools.count(1)
        self.commands: list[dict] = []
        self.heartbeats: list[str] = []
        self.heartbeat_bodies: list[dict] = []
        self.registrations = 0
        self.fail_register = False    # simulate a broken /register
        self.fail_heartbeat = False   # simulate a broken /heartbeat
        self.fail_killswitch = False  # simulate a broken /killswitch
        self.killswitch_map = {a["account_ref"]: True for a in ACCOUNTS}
        self.reports: list[dict] = []  # every execution report the copier posts
        self.states: list[dict] = []   # every account-state snapshot posted
        self.accounts: list[dict] = [] # every all-accounts PnL row posted

    # -- test-side helpers ----------------------------------------------------

    def publish(self, **overrides) -> dict:
        cmd = {
            "signal_id": next(self._ids),
            "revision": 1,
            "status": "published",
            "symbol": "MNQU6",
            "order_kind": "market",
            "side": "buy",
            "base_qty": 1,
            "limit_price": None,
            "stop_price": None,
            "tif": "day",
            "target_groups": ["Group 1"],
            "valid_until": None,
        }
        cmd.update(overrides)
        cmd["_seq"] = next(self._seq)
        self.commands.append(cmd)
        return cmd

    def revise(self, cmd: dict, **overrides) -> dict:
        """Edit an existing command: bump revision, re-deliver past the cursor."""
        new = {k: v for k, v in cmd.items() if k != "_seq"}
        new["revision"] = cmd["revision"] + 1
        new["status"] = overrides.pop("status", "updated")
        new.update(overrides)
        new["_seq"] = next(self._seq)
        self.commands.append(new)
        return new

    def cancel(self, cmd: dict) -> dict:
        return self.revise(cmd, status="cancelled")

    # -- the ASGI app ----------------------------------------------------------

    def app(self) -> FastAPI:
        api = FastAPI()

        def auth(x_copier_key: str | None):
            if x_copier_key != KEY:
                raise HTTPException(status_code=401, detail="Invalid or missing Copier key.")

        @api.post("/api/copier/register")
        def register(x_copier_key: str | None = Header(default=None)):
            auth(x_copier_key)
            if self.fail_register:
                raise HTTPException(status_code=500, detail="register broken")
            self.registrations += 1
            return {"copier_id": 1, "accounts": ACCOUNTS, "config": CONFIG}

        @api.post("/api/copier/heartbeat")
        async def heartbeat(request: Request, x_copier_key: str | None = Header(default=None)):
            auth(x_copier_key)
            if self.fail_heartbeat:
                raise HTTPException(status_code=500, detail="heartbeat broken")
            body = await request.json()
            self.heartbeats.append(body.get("status", "online"))
            self.heartbeat_bodies.append(body)
            return {"ok": True, "config": CONFIG}

        @api.get("/api/copier/commands")
        def commands(since: str | None = None, x_copier_key: str | None = Header(default=None)):
            auth(x_copier_key)
            try:
                floor = int(since) if since else 0
            except ValueError:
                floor = 0
            fresh = [c for c in self.commands if c["_seq"] > floor]
            cursor = str(fresh[-1]["_seq"]) if fresh else (since or None)
            return {
                "cursor": cursor,
                "commands": [{k: v for k, v in c.items() if k != "_seq"} for c in fresh],
            }

        @api.get("/api/copier/killswitch")
        def killswitch(x_copier_key: str | None = Header(default=None)):
            auth(x_copier_key)
            if self.fail_killswitch:
                raise HTTPException(status_code=500, detail="killswitch broken")
            return {"accounts": dict(self.killswitch_map)}

        @api.post("/api/copier/reports")
        async def reports(request: Request, x_copier_key: str | None = Header(default=None)):
            auth(x_copier_key)
            body = await request.json()
            self.reports.extend(body.get("reports") or [])
            return {"ok": True, "applied": len(body.get("reports") or [])}

        @api.post("/api/copier/state")
        async def state(request: Request, x_copier_key: str | None = Header(default=None)):
            auth(x_copier_key)
            body = await request.json()
            self.states.extend(body.get("states") or [])
            return {"ok": True, "applied": len(body.get("states") or [])}

        @api.post("/api/copier/accounts")
        async def accounts(request: Request, x_copier_key: str | None = Header(default=None)):
            auth(x_copier_key)
            body = await request.json()
            self.accounts.extend(body.get("accounts") or [])
            return {"ok": True, "applied": len(body.get("accounts") or [])}

        return api


@pytest.fixture
def fake_sender() -> FakeSender:
    return FakeSender()


@pytest.fixture
def sender_client(fake_sender) -> SenderClient:
    http = TestClient(fake_sender.app())
    return SenderClient("http://fake", KEY, "VM-1", http=http)


@pytest.fixture
def bad_key_client(fake_sender) -> SenderClient:
    http = TestClient(fake_sender.app())
    return SenderClient("http://fake", "wrong-key", "VM-1", http=http)


@pytest.fixture
def session_factory():
    # StaticPool: one shared connection, so the TestClient's worker thread sees the
    # same in-memory database the test thread created the tables in.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@pytest.fixture
def db(session_factory):
    session = session_factory()
    yield session
    session.close()


@pytest.fixture
def registration() -> dict:
    return {"copier_id": 1, "accounts": ACCOUNTS, "config": CONFIG}


@pytest.fixture
def poller(sender_client, session_factory):
    """A Poller whose cycle() we drive by hand - the thread is never started."""
    from app.poller import Poller

    settings = SimpleNamespace(
        copier_name="VM-1",
        sender_base_url="http://fake",
        poll_sec=None,
        heartbeat_sec=None,
        register_refresh_sec=60.0,
    )
    return Poller(sender_client, session_factory, settings)


@pytest.fixture
def bad_poller(bad_key_client, session_factory):
    from app.poller import Poller

    settings = SimpleNamespace(
        copier_name="VM-1",
        sender_base_url="http://fake",
        poll_sec=None,
        heartbeat_sec=None,
        register_refresh_sec=60.0,
    )
    return Poller(bad_key_client, session_factory, settings)
