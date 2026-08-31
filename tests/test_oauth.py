"""OAuth flow: URL building, token store lifecycle, callback CSRF, gateway session gate,
and that mounting the routes does not disturb the web path.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.gateways.api import oauth, token_store
from app.gateways.api.gateway import TradovateGateway
from app.models import Base


def _settings(**over):
    base = dict(
        copier_mode="api", tradovate_auth="oauth",
        tradovate_base="https://demo.tradovateapi.com/v1",
        tradovate_oauth_authorize="https://trader.tradovate.com/oauth",
        tradovate_client_id="585",
        tradovate_client_secret=SecretStr("shh-secret"),
        tradovate_redirect_uri="",
        dashboard_port=8100,
        http_timeout_sec=5.0,
    )
    base.update(over)
    s = SimpleNamespace(**base)
    # mimic settings.oauth_redirect_uri
    s.oauth_redirect_uri = (base["tradovate_redirect_uri"]
                            or f"http://localhost:{base['dashboard_port']}/oauth/tradovate/callback")
    return s


def _db():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def test_authorize_url_has_the_required_params():
    url = oauth.authorize_url(_settings(), "st4te")
    assert url.startswith("https://trader.tradovate.com/oauth?")
    assert "response_type=code" in url
    assert "client_id=585" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8100%2Foauth%2Ftradovate%2Fcallback" in url
    assert "state=st4te" in url


def test_secret_never_appears_in_authorize_url():
    assert "shh-secret" not in oauth.authorize_url(_settings(), "s")


def test_token_store_roundtrip_and_expiry():
    db = _db()
    rec = token_store.save_token(db, {
        "access_token": "AT", "refresh_token": "RT", "token_type": "Bearer",
        "expires_in": 3600, "refresh_token_expires_in": 86400})
    assert token_store.load_token(db)["access_token"] == "AT"
    assert token_store.access_expired(rec) is False
    assert token_store.refresh_expired(rec) is False
    # a token with no expiry is treated as expired (so we refresh, not trust forever)
    stale = token_store.save_token(db, {"access_token": "X", "refresh_token": "Y"})
    assert token_store.access_expired(stale) is True


def test_token_store_expired_when_past_expiry():
    db = _db()
    rec = token_store.save_token(db, {"access_token": "AT", "expires_in": -10})
    assert token_store.access_expired(rec) is True


def test_state_is_read_once():
    db = _db()
    token_store.save_state(db, "abc")
    assert token_store.pop_state(db) == "abc"
    assert token_store.pop_state(db) is None       # single-use


def test_gateway_holds_when_no_token():
    sf = lambda: _db()
    gw = TradovateGateway(_settings(), sf)
    ok, detail = gw.ensure_connected()
    assert ok is False and "not connected" in detail.lower()


def test_gateway_connected_with_valid_token():
    db = _db()
    token_store.save_token(db, {"access_token": "AT", "refresh_token": "RT",
                                "expires_in": 3600})
    gw = TradovateGateway(_settings(), lambda: db)
    ok, detail = gw.ensure_connected()
    assert ok is True and detail == "connected"


def test_gateway_execute_fails_not_raises_when_broker_unreachable():
    # execute() is implemented now: it returns a FAILED dict (never raises, never
    # silently claims a fill) when it cannot reach/verify the broker.
    db = _db()
    token_store.save_token(db, {"access_token": "AT", "expires_in": 3600})
    def boom(_tok):
        raise RuntimeError("no network in unit test")
    gw = TradovateGateway(_settings(), lambda: db, rest_factory=boom)
    out = gw.execute({"kind": "place_market", "symbol": "MNQU6", "side": "buy",
                      "qty": 1, "account_ref": "ACC1", "tif": "day",
                      "limit_price": None, "stop_price": None})
    assert out["outcome"] == "failed"
