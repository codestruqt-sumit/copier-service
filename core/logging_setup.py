"""Console + rotating file logging with a consistent, scannable format."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONSOLE_FMT = "%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s"
_FILE_FMT = "%(asctime)s | %(levelname)-7s | %(name)-24s | %(funcName)-22s | %(message)s"
_DATE_FMT = "%H:%M:%S"

_CONFIGURED = False


def setup(log_dir: Path, level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(logging.Formatter(_CONSOLE_FMT, datefmt=_DATE_FMT))
    root.addHandler(console)

    # everything, including DEBUG, goes to disk for post-mortem
    file_handler = RotatingFileHandler(
        log_dir / "bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_FILE_FMT))
    root.addHandler(file_handler)

    # a dedicated, never-rotated-away audit trail of order decisions
    audit = logging.getLogger("audit")
    audit_handler = RotatingFileHandler(
        log_dir / "orders.log", maxBytes=5_000_000, backupCount=20, encoding="utf-8"
    )
    audit_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    audit.addHandler(audit_handler)
    audit.propagate = True

    # selenium is extremely chatty at DEBUG
    for noisy in ("selenium", "urllib3", "websocket"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)


def audit(message: str) -> None:
    """Write to the order audit trail. Use for every trade decision."""
    logging.getLogger("audit").info(message)
