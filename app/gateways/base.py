"""The gateway seam as a typed contract.

Both execution modes implement this exact surface; the executor depends only on it. A mode
is a class with these methods - nothing more leaks across the boundary. Kept as a Protocol
(structural typing) so the existing TerminalGateway satisfies it without inheriting, and so
tests' FakeGateway keeps working unchanged.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Protocol, runtime_checkable


@runtime_checkable
class GatewayProtocol(Protocol):
    # Set per-action by the executor; polled by the gateway at its checkpoints only.
    abort_check: Optional[Callable[[], Optional[str]]]

    def ensure_connected(self) -> tuple[bool, str]: ...
    def login_check(self) -> tuple[bool, str]: ...
    def active_account(self) -> Optional[str]: ...
    def ensure_account(self, account_ref: str) -> tuple[bool, str]: ...
    def execute(self, action: dict) -> dict: ...          # {outcome, order_ref, detail}
    def read_state(self) -> dict: ...                      # {account, positions, working_orders}
    def read_accounts_summary(self) -> list[dict]: ...
    def keepalive(self) -> bool: ...
    def refresh_tab(self, settle_timeout: float = 25.0) -> tuple[bool, str]: ...
    def recycle_driver(self) -> tuple[bool, str]: ...
    def restart_browser(self, settle_timeout: float = 30.0) -> tuple[bool, str]: ...


# The eleven methods the executor calls, for tests/introspection.
SEAM_METHODS: tuple[str, ...] = (
    "ensure_connected", "login_check", "active_account", "ensure_account", "execute",
    "read_state", "read_accounts_summary", "keepalive", "refresh_tab", "recycle_driver",
    "restart_browser",
)
