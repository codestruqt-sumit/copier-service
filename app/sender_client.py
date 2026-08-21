"""HTTP client for the Sender's Copier API (/api/copier/*).

Thin and synchronous on purpose: the poller runs in its own thread and every call is
short. The `http` argument accepts any httpx.Client-compatible object, so tests inject
a starlette TestClient wrapping a fake Sender app - no network, same code path.

The `since` cursor is OPAQUE: the copier stores whatever the Sender returned and echoes
it back verbatim. Never parse it - its format belongs to the Sender.
"""

from __future__ import annotations

from typing import Any

import httpx


class SenderError(Exception):
    """Any non-200 from the Sender, with enough context to log."""


class SenderClient:
    def __init__(
        self,
        base_url: str,
        copier_key: str,
        copier_name: str,
        *,
        http: Any | None = None,
        timeout: float = 10.0,
    ):
        self.copier_name = copier_name
        self._headers = {"X-Copier-Key": copier_key}
        self._own_client = http is None
        self.http = http or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    # --- endpoints ------------------------------------------------------------

    def register(self) -> dict:
        """Authenticate and fetch assigned accounts (+ their groups) and cadence config."""
        return self._post("/api/copier/register", {"name": self.copier_name})

    def heartbeat(self, status: str = "online", *, healthy: bool | None = None,
                  detail: str | None = None, terminal: dict | None = None) -> dict:
        body: dict[str, Any] = {"name": self.copier_name, "status": status}
        if healthy is not None:
            body["healthy"] = healthy
        if detail is not None:
            body["detail"] = detail
        if terminal is not None:
            body["terminal"] = terminal
        return self._post("/api/copier/heartbeat", body)

    def commands(self, since: str | None) -> dict:
        params = {"since": since} if since else {}
        response = self.http.get("/api/copier/commands", params=params, headers=self._headers)
        return self._check(response)

    def killswitch(self) -> dict:
        """The safety barrier: {account_ref: trading_allowed} for this copier."""
        response = self.http.get("/api/copier/killswitch", headers=self._headers)
        return self._check(response)

    def post_reports(self, reports: list[dict]) -> dict:
        """Part 3 uses this after real executions; wired now so the contract is complete."""
        return self._post("/api/copier/reports", {"reports": reports})

    def post_state(self, states: list[dict]) -> dict:
        """Part 3: periodic account snapshots from the terminal."""
        return self._post("/api/copier/state", {"states": states})

    def post_accounts(self, accounts: list[dict]) -> dict:
        """All-accounts PnL read from the terminal's 'Accounts' widget (Open/Total P/L,
        Net Liq, ...). The Sender consolidates these across copiers into one table."""
        return self._post("/api/copier/accounts", {"accounts": accounts})

    # --- plumbing ---------------------------------------------------------------

    def _post(self, path: str, body: dict) -> dict:
        response = self.http.post(path, json=body, headers=self._headers)
        return self._check(response)

    @staticmethod
    def _check(response) -> dict:
        if response.status_code != 200:
            raise SenderError(f"{response.request.method} {response.url} -> "
                              f"{response.status_code}: {response.text[:200]}")
        return response.json()

    def close(self) -> None:
        if self._own_client:
            self.http.close()
