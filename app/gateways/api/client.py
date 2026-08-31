"""Authenticated Tradovate REST client - the raw calls the API gateway composes into the
order path. Deliberately thin: it does HTTP + status handling and returns parsed JSON; all
trading LOGIC (verify-before-claim, flatten math, classification) lives in gateway.py, the
same way the web mode keeps logic in terminal.py above the Selenium primitives.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger("copier.gateways.api.client")


class TradovateAPIError(RuntimeError):
    """A read/call failed at the transport or HTTP level (not an order rejection - those
    come back as a 200 with failureReason and are handled by the gateway)."""


class RestClient:
    def __init__(self, base: str, access_token: str, timeout: float = 10.0):
        self._base = base.rstrip("/")
        self._h = {"Authorization": f"Bearer {access_token}"}
        self._timeout = timeout

    # --- transport ------------------------------------------------------------------
    def _get(self, path: str):
        url = f"{self._base}{path}"
        try:
            with httpx.Client(timeout=self._timeout, headers=self._h) as c:
                r = c.get(url)
        except httpx.HTTPError as exc:
            raise TradovateAPIError(f"GET {path}: {type(exc).__name__}: {exc}") from exc
        if r.status_code == 401:
            raise TradovateAPIError("401 unauthorized (token invalid or evicted)")
        if r.status_code >= 400:
            raise TradovateAPIError(f"GET {path}: HTTP {r.status_code}")
        return r.json()

    def _post(self, path: str, body: dict):
        """POST JSON. Returns (ok_bytes_left_host, parsed_or_None). We distinguish a
        CONNECT failure (bytes never left -> safe to treat as not-sent) from a failure
        AFTER sending (unknown outcome -> the caller must NOT retry)."""
        url = f"{self._base}{path}"
        try:
            with httpx.Client(timeout=self._timeout, headers=self._h) as c:
                r = c.post(url, json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Never reached the server -> the caller may treat as not-sent.
            raise TradovateAPIError(f"POST {path}: not sent ({type(exc).__name__})") from exc
        except httpx.HTTPError as exc:
            # Sent but the response was lost -> AMBIGUOUS. Signalled distinctly.
            raise AmbiguousSend(f"POST {path}: sent but no response ({type(exc).__name__})") from exc
        if r.status_code >= 500:
            raise AmbiguousSend(f"POST {path}: HTTP {r.status_code} after send")
        try:
            return r.json()
        except Exception:  # noqa: BLE001
            return {}

    # --- reads ----------------------------------------------------------------------
    def account_list(self) -> list[dict]:
        return self._get("/account/list") or []

    def contract_find(self, symbol: str) -> dict | None:
        return self._get(f"/contract/find?name={symbol}")

    def order_item(self, order_id) -> dict | None:
        return self._get(f"/order/item?id={order_id}")

    def order_list(self) -> list[dict]:
        return self._get("/order/list") or []

    def position_list(self) -> list[dict]:
        return self._get("/position/list") or []

    # --- writes ---------------------------------------------------------------------
    def place_order(self, payload: dict) -> dict:
        """POST /order/placeorder. Returns {orderId?, failureReason?, failureText?}."""
        return self._post("/order/placeorder", payload) or {}

    def cancel_order(self, order_id) -> dict:
        # isAutomated must be set on cancel too (docs: accepted; defaults false).
        return self._post("/order/cancelorder",
                          {"orderId": order_id, "isAutomated": True}) or {}


class AmbiguousSend(TradovateAPIError):
    """The order POST left the host but no clean response came back. Outcome is UNKNOWN -
    the gateway must NEVER retry it (that could double the order); it reports failed and
    tells the operator to VERIFY on the terminal."""
