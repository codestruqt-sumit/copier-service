"""Tradovate OAuth authorization-code flow (per-VM, local).

Pure flow helpers - no storage, no framework. The copier hosts its own callback on its
local dashboard, so `redirect_uri` points back to THIS VM (settings.oauth_redirect_uri).

Verified shape (tradovate/example-api-oauth + partner.tradovate.com):
  authorize -> GET  {authorize_host}?response_type=code&client_id&redirect_uri&state
  exchange  -> POST {base}/auth/oauthtoken  grant_type=authorization_code + code + creds
  refresh   -> POST {base}/auth/oauthtoken  grant_type=refresh_token + refresh_token + creds

The client_secret is only ever read via SecretStr.get_secret_value() at the moment of the
POST and is NEVER logged.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

import httpx

log = logging.getLogger("copier.gateways.api.oauth")


class OAuthError(RuntimeError):
    """OAuth exchange/refresh failed (bad code, mismatched redirect, expired refresh...)."""


def authorize_url(settings, state: str) -> str:
    """The URL to send the user's browser to. They log in on Tradovate's own page."""
    params = {
        "response_type": "code",
        "client_id": settings.tradovate_client_id,
        "redirect_uri": settings.oauth_redirect_uri,
        "state": state,
    }
    return f"{settings.tradovate_oauth_authorize}?{urlencode(params)}"


def _secret(settings) -> str:
    sec = settings.tradovate_client_secret
    return sec.get_secret_value() if hasattr(sec, "get_secret_value") else str(sec or "")


def _post_token(settings, form: dict) -> dict:
    """POST the token endpoint and return the parsed token dict, raising OAuthError on any
    failure. The example uses form-encoded; we send form data."""
    url = f"{settings.tradovate_base}/auth/oauthtoken"
    try:
        with httpx.Client(timeout=settings.http_timeout_sec) as c:
            r = c.post(url, data=form)
    except httpx.HTTPError as exc:
        raise OAuthError(f"token endpoint unreachable: {type(exc).__name__}: {exc}") from exc
    try:
        body = r.json()
    except Exception as exc:  # noqa: BLE001
        raise OAuthError(f"token endpoint returned non-JSON (HTTP {r.status_code})") from exc
    # OAuth errors come back as {error, error_description} (often still HTTP 200-ish).
    if body.get("error") or not body.get("access_token"):
        # Do NOT include the request form (it holds the secret) in the message.
        raise OAuthError(
            f"token exchange failed: {body.get('error', 'no access_token')} "
            f"- {body.get('error_description', '')}".strip(" -"))
    return {
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token"),
        "token_type": body.get("token_type", "Bearer"),
        "expires_in": body.get("expires_in"),
        "refresh_token_expires_in": body.get("refresh_token_expires_in"),
    }


def exchange_code(settings, code: str) -> dict:
    """Exchange the callback `code` for tokens (server-to-server; secret stays here)."""
    log.info("OAuth: exchanging authorization code (redirect_uri=%s)",
             settings.oauth_redirect_uri)
    return _post_token(settings, {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": settings.tradovate_client_id,
        "client_secret": _secret(settings),
        "redirect_uri": settings.oauth_redirect_uri,
    })


def refresh_token(settings, refresh_tok: str) -> dict:
    """Get a fresh access token WITHOUT re-prompting the user."""
    log.info("OAuth: refreshing access token")
    return _post_token(settings, {
        "grant_type": "refresh_token",
        "refresh_token": refresh_tok,
        "client_id": settings.tradovate_client_id,
        "client_secret": _secret(settings),
    })
