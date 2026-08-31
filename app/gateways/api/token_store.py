"""Local, per-VM token store for API mode.

Persists the OAuth token in the copier's own SQLite (the KV table) - no central store, no
new dependency. This is consistent with the existing trust model: the same VM already
holds an unencrypted persistent Tradovate browser login (browser-profile/) for web mode,
and the VM itself is the trust boundary (RDP-locked, NSG-restricted, single tenant). The
token is never logged and never leaves the VM except to Tradovate.

Future hardening (if desired): wrap value in Fernet keyed by an env secret before storing -
add `cryptography` and swap `_dumps`/`_loads`. The call sites here would not change.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import KV

TOKEN_KEY = "tradovate_oauth_token"
STATE_KEY = "tradovate_oauth_state"      # transient CSRF state between start and callback
_EXPIRY_SKEW_SEC = 60                     # treat a token as expired this long before it is


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _kv_set(db: Session, key: str, value: str) -> None:
    row = db.get(KV, key)
    if row is None:
        db.add(KV(key=key, value=value))
    else:
        row.value = value


def _kv_get(db: Session, key: str) -> str | None:
    row = db.get(KV, key)
    return row.value if row is not None else None


# --- OAuth state (CSRF) ---------------------------------------------------------------

def save_state(db: Session, state: str) -> None:
    _kv_set(db, STATE_KEY, state)
    db.commit()


def pop_state(db: Session) -> str | None:
    """Read-and-clear the stored state so a code can only be redeemed once per start."""
    val = _kv_get(db, STATE_KEY)
    if val is not None:
        row = db.get(KV, STATE_KEY)
        if row is not None:
            db.delete(row)
            db.commit()
    return val


# --- token ----------------------------------------------------------------------------

def save_token(db: Session, token: dict) -> dict:
    """Persist a token dict from oauth.exchange_code / refresh_token, stamping absolute
    expiry times from the relative expires_in fields. Returns the stored record."""
    now = _now()
    rec = {
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token"),
        "token_type": token.get("token_type", "Bearer"),
        "obtained_at": now.isoformat(),
        "expires_at": (
            (now + timedelta(seconds=int(token["expires_in"]))).isoformat()
            if token.get("expires_in") is not None else None),
        "refresh_expires_at": (
            (now + timedelta(seconds=int(token["refresh_token_expires_in"]))).isoformat()
            if token.get("refresh_token_expires_in") is not None else None),
    }
    _kv_set(db, TOKEN_KEY, json.dumps(rec))
    db.commit()
    return rec


def load_token(db: Session) -> dict | None:
    raw = _kv_get(db, TOKEN_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def clear_token(db: Session) -> None:
    row = db.get(KV, TOKEN_KEY)
    if row is not None:
        db.delete(row)
        db.commit()


def _parse(dt_iso: str | None) -> datetime | None:
    if not dt_iso:
        return None
    try:
        d = datetime.fromisoformat(dt_iso)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def access_expired(rec: dict, skew_sec: float = _EXPIRY_SKEW_SEC) -> bool:
    """True if the access token is at/near expiry (or has no known expiry -> treat as
    expired so we refresh, rather than trusting it forever)."""
    exp = _parse(rec.get("expires_at"))
    if exp is None:
        return True
    return _now() >= (exp - timedelta(seconds=skew_sec))


def refresh_expired(rec: dict) -> bool:
    """True if the refresh token itself has lapsed -> the user must re-OAuth. Unknown
    refresh expiry is treated as NOT expired (many providers issue long-lived refresh)."""
    exp = _parse(rec.get("refresh_expires_at"))
    return exp is not None and _now() >= exp
