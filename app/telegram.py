"""Best-effort Telegram alerts - STRICTLY ADDITIVE and NON-BLOCKING.

Every send runs on a short-lived daemon thread with a hard HTTP timeout and swallows all
errors. A slow or broken Telegram can therefore NEVER delay or break reception, the
queue, or terminal execution. If it isn't configured (no token/chat id) it's a no-op.

The channel is a Telegram supergroup with Topics (forum) enabled; each alert category
maps to a topic's message_thread_id so signals / copier activity / errors / login-warnings
land in separate topics.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.parse
import urllib.request

from app.config import settings

log = logging.getLogger("copier.telegram")


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, topics: dict[str, int | None],
                 timeout: float = 5.0):
        self.token = (token or "").strip()
        self.chat_id = (chat_id or "").strip()
        self.topics = {k: v for k, v in (topics or {}).items() if v}
        self.timeout = max(1.0, float(timeout or 5.0))
        self.enabled = bool(self.token and self.chat_id)

    @classmethod
    def from_settings(cls, s) -> "TelegramNotifier":
        return cls(
            token=getattr(s, "telegram_bot_token", "") or "",
            chat_id=getattr(s, "telegram_chat_id", "") or "",
            topics={
                "copier": getattr(s, "telegram_topic_copier", None),
                "errors": getattr(s, "telegram_topic_errors", None),
                "warnings": getattr(s, "telegram_topic_warnings", None),
                "activity": getattr(s, "telegram_topic_activity", None),
            },
            timeout=getattr(s, "telegram_timeout_sec", 5.0),
        )

    def send(self, category: str, text: str) -> None:
        """Fire-and-forget. Returns immediately; the HTTP call happens on a daemon
        thread and can never block or raise into the caller."""
        if not self.enabled or not text:
            return
        threading.Thread(target=self._send, args=(category, text),
                         name="telegram-send", daemon=True).start()

    def _send(self, category: str, text: str) -> None:
        try:
            data = {"chat_id": self.chat_id, "text": text[:4000],
                    "disable_web_page_preview": "true"}
            thread_id = self.topics.get(category)
            if thread_id:
                data["message_thread_id"] = int(thread_id)
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data=urllib.parse.urlencode(data).encode("utf-8"),
                method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
        except Exception as exc:  # noqa: BLE001 - alerts must never break anything
            log.debug("telegram send failed (%s): %s", category, exc)


# Module-level singleton built from env once. Import via `from app import telegram as _tg`
# and use `_tg.notifier.send(...)` so tests can monkeypatch `app.telegram.notifier`.
notifier = TelegramNotifier.from_settings(settings)
