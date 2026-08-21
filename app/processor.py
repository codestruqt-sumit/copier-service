"""Signal processing: turn Sender commands into per-account queue actions.

Rules (each covered by tests):
  * Idempotent by (signal_id, revision) - a re-delivered batch enqueues nothing twice.
  * One Action per (signal, revision, account): the executor later does the terminal
    mechanics (switch account, switch symbol, place) as sub-steps of that one action.
  * qty = max(1, ceil(base_qty x copy_ratio)) - the copier owns the scaling.
  * A new revision SUPERSEDES the older revision's still-queued actions.
  * A cancelled/expired command cancels its still-queued actions. (Cancelling work
    that already reached the terminal becomes a cancel_order action in Part 3.)
  * order_kind 'exit' with symbol '*' is the Sender's flatten-all sentinel.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from math import ceil

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import telegram as _tg
from app.activity_file import append_activity
from app.config import settings
from app.models import Action, Event, SignalSeen

FLATTEN_ALL_SYMBOL = "*"

# One console line per activity so the operator can watch the running terminal.
_activity_log = logging.getLogger("copier.activity")
_LEVEL_INT = {"info": logging.INFO, "warn": logging.WARNING,
              "warning": logging.WARNING, "error": logging.ERROR, "debug": logging.DEBUG}

ACTION_KIND_BY_ORDER = {
    "market": "place_market",
    "bid": "place_bid",
    "ask": "place_ask",
    "limit": "place_limit",
    "stop": "place_stop",
    "stop_limit": "place_stop_limit",
}

OPEN_STATUSES = {"published", "updated"}
CLOSED_STATUSES = {"cancelled", "expired"}


def scale_qty(base_qty: int, copy_ratio: str | Decimal | None) -> int:
    """Per-account size: base x ratio, rounded UP, never below one lot."""
    try:
        ratio = Decimal(str(copy_ratio)) if copy_ratio is not None else Decimal("1")
    except (InvalidOperation, ValueError):
        ratio = Decimal("1")
    return max(1, ceil(Decimal(int(base_qty)) * ratio))


def log_event(db: Session, level: str, category: str, message: str, data: dict | None = None) -> None:
    """One activity fans out three ways: the DB (dashboard), a machine-readable JSONL
    file (the ATS), and the console (so the operator can watch the terminal). The DB row
    is the source of truth; the file + console mirrors never raise into the caller."""
    db.add(Event(level=level, category=category, message=message, data=data or {}))
    try:
        append_activity(level, category, message, data)
    except Exception:  # noqa: BLE001
        pass
    try:
        _activity_log.log(_LEVEL_INT.get(level, logging.INFO), "[%s] %s", category, message)
    except Exception:  # noqa: BLE001
        pass
    # Opt-in firehose: mirror the whole activity log to the Telegram activity topic.
    # Fire-and-forget + swallowed, so it never affects processing.
    if getattr(settings, "telegram_activity_all", False):
        try:
            _tg.notifier.send("activity", f"[{level}] {category}: {message}")
        except Exception:  # noqa: BLE001
            pass


def target_accounts(registration: dict, target_groups: list[str]) -> list[dict]:
    """This copier's accounts that belong to any of the command's target groups."""
    wanted = set(target_groups or [])
    out = []
    for account in registration.get("accounts", []):
        if wanted & set(account.get("groups", [])):
            out.append(account)
    return out


def _expired(cmd: dict) -> bool:
    raw = cmd.get("valid_until")
    if not raw:
        return False
    try:
        deadline = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) > deadline


def _action_kind(cmd: dict) -> str:
    if cmd["order_kind"] == "exit":
        return "flatten_all" if cmd["symbol"] == FLATTEN_ALL_SYMBOL else "exit_symbol"
    return ACTION_KIND_BY_ORDER.get(cmd["order_kind"], "place_market")


def process_batch(db: Session, commands: list[dict], registration: dict) -> dict:
    """Apply one poll batch with PER-COMMAND isolation.

    Each command commits on its own; a malformed one rolls back, logs an error event,
    and the loop moves on - one poison command must never wedge the copier and block
    every signal behind it. (The cursor still advances; a skipped command is
    re-delivered if the Sender revises it, and the error is loud on the dashboard.)
    """
    stats = {"received": 0, "queued": 0, "cancelled": 0, "superseded": 0,
             "skipped": 0, "duplicates": 0, "failed": 0}
    for cmd in commands:
        try:
            _process_one(db, cmd, registration, stats)
            db.commit()
        except Exception as exc:  # noqa: BLE001 - isolate the poison command
            db.rollback()
            stats["failed"] += 1
            try:
                signal_ref = cmd.get("signal_id", "?") if isinstance(cmd, dict) else "?"
                log_event(db, "error", "process",
                          f"Failed to process command {signal_ref}: {type(exc).__name__}: {exc}",
                          {"command": str(cmd)[:500]})
                db.commit()
            except Exception:  # noqa: BLE001 - even the error log must not break the loop
                db.rollback()
    return stats


def _process_one(db: Session, cmd: dict, registration: dict, stats: dict) -> None:
    signal_id, revision = int(cmd["signal_id"]), int(cmd.get("revision", 1))

    already = db.scalar(
        select(SignalSeen).where(
            SignalSeen.signal_id == signal_id, SignalSeen.revision == revision
        )
    )
    if already is not None:
        stats["duplicates"] += 1
        return

    stats["received"] += 1
    db.add(SignalSeen(
        signal_id=signal_id,
        revision=revision,
        status=cmd.get("status", ""),
        symbol=cmd.get("symbol", ""),
        order_kind=cmd.get("order_kind", ""),
        side=cmd.get("side"),
        base_qty=int(cmd.get("base_qty") or 1),
        limit_price=cmd.get("limit_price"),
        stop_price=cmd.get("stop_price"),
        tif=cmd.get("tif", "day"),
        target_groups=cmd.get("target_groups") or [],
        valid_until=cmd.get("valid_until"),
        raw=cmd,
    ))

    status = cmd.get("status", "")
    label = f"#{signal_id} r{revision} {cmd.get('order_kind', '?')} {cmd.get('symbol', '?')}"

    if status in CLOSED_STATUSES:
        # Kill anything of this signal still waiting in the queue.
        pending = list(db.scalars(
            select(Action).where(Action.signal_id == signal_id, Action.status == "queued")
        ))
        for action in pending:
            action.status = "cancelled"
            action.note = f"signal {status} at revision {revision}"
        stats["cancelled"] += len(pending)
        log_event(db, "info", "signal", f"Signal {label} {status} - "
                  f"{len(pending)} queued action(s) cancelled", {"signal_id": signal_id})

        # Orders ALREADY placed on the terminal: automated per-order cancel is not
        # built yet (the validated cancel path is symbol-wide Exit-at-Mkt&Cxl, which
        # would also flatten unrelated positions). Be LOUD instead of destructive.
        placed = list(db.scalars(
            select(Action).where(
                Action.signal_id == signal_id,
                Action.status == "done",
                Action.kind.in_(["place_limit", "place_stop", "place_bid",
                                 "place_ask", "place_stop_limit"]),
            )
        ))
        if placed:
            refs = ", ".join(
                f"{a.account_alias or a.account_ref}"
                + (f" (order {a.order_ref})" if a.order_ref else "")
                for a in placed
            )
            log_event(db, "error", "signal",
                      f"Signal {label} {status} but its order was ALREADY PLACED on: "
                      f"{refs} - cancel manually in the terminal "
                      f"(automated per-order cancel not yet supported)")
        return

    if status not in OPEN_STATUSES:
        stats["skipped"] += 1
        log_event(db, "warn", "signal", f"Signal {label}: unknown status '{status}', skipped")
        return

    if _expired(cmd):
        stats["skipped"] += 1
        log_event(db, "warn", "signal", f"Signal {label} already past valid_until - skipped")
        return

    # A newer revision replaces the older one's queued work.
    stale = list(db.scalars(
        select(Action).where(
            Action.signal_id == signal_id, Action.revision < revision, Action.status == "queued"
        )
    ))
    for action in stale:
        action.status = "superseded"
        action.note = f"superseded by revision {revision}"
    if stale:
        stats["superseded"] += len(stale)
        log_event(db, "info", "signal",
                  f"Signal {label}: superseded {len(stale)} action(s) from older revisions")

    accounts = target_accounts(registration, cmd.get("target_groups") or [])
    if not accounts:
        log_event(db, "warn", "signal",
                  f"Signal {label}: no local account in target groups {cmd.get('target_groups')}")
        return

    kind = _action_kind(cmd)
    is_exit = kind in {"exit_symbol", "flatten_all"}
    for account in accounts:
        exists = db.scalar(select(Action).where(
            Action.signal_id == signal_id,
            Action.revision == revision,
            Action.account_ref == account["account_ref"],
        ))
        if exists is not None:
            continue
        db.add(Action(
            signal_id=signal_id,
            revision=revision,
            account_ref=account["account_ref"],
            account_alias=account.get("alias", ""),
            kind=kind,
            symbol=cmd.get("symbol", ""),
            side=None if is_exit else cmd.get("side"),
            qty=None if is_exit else scale_qty(int(cmd.get("base_qty") or 1), account.get("copy_ratio")),
            limit_price=cmd.get("limit_price"),
            stop_price=cmd.get("stop_price"),
            tif=cmd.get("tif", "day"),
            status="queued",
        ))
        stats["queued"] += 1

    log_event(db, "info", "queue",
              f"Signal {label} -> {len(accounts)} action(s) queued "
              f"({', '.join(a.get('alias', a['account_ref']) for a in accounts)})",
              {"signal_id": signal_id, "revision": revision})
