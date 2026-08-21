"""SQLite storage - one small durable file per copier instance.

WAL mode lets the dashboard read while the poller writes. The database is the
audit trail AND the action queue, so it lives on a mounted volume in Docker and
survives restarts (the poll cursor too - no re-delivery storms after a bounce).
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker


def make_engine(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db(engine) -> None:
    from sqlalchemy import text

    from app.models import Base

    Base.metadata.create_all(engine)

    # Tiny in-place migration: create_all never ALTERs existing tables, and live
    # copiers already carry a populated actions table from Parts 1+2.
    with engine.begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(actions)"))}
        if "order_ref" not in columns:
            conn.execute(text("ALTER TABLE actions ADD COLUMN order_ref VARCHAR(64)"))
