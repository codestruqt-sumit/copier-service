"""Mode selection + segregation: the factory returns exactly the selected gateway, both
satisfy the seam, and the unfinished API mode fails LOUDLY rather than silently no-opping.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.gateways import build_gateway, WEB, API, MODES
from app.gateways.base import GatewayProtocol, SEAM_METHODS


def _settings(mode):
    # Only the fields the factory / gateways read at construction.
    return SimpleNamespace(
        copier_mode=mode,
        market_fast_path=True,
        net_verify_sec=12.0,
        tradovate_auth="oauth",
    )


def test_web_mode_builds_the_selenium_gateway():
    gw = build_gateway(_settings("web"))
    assert type(gw).__name__ == "TerminalGateway"
    # It satisfies the seam contract (structural).
    assert isinstance(gw, GatewayProtocol)
    for m in SEAM_METHODS:
        assert callable(getattr(gw, m)), f"web gateway missing seam method {m}"


def test_api_mode_builds_the_tradovate_stub_not_a_browser():
    gw = build_gateway(_settings("api"))
    assert type(gw).__name__ == "TradovateGateway"
    assert isinstance(gw, GatewayProtocol)
    for m in SEAM_METHODS:
        assert callable(getattr(gw, m)), f"api gateway missing seam method {m}"


def test_mode_is_case_insensitive_and_defaults_to_web():
    assert type(build_gateway(_settings("WEB"))).__name__ == "TerminalGateway"
    assert type(build_gateway(_settings(None))).__name__ == "TerminalGateway"     # default
    assert type(build_gateway(_settings("  api "))).__name__ == "TradovateGateway"


def test_unknown_mode_raises_not_silently_falls_back():
    with pytest.raises(ValueError) as ei:
        build_gateway(_settings("carrier-pigeon"))
    assert "carrier-pigeon" in str(ei.value)


def test_api_execute_never_silently_succeeds():
    # API execute must never return a fake success. With no session/token it FAILS
    # loudly rather than reporting a fill.
    gw = build_gateway(_settings("api"))
    out = gw.execute({"kind": "place_market", "symbol": "MNQU6", "side": "buy",
                      "qty": 1, "account_ref": "ACC1", "tif": "day",
                      "limit_price": None, "stop_price": None})
    assert out["outcome"] == "failed"


def test_api_session_checks_hold_not_crash():
    """ensure_connected / login_check return (False, reason) so the executor HOLDS the
    action (never trades) instead of the loop crashing."""
    gw = build_gateway(_settings("api"))            # session_factory defaults to None
    ok, detail = gw.ensure_connected()
    assert ok is False and detail                    # holds, with a human reason
    ok2, _ = gw.login_check()
    assert ok2 is False


def test_modes_constant():
    assert MODES == (WEB, API) == ("web", "api")
