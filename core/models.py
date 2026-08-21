"""Domain types shared across the bot. No Selenium, no I/O - pure data."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Direction(Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"          # "close whatever is open on this symbol"

    @classmethod
    def parse(cls, raw: Any) -> "Direction":
        if isinstance(raw, cls):
            return raw
        text = str(raw).strip().lower()
        aliases = {
            "long": cls.LONG, "buy": cls.LONG, "b": cls.LONG, "1": cls.LONG,
            "short": cls.SHORT, "sell": cls.SHORT, "s": cls.SHORT, "-1": cls.SHORT,
            "flat": cls.FLAT, "close": cls.FLAT, "exit": cls.FLAT, "0": cls.FLAT,
        }
        if text not in aliases:
            raise ValueError(f"unrecognised trade direction: {raw!r}")
        return aliases[text]

    @property
    def is_entry(self) -> bool:
        return self is not Direction.FLAT


class SignalStatus(Enum):
    PENDING = "pending"
    EXECUTED = "executed"
    REJECTED = "rejected"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class Signal:
    """One instruction from the signal source.

    `balance_ratio` is the fraction of account balance to commit (0.02 == 2%).
    `qty` may be supplied directly; when it is, sizing is bypassed entirely.
    """

    id: str
    symbol: str
    direction: Direction
    balance_ratio: Optional[float] = None
    qty: Optional[int] = None
    received_at: datetime = field(default_factory=utcnow)
    raw: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.direction.is_entry and self.balance_ratio is None and self.qty is None:
            raise ValueError(f"signal {self.id}: entry needs balance_ratio or qty")
        if self.balance_ratio is not None and not 0 < self.balance_ratio <= 1:
            raise ValueError(f"signal {self.id}: balance_ratio must be in (0, 1]")
        if self.qty is not None and self.qty <= 0:
            raise ValueError(f"signal {self.id}: qty must be positive")

    @classmethod
    def from_dict(cls, data: dict) -> "Signal":
        return cls(
            id=str(data.get("id") or data.get("signal_id") or ""),
            symbol=str(data["symbol"]).strip().upper(),
            direction=Direction.parse(data.get("direction") or data.get("side")),
            balance_ratio=(float(data["balance_ratio"])
                           if data.get("balance_ratio") is not None else None),
            qty=int(data["qty"]) if data.get("qty") is not None else None,
            raw=dict(data),
        )

    def __str__(self) -> str:
        size = f"qty={self.qty}" if self.qty else f"ratio={self.balance_ratio}"
        return f"Signal[{self.id}] {self.direction.value.upper()} {self.symbol} {size}"


@dataclass(frozen=True)
class AccountSnapshot:
    """What we could read off the account panel at a point in time."""

    balance: Optional[float]
    raw_text: str = ""
    captured_at: datetime = field(default_factory=utcnow)

    @property
    def is_valid(self) -> bool:
        return self.balance is not None and self.balance > 0


@dataclass(frozen=True)
class SessionStatus:
    logged_in: bool
    detail: str = ""
    checked_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True)
class ExecutionResult:
    signal: Signal
    status: SignalStatus
    qty: Optional[int] = None
    message: str = ""
    dry_run: bool = True
    finished_at: datetime = field(default_factory=utcnow)

    @property
    def ok(self) -> bool:
        return self.status in (SignalStatus.EXECUTED, SignalStatus.SKIPPED)


class PositionSizer:
    """Turn a balance ratio into a contract count.

    qty = floor(balance * ratio / margin_per_contract), clamped to [1, max_qty].
    Margins come from settings so they can be tuned without touching code.
    """

    def __init__(self, margins: dict, default_margin: float, max_qty: int):
        self._margins = {k.upper(): float(v) for k, v in margins.items()}
        self._default = float(default_margin)
        self._max_qty = int(max_qty)

    def margin_for(self, symbol: str) -> float:
        return self._margins.get(symbol.upper(), self._default)

    def size(self, signal: Signal, account: AccountSnapshot) -> tuple[Optional[int], str]:
        """Return (qty, explanation). qty is None when sizing is impossible."""
        if signal.qty is not None:
            return signal.qty, f"explicit qty={signal.qty} from signal"

        if not account.is_valid:
            return None, "account balance unavailable - cannot size from ratio"

        margin = self.margin_for(signal.symbol)
        notional = account.balance * signal.balance_ratio
        raw_qty = notional / margin
        qty = int(math.floor(raw_qty))

        if qty < 1:
            return None, (f"sized below one contract: balance={account.balance:,.2f} "
                          f"x ratio={signal.balance_ratio} = {notional:,.2f}, "
                          f"margin/contract={margin:,.2f} -> {raw_qty:.3f}")

        explanation = (f"balance={account.balance:,.2f} x ratio={signal.balance_ratio} "
                       f"= {notional:,.2f} / margin {margin:,.2f} = {raw_qty:.3f} "
                       f"-> floor {qty}")

        if qty > self._max_qty:
            explanation += f" -> CLAMPED to max_qty {self._max_qty}"
            qty = self._max_qty

        return qty, explanation
