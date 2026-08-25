"""Position-read correctness against VIRTUALIZED rows (no Selenium needed).

Proven live (ATS 2026-08-24): the Positions widget keeps one row per traded symbol all
session, rows below the fold are virtualized OUT of the DOM, and visible-only reads
returned net 0 for real MGCZ6 fills -> false "SUBMITTED but not verified" failures and
a double-send-prone ticket fallback. These tests pin the fix: decision reads fall back
to a scrolled read whenever the row is not among the visible rows.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.terminal import TerminalGateway


class FakePositions:
    """visible = rows currently rendered; full = every row (what scrolling reveals)."""

    def __init__(self, visible: dict | None = None, full: dict | None = None):
        self.visible = visible or {}
        self.full = dict(self.visible) | (full or {})
        self.scroll_reads = 0

    def get(self, symbol, scroll=True):
        if scroll:
            self.scroll_reads += 1
        table = self.full if scroll else self.visible
        found = symbol in table
        return SimpleNamespace(
            ok=True,
            data={"found": found,
                  "position": {"net_pos": table[symbol]} if found else None})


def gateway(positions) -> TerminalGateway:
    g = TerminalGateway(net_verify_sec=0.4)
    g.positions = positions
    return g


def test_visible_row_needs_no_scroll():
    fake = FakePositions(visible={"MNQU6": -1})
    g = gateway(fake)
    assert g._net_thorough("MNQU6") == -1
    assert fake.scroll_reads == 0          # found visibly -> never drags


def test_off_screen_row_found_by_thorough_read():
    fake = FakePositions(visible={"MNQU6": 0}, full={"MGCZ6": -1})
    g = gateway(fake)
    assert g._net("MGCZ6") == 0            # the old visible-only lie
    assert g._net_thorough("MGCZ6") == -1  # the truth, via one scrolled read
    assert fake.scroll_reads == 1


def test_absent_row_reads_zero():
    fake = FakePositions(visible={}, full={})
    g = gateway(fake)
    assert g._net_thorough("SIU6") == 0


def test_await_net_verifies_off_screen_fill():
    """A fill whose row is off-screen must still verify (this was the false
    'SUBMITTED but not verified' failure)."""
    fake = FakePositions(visible={"MNQU6": 0}, full={"MGCZ6": -1})
    g = gateway(fake)
    assert g._await_net("MGCZ6", -1) is True


def test_await_net_zero_not_fooled_by_hidden_position():
    """Exit verification must NOT report flat while a hidden row still holds a
    position - visible-only 0 is not proof of flatness."""
    fake = FakePositions(visible={"MNQU6": 0}, full={"MGCZ6": -1})
    g = gateway(fake)
    assert g._await_net("MGCZ6", 0) is False


def test_await_net_fast_path_on_visible_match():
    fake = FakePositions(visible={"MNQU6": 2})
    g = gateway(fake)
    assert g._await_net("MNQU6", 2) is True
    assert fake.scroll_reads == 0          # matched visibly -> no drag at all


# --- ticket-market classification: success must not be misreported as "submitted" -------

class FakeTicket:
    """place() result stub: ok + meta.submitted the way order_ticket returns them."""

    def __init__(self, ok=True, submitted=True, error=None):
        self._r = SimpleNamespace(ok=ok, error=error,
                                  meta={"submitted": submitted, "attempts": 1}, data={})

    def place(self, *a, **k):
        return self._r


def test_successful_ticket_market_reports_filled_not_submitted():
    """A MARKET send returns ok=True AND meta.submitted=True; that must verify via the
    net and report FILLED - the old code failed it as 'SUBMITTED but not verified'
    (the empty trailing error in every such note was this bug's fingerprint)."""
    fake_pos = FakePositions(visible={"SIU6": -1})     # the fill is on the board
    g = gateway(fake_pos)
    g.ticket = FakeTicket(ok=True, submitted=True)
    out = g._market_via_ticket("SIU6", "sell", 1, before=0, expected=-1)
    assert out["outcome"] == "filled"
    assert "net 0 -> -1" in out["detail"]


def test_failed_submitted_ticket_market_stays_loud():
    """ok=False + submitted=True = sent but its own verify failed -> loud failure,
    never retried."""
    g = gateway(FakePositions())
    g.ticket = FakeTicket(ok=False, submitted=True, error="orders grid empty")
    out = g._market_via_ticket("SIU6", "sell", 1, before=0, expected=-1)
    assert out["outcome"] == "failed"
    assert "SUBMITTED but not verified" in out["detail"]
    assert "orders grid empty" in out["detail"]


def test_successful_ticket_market_with_invisible_fill_still_verifies():
    """Success + the fill row off-screen: the thorough read must still confirm it."""
    fake_pos = FakePositions(visible={"MNQU6": 0}, full={"SIU6": -1})
    g = gateway(fake_pos)
    g.ticket = FakeTicket(ok=True, submitted=True)
    out = g._market_via_ticket("SIU6", "sell", 1, before=0, expected=-1)
    assert out["outcome"] == "filled"


# --- speed package: tab-switch early-exit + live/verified annotation ---------------------

def test_ensure_tab_skips_switch_when_panel_already_on_symbol():
    g = gateway(FakePositions())
    g.driver = SimpleNamespace(execute_script=lambda js: "MNQU6")   # panel shows it
    calls = []
    g.tabs = SimpleNamespace(switch_to=lambda s: calls.append(s) or SimpleNamespace(ok=True))
    out = g._ensure_tab("MNQU6")
    assert out.ok is True and calls == []          # no full switch performed


def test_ensure_tab_switches_on_mismatch_or_unknown():
    g = gateway(FakePositions())
    g.driver = SimpleNamespace(execute_script=lambda js: "MGCZ6")
    calls = []
    g.tabs = SimpleNamespace(
        switch_to=lambda s: calls.append(s) or SimpleNamespace(ok=True, error=None))
    assert g._ensure_tab("SIU6").ok is True
    assert calls == ["SIU6"]                       # mismatch -> real switch

    g.driver = SimpleNamespace(execute_script=lambda js: (_ for _ in ()).throw(RuntimeError()))
    assert g._ensure_tab("SIU6").ok is True        # read failure -> safe fallback switch
    assert calls == ["SIU6", "SIU6"]


def test_latency_annotation_on_live_results():
    import time as _t

    g = gateway(FakePositions())
    t0 = _t.monotonic()
    g._live_at = t0 + 0.5                          # order went live 0.5s in
    out = g._annotate_latency({"outcome": "filled", "detail": "net 0 -> 1"}, t0)
    assert "[live +0.5s, verified +" in out["detail"]

    g._live_at = None                              # nothing went live -> no annotation
    out2 = g._annotate_latency({"outcome": "failed", "detail": "x"}, t0)
    assert out2["detail"] == "x"


# --- orders grid: a fresh resting order below the fold must still be found --------------

class FakeOrders:
    """report(scroll=False) = visible working rows; scroll=True = the full grid."""

    def __init__(self, visible=None, full=None):
        self.visible = visible or []
        self.full = (full or []) + self.visible
        self.scroll_reads = 0

    def report(self, scroll=True):
        if scroll:
            self.scroll_reads += 1
        rows = self.full if scroll else self.visible
        return SimpleNamespace(ok=True, data={"working": rows})


def test_working_order_below_fold_found_by_thorough_read():
    g = gateway(FakePositions())
    g.orders = FakeOrders(visible=[], full=[{"id": "999111", "contract": "MGCZ6 ..."}])
    assert g._find_working_ref("MGCZ6") is None            # the old visible-only miss
    assert g._find_working_thorough("MGCZ6") == "999111"   # scrolled read finds it
    assert g.orders.scroll_reads == 1


def test_working_order_visible_needs_no_scroll():
    g = gateway(FakePositions())
    g.orders = FakeOrders(visible=[{"id": "42", "contract": "SIU6 ..."}])
    assert g._find_working_thorough("SIU6") == "42"
    assert g.orders.scroll_reads == 0


# --- bid/ask safe re-fire: retry ONLY when provably nothing landed -----------------------

class FakeFirePanel:
    """buy_bid/sell_ask stub: the click 'takes' only on `lands_on` attempt (None = never),
    landing either a FILL (mutates positions) or a RESTING order (mutates orders)."""

    def __init__(self, positions, orders, symbol, lands_on=None, mode="fill", net=1):
        self.positions, self.orders = positions, orders
        self.symbol, self.lands_on, self.mode, self.net = symbol, lands_on, mode, net
        self.fires = 0

    def set_qty(self, q):
        return SimpleNamespace(ok=True, error=None, meta={})

    def _fire(self, **k):
        self.fires += 1
        if self.lands_on is not None and self.fires >= self.lands_on:
            if self.mode == "fill":
                self.positions.visible[self.symbol] = self.net
                self.positions.full[self.symbol] = self.net
            else:  # resting order below the fold (visible scan misses it)
                self.orders.full.append({"id": "777", "contract": f"{self.symbol} ..."})
        return SimpleNamespace(ok=True, error=None, data={"confirmation": "x"}, meta={})

    buy_bid = _fire
    sell_ask = _fire


def bid_gateway(symbol="SIU6", lands_on=None, mode="fill", net=1):
    pos = FakePositions()
    orders = FakeOrders()
    g = gateway(pos)
    g.orders = orders
    g.driver = SimpleNamespace(execute_script=lambda js: symbol)  # _ensure_tab skips
    g.panel = FakeFirePanel(pos, orders, symbol, lands_on=lands_on, mode=mode, net=net)
    g.classify_wait_sec = 0.2                   # keep the classify polls fast in tests
    return g


def test_bid_refire_recovers_a_missed_click():
    """First fire lands nothing (provably); the safe re-fire lands the fill."""
    g = bid_gateway(lands_on=2, mode="fill", net=1)
    out = g._bid_ask({"kind": "place_bid", "symbol": "SIU6", "qty": 1})
    assert g.panel.fires == 2
    assert out["outcome"] == "filled"
    assert "safe re-fire" in out["detail"]


def test_bid_no_refire_when_first_fire_fills():
    g = bid_gateway(lands_on=1, mode="fill", net=1)
    out = g._bid_ask({"kind": "place_bid", "symbol": "SIU6", "qty": 1})
    assert g.panel.fires == 1                   # anything landed -> never re-fire
    assert out["outcome"] == "filled" and "re-fire" not in out["detail"]


def test_bid_no_refire_when_order_rests_below_fold():
    """A rest below the Orders fold is FOUND by the thorough read -> executing+ref,
    and crucially NO second fire (that would double the order)."""
    g = bid_gateway(lands_on=1, mode="rest")
    out = g._bid_ask({"kind": "place_bid", "symbol": "SIU6", "qty": 1})
    assert g.panel.fires == 1
    assert out["outcome"] == "executing" and out["order_ref"] == "777"


def test_bid_two_empty_fires_report_honestly():
    g = bid_gateway(lands_on=None)              # the click never takes
    out = g._bid_ask({"kind": "place_ask", "symbol": "SIU6", "qty": 1})
    assert g.panel.fires == 2
    assert out["outcome"] == "executing"
    assert "sent twice" in out["detail"]
