"""Local storage model: received signals, the action queue, the activity log, and KV.

Statuses on Action:
  queued     - waiting for the (Part 3) terminal executor
  cancelled  - the Sender cancelled the signal before execution started
  superseded - a newer revision of the same signal replaced this action
  skipped    - not actionable on arrival (e.g. expired valid_until)
  executing / done / failed - reserved for Part 3 (terminal execution)
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class KV(Base):
    """Tiny key/value store: poll cursor, cached registration, run metadata."""

    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")


class SignalSeen(Base):
    """Every (signal, revision) the copier has received - the reception audit trail."""

    __tablename__ = "signals_seen"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    order_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str | None] = mapped_column(String(8))
    base_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    limit_price: Mapped[str | None] = mapped_column(String(32))
    stop_price: Mapped[str | None] = mapped_column(String(32))
    tif: Mapped[str] = mapped_column(String(8), nullable=False, default="day")
    target_groups: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    valid_until: Mapped[str | None] = mapped_column(String(40))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    raw: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (UniqueConstraint("signal_id", "revision", name="uq_signal_revision"),)


class Action(Base):
    """One unit of terminal work: one signal applied to one local account."""

    __tablename__ = "actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    account_ref: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    account_alias: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    # place_market / place_bid / place_ask / place_limit / place_stop /
    # place_stop_limit / exit_symbol / flatten_all  (+ cancel_order in Part 3)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str | None] = mapped_column(String(8))
    qty: Mapped[int | None] = mapped_column(Integer)
    limit_price: Mapped[str | None] = mapped_column(String(32))
    stop_price: Mapped[str | None] = mapped_column(String(32))
    tif: Mapped[str] = mapped_column(String(8), nullable=False, default="day")
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="queued", index=True)
    # The broker order id once the executor places a resting order (audit + cancels).
    order_ref: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint("signal_id", "revision", "account_ref", name="uq_action_per_account"),
    )


class Event(Base):
    """The activity log the dashboard shows - what happened, in order."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(8), nullable=False, default="info")  # info/warn/error
    category: Mapped[str] = mapped_column(String(24), nullable=False, default="general")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
