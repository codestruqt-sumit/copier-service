"""Signal processing rules: scaling, fan-out, dedup, supersede, cancel, sentinel."""

from __future__ import annotations

from sqlalchemy import select

from app.models import Action, Event, SignalSeen
from app.processor import process_batch, scale_qty


def _cmd(**overrides) -> dict:
    base = {
        "signal_id": 1, "revision": 1, "status": "published", "symbol": "MNQU6",
        "order_kind": "market", "side": "buy", "base_qty": 1, "limit_price": None,
        "stop_price": None, "tif": "day", "target_groups": ["Group 1"], "valid_until": None,
    }
    base.update(overrides)
    return base


def _actions(db, **filters):
    q = select(Action).order_by(Action.id)
    for key, value in filters.items():
        q = q.where(getattr(Action, key) == value)
    return list(db.scalars(q))


# --- quantity scaling -----------------------------------------------------------

def test_scale_qty_ratio_one():
    assert scale_qty(3, "1") == 3


def test_scale_qty_rounds_up_and_floors_at_one():
    assert scale_qty(3, "0.5") == 2      # ceil(1.5)
    assert scale_qty(1, "0.4") == 1      # never below one lot
    assert scale_qty(2, "2") == 4
    assert scale_qty(1, None) == 1       # missing ratio defaults to 1
    assert scale_qty(1, "garbage") == 1  # unparseable ratio defaults to 1


# --- fan-out ----------------------------------------------------------------------

def test_market_fans_out_per_account_with_scaled_qty(db, registration):
    stats = process_batch(db, [_cmd(base_qty=3)], registration)
    assert stats["queued"] == 2  # Group 1 holds Acc-1 (x1) and Acc-2 (x2)
    by_ref = {a.account_ref: a for a in _actions(db)}
    assert by_ref["REF1"].qty == 3
    assert by_ref["REF2"].qty == 6
    assert all(a.kind == "place_market" and a.status == "queued" for a in by_ref.values())


def test_account_in_two_target_groups_gets_one_action(db, registration):
    process_batch(db, [_cmd(target_groups=["Group 1", "Aggressive"])], registration)
    refs = [a.account_ref for a in _actions(db)]
    assert sorted(refs) == ["REF1", "REF2", "REF3"]  # Acc-2 once, not twice


def test_no_matching_group_queues_nothing_but_logs(db, registration):
    stats = process_batch(db, [_cmd(target_groups=["Elsewhere"])], registration)
    assert stats["queued"] == 0
    assert _actions(db) == []
    events = list(db.scalars(select(Event)))
    assert any("no local account" in e.message for e in events)


# --- order kinds ------------------------------------------------------------------

def test_limit_carries_limit_price(db, registration):
    process_batch(db, [_cmd(order_kind="limit", limit_price="28000.00")], registration)
    action = _actions(db)[0]
    assert action.kind == "place_limit"
    assert action.limit_price == "28000.00" and action.stop_price is None


def test_stop_carries_stop_price(db, registration):
    process_batch(db, [_cmd(order_kind="stop", stop_price="27950.00")], registration)
    action = _actions(db)[0]
    assert action.kind == "place_stop"
    assert action.stop_price == "27950.00" and action.limit_price is None


def test_stop_limit_carries_both_prices(db, registration):
    process_batch(
        db,
        [_cmd(order_kind="stop_limit", stop_price="27950.00", limit_price="27960.00")],
        registration,
    )
    action = _actions(db)[0]
    assert action.kind == "place_stop_limit"
    assert action.stop_price == "27950.00" and action.limit_price == "27960.00"


def test_exit_symbol_and_flatten_all_sentinel(db, registration):
    process_batch(db, [
        _cmd(signal_id=1, order_kind="exit", side=None, symbol="MNQU6"),
        _cmd(signal_id=2, order_kind="exit", side=None, symbol="*"),
    ], registration)
    kinds = {a.signal_id: a.kind for a in _actions(db)}
    assert kinds[1] == "exit_symbol"
    assert kinds[2] == "flatten_all"
    assert all(a.qty is None and a.side is None for a in _actions(db))


# --- idempotency / lifecycle ------------------------------------------------------

def test_redelivered_batch_is_deduplicated(db, registration):
    process_batch(db, [_cmd()], registration)
    stats = process_batch(db, [_cmd()], registration)  # same (signal, revision) again
    assert stats["duplicates"] == 1 and stats["queued"] == 0
    assert len(_actions(db)) == 2  # still just the original fan-out


def test_two_distinct_signals_same_symbol_coexist(db, registration):
    process_batch(db, [
        _cmd(signal_id=1, order_kind="market"),
        _cmd(signal_id=2, order_kind="limit", limit_price="28000.00"),
    ], registration)
    assert len(_actions(db, status="queued")) == 4  # 2 accounts x 2 independent signals


def test_new_revision_supersedes_queued_actions(db, registration):
    process_batch(db, [_cmd(order_kind="limit", limit_price="28000.00")], registration)
    process_batch(
        db,
        [_cmd(revision=2, status="updated", order_kind="limit", limit_price="27990.00")],
        registration,
    )
    old = _actions(db, revision=1)
    new = _actions(db, revision=2)
    assert all(a.status == "superseded" for a in old)
    assert all(a.status == "queued" and a.limit_price == "27990.00" for a in new)


def test_cancel_kills_queued_actions(db, registration):
    process_batch(db, [_cmd()], registration)
    process_batch(db, [_cmd(revision=2, status="cancelled")], registration)
    assert all(a.status == "cancelled" for a in _actions(db, revision=1))
    assert _actions(db, revision=2) == []  # a cancel enqueues nothing new


def test_expired_on_arrival_is_skipped(db, registration):
    stats = process_batch(
        db, [_cmd(valid_until="2020-01-01T00:00:00+00:00")], registration
    )
    assert stats["skipped"] == 1
    assert _actions(db) == []
    assert any("valid_until" in e.message for e in db.scalars(select(Event)))


def test_every_received_signal_is_recorded(db, registration):
    process_batch(db, [_cmd(signal_id=7), _cmd(signal_id=8, order_kind="exit", side=None)], registration)
    seen = list(db.scalars(select(SignalSeen).order_by(SignalSeen.signal_id)))
    assert [s.signal_id for s in seen] == [7, 8]
    assert seen[0].raw["order_kind"] == "market"


# --- resilience -------------------------------------------------------------------

def test_poison_command_is_isolated(db, registration):
    """One malformed command must not wedge the batch (that would block ALL signals)."""
    poison = {"garbage": True}  # no signal_id, no anything
    stats = process_batch(db, [poison, _cmd(signal_id=9)], registration)
    assert stats["failed"] == 1
    assert stats["queued"] == 2                       # the good command still fanned out
    assert len(_actions(db, signal_id=9)) == 2
    events = list(db.scalars(select(Event)))
    assert any(e.level == "error" and "Failed to process" in e.message for e in events)


def test_malformed_valid_until_does_not_crash(db, registration):
    stats = process_batch(db, [_cmd(valid_until=12345)], registration)  # not a string
    assert stats["failed"] == 0 and stats["queued"] == 2  # treated as no deadline
