"""API execution mode: the Tradovate REST + WebSocket transport.

Public surface: `TradovateGateway` (the seam implementation). Everything Tradovate-specific
lives in this package; nothing here imports the web (Selenium) side, and nothing above the
gateway seam imports this package except the factory (app/gateways/__init__.py) and the
OAuth routes (app/oauth_routes.py).
"""

from __future__ import annotations

from app.gateways.api.gateway import TradovateGateway

__all__ = ["TradovateGateway"]
