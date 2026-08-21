"""HTTP clients the ATS uses.

Two, and only two, kinds of access:
  * SenderAts     - the Signal Provider machine API (/api/ats/*): GENERATE signals.
  * CopierObserver - the copier's READ-ONLY machine endpoints (/api/activity, /api/overview).

There is intentionally no path here to instruct the copier/terminal to execute anything.
"""

from __future__ import annotations

import time

import httpx


class SenderAts:
    """Signal Provider API (bearer key). Generates signals + reads delivery status."""

    def __init__(self, base_url: str, key: str, timeout: float = 20.0):
        self.base = base_url.rstrip("/")
        self.http = httpx.Client(base_url=self.base, timeout=timeout,
                                 headers={"X-ATS-Key": key})

    def config(self) -> dict:
        return self._json(self.http.get("/api/ats/config"))

    def send_signal(self, **body) -> dict:
        return self._json(self._post_retry("/api/ats/signals", json=body))

    def cancel_signal(self, cmd_id: int) -> dict:
        return self._json(self._post_retry(f"/api/ats/signals/{cmd_id}/cancel"))

    def _post_retry(self, path: str, *, json: dict | None = None,
                    attempts: int = 4, backoff: float = 1.5) -> httpx.Response:
        """POST, retrying ONLY on connection-level failures (DNS/connect) - those mean the
        request never reached the server, so a retry can't double-send. Timeouts and HTTP
        error statuses are NOT retried (the request may have landed). Keeps a client-side
        network blip from orphaning a position when the EXIT send fails."""
        last: Exception | None = None
        for i in range(attempts):
            try:
                return self.http.post(path, json=json)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last = exc
                if i < attempts - 1:
                    time.sleep(backoff * (i + 1))
        raise last  # exhausted retries - surfaced to the caller as before

    def status(self) -> dict:
        return self._json(self.http.get("/api/ats/status"))

    @staticmethod
    def _json(resp: httpx.Response) -> dict:
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self.http.close()


class CopierObserver:
    """Read-only observation of ONE copier. Tracks an event cursor so it accumulates the
    full event audit forward from ATS start; actions/signals it returns are always the
    copier's current rows (correlated to signals by signal_id)."""

    def __init__(self, base_url: str, timeout: float = 20.0):
        self.base = base_url.rstrip("/")
        self.http = httpx.Client(base_url=self.base, timeout=timeout)
        self.cursor = 0
        self.events: list[dict] = []   # accumulated audit for the report

    def _activity(self, since: int) -> dict:
        r = self.http.get("/api/activity", params={"since": since})
        r.raise_for_status()
        return r.json()

    def init_cursor(self) -> None:
        """Start the event audit from 'now' (the current max event id)."""
        self.cursor = int(self._activity(2_000_000_000_000).get("latest", 0) or 0)

    def drain(self) -> tuple[list[dict], list[dict]]:
        """Accumulate all NEW events (advancing the cursor) and return the copier's
        current actions + signals for correlation."""
        actions: list[dict] = []
        signals: list[dict] = []
        for _ in range(50):   # bounded drain
            data = self._activity(self.cursor)
            actions = data.get("actions", actions)
            signals = data.get("signals", signals)
            evs = data.get("events", [])
            if evs:
                self.events.extend(evs)
                self.cursor = int(data.get("cursor", self.cursor) or self.cursor)
            if len(evs) < 300:
                break
        return actions, signals

    def overview(self) -> dict:
        r = self.http.get("/api/overview")
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self.http.close()
