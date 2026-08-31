"""Execution-mode selection: the ONE place that knows both modes exist.

`build_gateway(settings)` reads COPIER_MODE and returns exactly one object implementing
the gateway seam (see base.GatewayProtocol). Nothing else in the codebase imports a mode
directly - the executor depends only on the seam, so swapping the transport swaps only
what this factory constructs.

Segregation guarantee: only the selected mode's module is imported here, and each mode's
heavy dependencies (Selenium for web, httpx/websockets for api) live inside that mode's
module. Selecting `api` therefore never launches a browser; selecting `web` never opens a
Tradovate socket.
"""

from __future__ import annotations

import logging

log = logging.getLogger("copier.gateways")

WEB = "web"
API = "api"
MODES = (WEB, API)

MODE_OVERRIDE_KEY = "copier_mode_override"   # set from the dashboard; applied at boot


def resolve_mode(settings, session_factory=None) -> str:
    """Effective mode: the dashboard-stored override (local DB) wins over the env value.
    Read once at boot - a mode change always takes effect on restart, never mid-run."""
    env_mode = (getattr(settings, "copier_mode", WEB) or WEB).strip().lower()
    if session_factory is None:
        return env_mode
    try:
        from app.models import KV
        db = session_factory()
        try:
            row = db.get(KV, MODE_OVERRIDE_KEY)
            stored = (row.value or "").strip().lower() if row else ""
        finally:
            db.close()
        if stored in MODES and stored != env_mode:
            log.info("mode override from dashboard: %s (env had %s)", stored, env_mode)
            return stored
        return stored if stored in MODES else env_mode
    except Exception:  # noqa: BLE001 - a DB hiccup must never block boot
        return env_mode


def build_gateway(settings, session_factory=None):
    """Return the gateway for settings.copier_mode. Raises ValueError on an unknown mode."""
    mode = resolve_mode(settings, session_factory)
    if mode == WEB:
        # Web mode = the shipping Selenium gateway, constructed exactly as before.
        from app.terminal import TerminalGateway
        log.info("execution mode: WEB (Selenium browser automation)")
        return TerminalGateway(
            fast_market=settings.market_fast_path,
            net_verify_sec=settings.net_verify_sec,
        )
    if mode == API:
        from app.gateways.api import TradovateGateway
        log.info("execution mode: API (Tradovate REST + WebSocket)")
        return TradovateGateway(settings, session_factory)
    raise ValueError(
        f"COPIER_MODE={mode!r} is not valid - expected one of {MODES}"
    )
