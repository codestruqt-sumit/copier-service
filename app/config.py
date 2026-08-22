"""Copier configuration - every knob is env-driven so one Docker image fits every VM.

Per-VM deployment only needs SENDER_BASE_URL, COPIER_KEY and COPIER_NAME; poll cadences
come from the Sender's own trading config at registration time (env values override).
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Identity + where the signal provider lives.
    copier_name: str = "copier-1"
    sender_base_url: str = "http://localhost:8000"
    copier_key: str = ""

    # Local dashboard.
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8100

    # Durable state (SQLite). In Docker this is a mounted volume (/data).
    data_dir: str = "./data"

    # Cadence overrides - None means "use what the Sender's registration config says".
    poll_sec: float | None = None
    heartbeat_sec: float | None = None
    register_refresh_sec: float = 60.0
    # A FRESH copier (empty local DB, no cursor) starts listening from NOW and SKIPS the
    # historical backlog - those signals already happened; replaying them would fire a
    # burst of stale orders. Set true only to deliberately replay history on a new copier.
    replay_backlog_on_start: bool = False

    # --- Part 3: terminal execution -------------------------------------------
    # The executor is OFF unless explicitly armed; reception/queueing always runs.
    executor_enabled: bool = False
    # Safety: an action older than this when picked is SKIPPED, never executed -
    # a restart or long outage must not fire a burst of stale trades.
    max_action_age_sec: float = 300.0
    # Kill-switch cache: re-fetch after ttl; if the Sender is unreachable and the
    # cache is older than the stale limit, execution HOLDS (fail-closed).
    killswitch_ttl_sec: float = 5.0
    killswitch_stale_block_sec: float = 60.0
    # Terminal monitoring cadence (relaxed on purpose).
    state_poll_sec: float = 15.0
    # --- periodic terminal maintenance (all serialized executor steps: they run only
    # BETWEEN actions, never mid-order; 0 disables each) ---------------------------------
    # Reload the Tradovate tab (page-level). Relaxed to 10 min: it's now a light backstop
    # (the account-widget wedge it was added for is root-fixed) and the driver recycle is
    # the primary maintenance.
    tab_refresh_sec: float = 600.0
    # Recycle the Selenium driver: drop it and re-attach a fresh one to the SAME running
    # browser (browser + login untouched). Clears driver/CDP-side overhead. Primary
    # anti-slowdown maintenance.
    driver_recycle_sec: float = 1800.0
    # Full browser restart: close the browser process and relaunch it (persistent profile
    # keeps the login), then re-attach. Heaviest - clears browser-side buildup. WIRED but
    # OFF by default (0); enable if a driver recycle proves insufficient. Also available
    # on demand via the dashboard's "Restart browser" button.
    browser_restart_sec: float = 0.0
    # How often to read the 'Accounts' widget (all-accounts Open/Total P/L + Net Liq) and
    # report it to the Sender for the consolidated PnL table. Pure DOM read (no switching,
    # no network call), so it's cheap and safe. 0 disables it.
    accounts_pnl_sec: float = 10.0
    # Executor loop pacing (speed knobs): how long to wait after acting, and how often
    # to re-check the queue when idle. Lower = snappier pickup; higher = gentler on CPU.
    exec_acted_sec: float = 0.2
    exec_idle_sec: float = 0.25
    # Market orders: use the panel's one-click Buy Mkt / Sell Mkt button (fast) with the
    # OrderTicket as a fallback. Set false to force the (slower) ticket-only route.
    market_fast_path: bool = True
    # How long to wait for the Positions widget to reflect a market fill before giving up
    # (and reporting "submitted but not verified" / falling back). Returns the INSTANT the
    # fill registers, so a large value never slows a fast fill - it only helps a slow box.
    # A slower VM needs a bigger window: set NET_VERIFY_SEC=20 there.
    net_verify_sec: float = 12.0

    http_timeout_sec: float = 10.0
    log_level: str = "INFO"

    # --- Telegram alerts (optional, additive, non-blocking) ---------------------------
    # A supergroup with Topics enabled; each category posts to its topic thread. Empty
    # token/chat id = disabled (no-op). Delays here never affect trading (timeout-bounded,
    # fire-and-forget). See TELEGRAM_SETUP for how to get these values.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_timeout_sec: float = 5.0
    telegram_topic_copier: int | None = None    # signals this copier caught + executed
    telegram_topic_errors: int | None = None     # execution errors
    telegram_topic_warnings: int | None = None   # terminal logged out / needs login
    telegram_topic_activity: int | None = None   # lifecycle: startup/shutdown, registration,
    #                                              sender online/offline (low volume; unset =
    #                                              posts to the group's General topic)
    # Firehose: when true, EVERY activity-log event (info/warn/error) is ALSO mirrored to
    # the activity topic - the full stream, not just lifecycle. Off by default (it can be a
    # lot during active trading; per-signal + errors already have their own topics).
    telegram_activity_all: bool = False
    # How often (sec) to RE-SEND the "terminal needs login / browser down" warning while it
    # stays down, so a single dropped alert is always followed up. 0 = alert once only.
    login_alert_repeat_sec: float = 300.0

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "copier.db"


settings = Settings()
