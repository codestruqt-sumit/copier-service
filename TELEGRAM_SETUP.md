# Telegram alerts — setup (Sender + Copier)

Telegram alerts are **optional, additive, and non-blocking**. Every send is fire-and-forget
on a background thread with a hard timeout, and all errors are swallowed — so a slow or
broken Telegram can **never** delay or break signal sending, reception, the action queue,
or terminal execution. Leave the token/chat-id blank and the whole feature is a silent
no-op.

Both the **Sender** (the signal portal) and every **Copier** VM post into **one shared
Telegram channel**, using **Topics** (a "forum" supergroup) so each kind of message lands
in its own topic thread.

---

## 1. One-time Telegram setup (do this once)

### a) Create the bot
1. In Telegram, message **@BotFather** → `/newbot` → follow the prompts.
2. Copy the **bot token** it gives you (looks like `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxx`).
   This is your `TELEGRAM_BOT_TOKEN`.

### b) Create the channel as a forum (Topics enabled)
1. Create a **new group** (a normal group is fine to start).
2. Open the group → **Edit** → turn ON **Topics**. This upgrades it to a forum supergroup.
3. **Add your bot to the group and make it an Admin** (admins can post to any topic).

### c) Create the topics
Create these topics (the ➕ / "Create topic" button in the forum):
- **Signals sent**
- **Signals caught**
- **Errors**
- **Login / attention**
- **Activity** (optional — copier startup / registration / sender online-offline)

(You can name them anything — what matters is their numeric ids, found below.)

### d) Find the chat id and each topic's thread id
The easiest way, using the token from step (a):

1. Post one message **in each topic** (e.g. type "test" in each), so Telegram has an update
   for each thread.
2. Open this URL in a browser (replace `<TOKEN>`):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. In the JSON you'll see, for each message:
   - `"chat":{"id":-1001234567890, ...}` → that number is your **`TELEGRAM_CHAT_ID`**
     (same for all topics — it's the channel).
   - `"message_thread_id": 42` → that is the **topic's thread id**. Match each id to the
     topic you posted in (the message text / `"forum_topic_created"` name tells you which).

> If `getUpdates` is empty: make sure you actually posted **after** adding the bot, and that
> the bot is an admin. You can also disable the bot's "Group Privacy" in BotFather
> (`/mybots` → Bot Settings → Group Privacy → Turn off) so it can read group messages.

You now have: one **bot token**, one **chat id**, and four **thread ids**.

---

## 2. Which message goes to which topic

| Message | Sent by | Category | Topic env var |
|---|---|---|---|
| A signal was sent to groups | **Sender** | `signals` | `TELEGRAM_TOPIC_SIGNALS` |
| A copier caught / received signal(s) | **Copier** | `copier` | `TELEGRAM_TOPIC_COPIER` |
| An order failed to execute | **Copier** | `errors` | `TELEGRAM_TOPIC_ERRORS` |
| A terminal is logged out / a VM needs login | **Both** | `warnings` | `TELEGRAM_TOPIC_WARNINGS` |
| Copier lifecycle: startup, registration, sender online/offline | **Copier** | `activity` | `TELEGRAM_TOPIC_ACTIVITY` |

The **bot token** and **chat id** are the **same** on both sides. The **thread ids** are
shared too — set the "Login / attention" thread id as `TELEGRAM_TOPIC_WARNINGS` on *both*
the Sender and every Copier, the "Errors" thread id as `TELEGRAM_TOPIC_ERRORS`, etc.

---

## 3. Configure the **Copier** (each VM)

Add to each VM's `.env` (next to `SENDER_BASE_URL` / `COPIER_KEY` / `COPIER_NAME`):

```dotenv
TELEGRAM_BOT_TOKEN=123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=-1001234567890
TELEGRAM_TOPIC_COPIER=<thread id of "Signals caught">
TELEGRAM_TOPIC_ERRORS=<thread id of "Errors">
TELEGRAM_TOPIC_WARNINGS=<thread id of "Login / attention">
# optional — copier lifecycle (startup/registration/sender online-offline). If unset,
# these low-volume messages go to the group's General topic instead of their own thread.
TELEGRAM_TOPIC_ACTIVITY=<thread id of "Activity">
# optional firehose — mirror EVERY activity-log line to the Activity topic (not just
# lifecycle). Off by default; can be chatty during active trading. true to enable.
TELEGRAM_ACTIVITY_ALL=false
# optional (default 5.0s) — hard cap per send; delays never affect trading
TELEGRAM_TIMEOUT_SEC=5.0
```

Restart the copier service. On startup it logs `telegram alerts enabled` when a token +
chat id are present. `COPIER_NAME` is used as the VM's label in every message, so give each
VM a distinct name.

Leave any `TELEGRAM_TOPIC_*` blank to send that category to the channel's **General** topic
instead of a specific thread. Leave the token or chat id blank to disable Telegram entirely
on that VM.

---

## 4. Configure the **Sender** (signal portal)

Set these as environment variables (Azure App Service → Configuration → Application
settings, or `.env` locally):

```dotenv
TELEGRAM_BOT_TOKEN=123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=-1001234567890
TELEGRAM_TOPIC_SIGNALS=<thread id of "Signals sent">
TELEGRAM_TOPIC_WARNINGS=<thread id of "Login / attention">
# optional
TELEGRAM_TOPIC_ERRORS=<thread id of "Errors">
TELEGRAM_TIMEOUT_SEC=5.0
```

Restart / redeploy. The Sender posts to **`signals`** whenever a signal is sent (from the
Send page, flatten-all, or the ATS API), and to **`warnings`** the moment any copier's
heartbeat flips to unhealthy (naming the VM) — this is the centralized "a VM is asking for
login" alert.

> **Duplicate login warnings?** Both the Copier (from its own terminal) and the Sender
> (from the copier's heartbeat) can post to the `warnings` topic. If you point them at the
> same channel and don't want two messages per logout, leave `TELEGRAM_TOPIC_WARNINGS`
> unset on the Copier and rely on the Sender's centralized warning (it names the VM). Both
> are safe either way — each is deduped to fire only on the *transition* into unhealthy.

---

## 5. Verify

- Send a test signal from the Sender → a message appears in **Signals sent**.
- With a copier running and armed, that signal also appears in **Signals caught**.
- Log out of Tradovate on a VM → within a heartbeat you get a **Login / attention** message
  (and the copier's own dashboard shows the red login banner; the Sender dashboard pops a
  warning naming that VM).

If nothing arrives, it's always safe — the app keeps trading normally. Re-check the token,
chat id, that the bot is an **admin** of the channel, and the thread ids.
