"""Mode selection is ENV-ONLY: COPIER_MODE decides at boot; no runtime override."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.gateways import build_gateway


def _settings(mode):
    return SimpleNamespace(copier_mode=mode, market_fast_path=True, net_verify_sec=12.0,
                           tradovate_auth="oauth",
                           tradovate_base="https://demo.tradovateapi.com/v1",
                           http_timeout_sec=5.0)


def test_env_web_builds_selenium_gateway():
    assert type(build_gateway(_settings("web"))).__name__ == "TerminalGateway"


def test_env_api_builds_tradovate_gateway():
    assert type(build_gateway(_settings("api"))).__name__ == "TradovateGateway"


def test_unset_defaults_to_web():
    assert type(build_gateway(_settings(None))).__name__ == "TerminalGateway"


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        build_gateway(_settings("carrier-pigeon"))
