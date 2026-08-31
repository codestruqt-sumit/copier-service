"""API-mode order execution mirrors the web automation: verify-before-claim, visible
rejections, never retry an ambiguous send, honest classification. Driven by a fake REST -
no network, no browser.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app.gateways.api.client import AmbiguousSend
from app.gateways.api.gateway import TradovateGateway


class FakeRest:
    """A tiny Tradovate stand-in. `place_behaviour` decides what each placeorder does."""

    def __init__(self, *, net_after_place=None, order_status="Filled", place_resp=None,
                 raise_on_place=None, positions=None):
        self._net = {}                        # (accountId, contractId) -> netPos
        self._net_after = net_after_place      # net to jump to after a successful place
        self._order_status = order_status
        self._place_resp = place_resp          # override the placeorder response dict
        self._raise = raise_on_place
        self._positions = positions or []
        self.placed = []
        self.cancelled = []

    def account_list(self):
        return [{"id": 111, "name": "ACC1"}]

    def contract_find(self, symbol):
        return {"id": 4399654, "name": symbol}

    def position_list(self):
        rows = [{"accountId": 111, "contractId": cid, "netPos": n}
                for (aid, cid), n in self._net.items()]
        return rows + self._positions

    def order_list(self):
        return []

    def order_item(self, order_id):
        return {"id": order_id, "ordStatus": self._order_status}

    def place_order(self, payload):
        if self._raise:
            raise self._raise
        self.placed.append(payload)
        if self._place_resp is not None:
            return self._place_resp
        if self._net_after is not None:
            self._net[(payload["accountId"], 4399654)] = self._net_after
        return {"orderId": 900001}

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return {"commandId": 1}


def _settings():
    return SimpleNamespace(
        tradovate_base="https://demo.tradovateapi.com/v1", tradovate_auth="oauth",
        net_verify_sec=1.0, http_timeout_sec=5.0, tradovate_client_id="585",
        tradovate_client_secret=SecretStr("x"))


def _gw(rest):
    gw = TradovateGateway(_settings(), session_factory=None, rest_factory=lambda tok: rest)
    gw._current_access_token = lambda: ("AT", "connected")   # pretend connected
    return gw


def _act(kind, side="buy", qty=1, **extra):
    return {"kind": kind, "symbol": "MNQU6", "side": side, "qty": qty,
            "tif": "day", "account_ref": "ACC1", "limit_price": None,
            "stop_price": None, **extra}


def test_market_verified_fill():
    rest = FakeRest(net_after_place=1)          # buy 1 -> net becomes 1
    out = _gw(rest).execute(_act("place_market", "buy", 1))
    assert out["outcome"] == "filled" and out["order_ref"] == "900001"
    assert "net 0 -> 1 (api market)" in out["detail"]
    assert "[live +" in out["detail"]           # latency annotation present


def test_market_rejection_is_visible():
    rest = FakeRest(place_resp={"failureReason": "InsufficientMargin",
                                "failureText": "not enough margin"})
    out = _gw(rest).execute(_act("place_market", "buy", 1))
    assert out["outcome"] == "failed"
    assert "InsufficientMargin" in out["detail"]     # the thing web mode could never see


def test_market_accepted_but_net_not_moving_is_honest():
    rest = FakeRest(net_after_place=None)       # placed, but net never changes
    out = _gw(rest).execute(_act("place_market", "sell", 1))
    assert out["outcome"] == "executing"
    assert "not verified" in out["detail"]


def test_ambiguous_send_is_never_retried_and_loud():
    rest = FakeRest(raise_on_place=AmbiguousSend("sent but no response"))
    out = _gw(rest).execute(_act("place_market", "buy", 1))
    assert out["outcome"] == "failed"
    assert "AMBIGUOUS" in out["detail"] and len(rest.placed) == 0


def test_limit_rests_working():
    rest = FakeRest(order_status="Working")
    out = _gw(rest).execute(_act("place_limit", "buy", 1, limit_price="20000.25"))
    assert out["outcome"] == "executing" and "working" in out["detail"]
    assert rest.placed[0]["orderType"] == "Limit" and rest.placed[0]["price"] == 20000.25


def test_bid_without_price_fails_loudly_never_market():
    rest = FakeRest()
    out = _gw(rest).execute(_act("place_bid", "buy", 1))       # no limit_price
    assert out["outcome"] == "failed"
    assert "market data" in out["detail"] and len(rest.placed) == 0   # never silently placed


def test_bid_with_price_is_a_limit():
    rest = FakeRest(order_status="Working")
    out = _gw(rest).execute(_act("place_bid", "buy", 1, limit_price="19999"))
    assert out["outcome"] == "executing"
    assert rest.placed[0]["orderType"] == "Limit"


def test_exit_flattens_and_verifies():
    rest = FakeRest()
    rest._net[(111, 4399654)] = -2              # short 2
    def place(payload):
        rest.placed.append(payload)
        rest._net[(111, 4399654)] = 0           # the buy-2 flattens it
        return {"orderId": 5}
    rest.place_order = place
    out = _gw(rest).execute(_act("exit_symbol"))
    assert out["outcome"] == "filled" and "flat (net verified)" in out["detail"]
    assert rest.placed[0]["action"] == "Buy" and rest.placed[0]["orderQty"] == 2


def test_exit_already_flat():
    rest = FakeRest()                            # net 0
    out = _gw(rest).execute(_act("exit_symbol"))
    assert out["outcome"] == "filled" and "already flat" in out["detail"]
    assert len(rest.placed) == 0


def test_isAutomated_always_set():
    rest = FakeRest(net_after_place=1)
    _gw(rest).execute(_act("place_market", "buy", 1))
    assert rest.placed[0]["isAutomated"] is True
