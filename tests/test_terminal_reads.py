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
