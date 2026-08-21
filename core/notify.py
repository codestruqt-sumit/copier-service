"""Notification seam. Terminal actions hand their ActionResult to a Notifier;
what happens next (console, Telegram, dashboard webhook) is the notifier's
problem, not the action's.

Today only ConsoleNotifier is real. TelegramNotifier / DashboardNotifier are
deliberate stubs - wire them when the channels are decided, nothing else in
the codebase will need to change.
"""

from __future__ import annotations

import abc

from core import logging_setup
from core.results import ActionResult

log = logging_setup.get("notify")


class Notifier(abc.ABC):
    """Receives every ActionResult. Implementations decide what to forward."""

    @abc.abstractmethod
    def send(self, result: ActionResult) -> None: ...


class ConsoleNotifier(Notifier):
    """Logs successes at INFO, failures loudly at ERROR."""

    def send(self, result: ActionResult) -> None:
        if result.ok:
            log.info("%s", result.summary())
        else:
            log.error("%s", result.summary())
            if result.screenshot:
                log.error("  screenshot: %s", result.screenshot)


class CompositeNotifier(Notifier):
    """Fan out to several notifiers; one failing must not silence the rest."""

    def __init__(self, *notifiers: Notifier):
        self.notifiers = list(notifiers)

    def send(self, result: ActionResult) -> None:
        for notifier in self.notifiers:
            try:
                notifier.send(result)
            except Exception as exc:  # noqa: BLE001 - a notifier must never kill the bot
                log.warning("notifier %s failed: %s",
                            type(notifier).__name__, exc)


class TelegramNotifier(Notifier):
    """STUB - decide bot token + chat id, then implement send() with one
    requests.post to api.telegram.org/bot<token>/sendMessage.
    Failures only (or all results?) - decide with the dashboard design."""

    def send(self, result: ActionResult) -> None:
        log.debug("TelegramNotifier stub - dropping %s", result.action)


class DashboardNotifier(Notifier):
    """STUB - POST result.to_dict() to the ArcNine dashboard ingest endpoint
    once it exists."""

    def send(self, result: ActionResult) -> None:
        log.debug("DashboardNotifier stub - dropping %s", result.action)
