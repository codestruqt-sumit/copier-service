"""Direct-credential auth (TRADOVATE_AUTH=credentials) - the per-VM, HEADLESS path.

The user's OWN Tradovate name/password + API key (cid/sec) live in this VM's .env; the
copier mints an access token via /auth/accesstokenrequest and RENEWS it before expiry via
/auth/renewaccesstoken. It NEVER re-calls accesstokenrequest on a schedule - that endpoint
carries the p-captcha (~1h) lockout and the 2-concurrent-session eviction risk. Re-mint
only as a fallback when a renew fails.

Payload fields mirror the tested tradovate_place_order.sh exactly:
  {name, password, appId, appVersion, cid, sec} -> {accessToken, expirationTime, ...}
Secrets (password, sec) are read via SecretStr.get_secret_value() only at POST time and
never logged.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

log = logging.getLogger("copier.gateways.api.credentials")


class CredError(RuntimeError):
    """accesstokenrequest / renew failed (bad creds, penalty, transport)."""


def _secret(v) -> str:
    return v.get_secret_value() if hasattr(v, "get_secret_value") else str(v or "")


def _normalise(body: dict) -> dict:
    """accesstokenrequest & renew both return accessToken + expirationTime (absolute ISO).
    Convert to the token_store shape ({access_token, expires_in, refresh_token=None})."""
    tok = body.get("accessToken")
    if not tok:
        raise CredError(body.get("errorText") or "no accessToken returned")
    expires_in = None
    exp = body.get("expirationTime")
    if exp:
        try:
            d = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
            expires_in = max(0, int((d - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError):
            expires_in = None
    return {"access_token": tok, "expires_in": expires_in, "refresh_token": None}


def _penalty_guard(body: dict) -> None:
    if body.get("p-captcha"):
        raise CredError("auth penalty (p-captcha) - locked ~1h; NOT retried automatically")
    if body.get("p-ticket"):
        raise CredError(f"auth time penalty: retry in {body.get('p-time')}s (p-ticket) - NOT retried")


def access_token_request(settings) -> dict:
    """Mint a token from the user's credentials. Headless - no browser, no redirect."""
    url = f"{settings.tradovate_base}/auth/accesstokenrequest"
    payload = {
        "name": settings.tradovate_name,
        "password": _secret(settings.tradovate_password),
        "appId": settings.tradovate_app_id,
        "appVersion": settings.tradovate_app_version,
        "cid": settings.tradovate_cid,
        "sec": _secret(settings.tradovate_sec),
    }
    try:
        with httpx.Client(timeout=settings.http_timeout_sec) as c:
            body = c.post(url, json=payload).json()
    except Exception as exc:  # noqa: BLE001
        raise CredError(f"accesstokenrequest failed: {type(exc).__name__}: {exc}") from exc
    _penalty_guard(body)
    rec = _normalise(body)          # raises CredError on errorText / missing token
    log.info("credentials: minted access token via accesstokenrequest")
    return rec


def renew_access_token(settings, access_token: str) -> dict:
    """Extend the current session (the correct way to keep a token alive)."""
    url = f"{settings.tradovate_base}/auth/renewaccesstoken"
    try:
        with httpx.Client(timeout=settings.http_timeout_sec,
                          headers={"Authorization": f"Bearer {access_token}"}) as c:
            body = c.get(url).json()
    except Exception as exc:  # noqa: BLE001
        raise CredError(f"renewaccesstoken failed: {type(exc).__name__}: {exc}") from exc
    return _normalise(body)
