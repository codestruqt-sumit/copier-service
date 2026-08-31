"""UI mode selection: stored in the local DB, wins over env at BOOT, never mid-run."""
from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.gateways import MODE_OVERRIDE_KEY, resolve_mode
from app.models import KV, Base


def _sf():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)


def _settings(mode="web"):
    return SimpleNamespace(copier_mode=mode)


def _store(sf, mode):
    db = sf()
    db.add(KV(key=MODE_OVERRIDE_KEY, value=mode))
    db.commit()
    db.close()


def test_env_mode_when_no_override():
    assert resolve_mode(_settings("web"), _sf()) == "web"
    assert resolve_mode(_settings("api"), _sf()) == "api"


def test_stored_override_beats_env():
    sf = _sf()
    _store(sf, "api")
    assert resolve_mode(_settings("web"), sf) == "api"


def test_garbage_override_falls_back_to_env():
    sf = _sf()
    _store(sf, "carrier-pigeon")
    assert resolve_mode(_settings("web"), sf) == "web"


def test_no_session_factory_uses_env():
    assert resolve_mode(_settings("api"), None) == "api"
