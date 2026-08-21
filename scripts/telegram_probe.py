"""Discover your Telegram chat_id + each topic's message_thread_id, using the BOT.

READ-ONLY: it calls getUpdates and prints what the bot has seen. It never posts or
changes anything.

How to use
----------
1. Create the bot (@BotFather) and the forum group with your 4 topics, and add the bot
   to the group (admin is easiest, but not required for the step below).
2. In EACH topic, send a message that ADDRESSES the bot by a slash command, e.g.:

       /id@YourBotUsername signals      (in the "Signals sent" topic)
       /id@YourBotUsername caught       (in "Signals caught")
       /id@YourBotUsername errors       (in "Errors")
       /id@YourBotUsername login        (in "Login / attention")

   A /command that mentions the bot is delivered to it EVEN WITH Group Privacy ON, so
   you don't have to toggle privacy or re-add the bot. (Replace YourBotUsername with your
   bot's actual @username from BotFather.)
3. Run this (reads TELEGRAM_BOT_TOKEN from .env / env):

       python scripts/telegram_probe.py
   or: python scripts/telegram_probe.py --token 123456789:AAE...

It prints the chat_id and a thread_id per topic (labelled by the word you typed), plus
ready-to-paste env lines.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

# so `from app.config import settings` works when run as `python scripts/telegram_probe.py`
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def _get_token(cli_token: str | None) -> str:
    if cli_token:
        return cli_token.strip()
    try:
        from app.config import settings  # reads .env + real env via pydantic-settings
        if settings.telegram_bot_token:
            return settings.telegram_bot_token.strip()
    except Exception:  # noqa: BLE001 - app import optional; fall back to the env var
        pass
    return (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()


def _updates(token: str) -> list[dict]:
    url = f"https://api.telegram.org/bot{token}/getUpdates?timeout=0"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"HTTP {exc.code} from Telegram: {body}\n"
                         "  401 = bad/rotated token · 409 = a webhook is set "
                         "(call deleteWebhook first).")
    if not payload.get("ok"):
        raise SystemExit(f"Telegram error: {payload.get('description')}")
    return payload.get("result", [])


def _msg(update: dict) -> dict | None:
    for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
        if key in update:
            return update[key]
    return None


def _topic_name(m: dict) -> str | None:
    for src in (m, m.get("reply_to_message", {}) or {}):
        name = (src.get("forum_topic_created") or {}).get("name")
        if name:
            return name
    return None


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Discover Telegram chat_id + topic thread ids via the bot (read-only).")
    ap.add_argument("--token", help="bot token (else TELEGRAM_BOT_TOKEN from .env / env)")
    args = ap.parse_args()

    token = _get_token(args.token)
    if not token:
        raise SystemExit("No bot token. Put TELEGRAM_BOT_TOKEN in .env, or pass --token.")

    updates = _updates(token)
    if not updates:
        print("getUpdates returned NOTHING yet.\n"
              "In EACH topic send:  /id@YourBotUsername <label>   (e.g. /id@MyBot signals)\n"
              "A /command addressed to the bot is delivered even with Group Privacy ON.\n"
              "Then re-run this. (If you set a webhook, getUpdates stays empty until "
              "deleteWebhook.)")
        return

    chats: dict = {}                       # chat_id -> title
    threads: dict[tuple, str] = {}         # (chat_id, thread_id) -> label
    for u in updates:
        m = _msg(u)
        if not m:
            continue
        chat = m.get("chat", {})
        cid = chat.get("id")
        if cid is None:
            continue
        chats[cid] = chat.get("title") or chat.get("type") or ""
        tid = m.get("message_thread_id")
        if tid is not None:
            label = _topic_name(m) or (m.get("text") or "").strip() or "?"
            threads[(cid, tid)] = label

    for cid, title in chats.items():
        print(f"\nchat_id = {cid}    (group: {title!r})")
        rows = sorted(t for (c, t) in threads if c == cid)
        if not rows:
            print("  (no topic messages seen — post /id@YourBot <label> INSIDE each topic)")
        for tid in rows:
            print(f"  thread_id = {str(tid):<6} <- {threads[(cid, tid)]}")

    if chats:
        cid = next(iter(chats))
        print("\n--- paste into the COPIER .env (map each thread_id to the right topic) ---")
        print(f"TELEGRAM_CHAT_ID={cid}")
        print("TELEGRAM_TOPIC_COPIER=<thread id you labelled 'caught'>")
        print("TELEGRAM_TOPIC_ERRORS=<thread id you labelled 'errors'>")
        print("TELEGRAM_TOPIC_WARNINGS=<thread id you labelled 'login'>")
        print("# Sender uses the SAME chat id; its topics are SIGNALS (labelled 'signals') "
              "+ WARNINGS + ERRORS")


if __name__ == "__main__":
    main()
