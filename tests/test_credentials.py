"""Direct-credential (headless) auth: mint at boot, renew before expiry, mode-branching.
Uses a fake credentials module (monkeypatched) - no network.
"""
from __future__ import annotations

from types import SimpleNamespace

from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.gateways.api import credentials, token_store
from app.gateways.api.gateway import TradovateGateway
from app.models import Base


def _settings(auth="credentials"):
    return SimpleNamespace(
        tradovate_base="https://demo.tradovateapi.com/v1", tradovate_auth=auth,
        tradovate_name="Trader1", tradovate_password=SecretStr("pw"),
        tradovate_cid="1234", tradovate_sec=SecretStr("sec"),
        tradovate_app_id="CopierM46", tradovate_app_version="1.0",
        net_verify_sec=1.0, http_timeout_sec=5.0)


def _db():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def test_credentials_mint_on_first_use(monkeypatch):
    calls = {"mint": 0, "renew": 0}
    monkeypatch.setattr(credentials, "access_token_request",
                        lambda s: calls.__setitem__("mint", calls["mint"] + 1)
                        or {"access_token": "AT1", "expires_in": 3600, "refresh_token": None})
    monkeypatch.setattr(credentials, "renew_access_token",
                        lambda s, t: calls.__setitem__("renew", calls["renew"] + 1)
                        or {"access_token": "AT2", "expires_in": 3600})
    db = _db()
    gw = TradovateGateway(_settings("credentials"), lambda: db)
    ok, detail = gw.ensure_connected()             # headless: no page, no token yet -> mint
    assert ok is True and detail == "connected"
    assert calls["mint"] == 1 and calls["renew"] == 0
    assert token_store.load_token(db)["access_token"] == "AT1"


def test_credentials_renew_when_expired(monkeypatch):
    monkeypatch.setattr(credentials, "renew_access_token",
                        lambda s, t: {"access_token": "RENEWED", "expires_in": 3600})
    db = _db()
    token_store.save_token(db, {"access_token": "OLD", "expires_in": -5, "refresh_token": None})
    gw = TradovateGateway(_settings("credentials"), lambda: db)
    tok, _ = gw._current_access_token()
    assert tok == "RENEWED"


def test_credentials_remint_if_renew_fails(monkeypatch):
    def bad_renew(s, t):
        raise credentials.CredError("renew rejected")
    monkeypatch.setattr(credentials, "renew_access_token", bad_renew)
    monkeypatch.setattr(credentials, "access_token_request",
                        lambda s: {"access_token": "MINTED2", "expires_in": 3600, "refresh_token": None})
    db = _db()
    token_store.save_token(db, {"access_token": "OLD", "expires_in": -5, "refresh_token": None})
    gw = TradovateGateway(_settings("credentials"), lambda: db)
    tok, _ = gw._current_access_token()
    assert tok == "MINTED2"


def test_oauth_mode_does_not_mint_headlessly():
    db = _db()
    gw = TradovateGateway(_settings("oauth"), lambda: db)   # no token, oauth mode
    ok, detail = gw.ensure_connected()
    assert ok is False and "not connected" in detail.lower()  # must NOT auto-mint


def test_penalty_guard_raises():
    import pytest
    # p-captcha / p-ticket bodies must raise CredError, never be treated as a token.
    with pytest.raises(credentials.CredError):
        credentials._penalty_guard({"p-captcha": True})
    with pytest.raises(credentials.CredError):
        credentials._penalty_guard({"p-ticket": "abc", "p-time": 60})
    credentials._penalty_guard({})                 # clean -> no raise
