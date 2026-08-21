"""Configuration. Everything tunable lives here or in settings.local.json."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
STATE_DIR = PROJECT_ROOT / "state"
LOCAL_OVERRIDES = PROJECT_ROOT / "settings.local.json"

TRADOVATE_URL = "https://trader.tradovate.com/"


@dataclass
class BrowserSettings:
    browser: str = "edge"                  # "edge" or "chrome"
    host: str = "127.0.0.1"
    # deliberately not 9222 - that is the common default, and attaching to some
    # other debug browser by accident would drive the wrong window
    port: int = 9250
    profile_dir: str = str(PROJECT_ROOT / "browser-profile")
    page_load_timeout: int = 45
    default_wait: int = 15                 # seconds for element waits
    keep_open: bool = True                 # never close the trading window on exit


@dataclass
class RiskSettings:
    """Hard limits. These are the last line of defence before a bad fill."""

    max_qty_per_order: int = 5
    max_orders_per_hour: int = 20
    default_margin_per_contract: float = 1500.0
    margin_per_contract: dict = field(default_factory=lambda: {
        # day-trade margin per contract; tune to your broker's actual values
        "ES": 1500.0, "MES": 150.0,
        "NQ": 2000.0, "MNQ": 200.0,
        "CL": 2500.0, "MCL": 250.0,
        "GC": 2000.0, "MGC": 200.0,
    })
    # refuse to trade if balance falls below this
    min_account_balance: float = 500.0


@dataclass
class SessionSettings:
    """Keep-alive and login monitoring."""

    keepalive_interval_sec: int = 240      # nudge the UI every 4 min
    login_check_interval_sec: int = 60
    notify_on_logout: bool = True
    # consecutive failed login checks before we declare the session dead
    logout_confirm_checks: int = 2


@dataclass
class SignalSettings:
    source: str = "file"                   # "file" | "firebase"
    poll_interval_sec: int = 5
    file_path: str = str(STATE_DIR / "signals.json")
    # firebase placeholders - wired up later
    firebase_credentials_path: str = ""
    firebase_database_url: str = ""
    firebase_collection: str = "signals"
    # signals older than this are ignored on startup so a stale queue
    # cannot fire a burst of trades when the bot restarts
    max_signal_age_sec: int = 300


@dataclass
class Settings:
    browser: BrowserSettings = field(default_factory=BrowserSettings)
    risk: RiskSettings = field(default_factory=RiskSettings)
    session: SessionSettings = field(default_factory=SessionSettings)
    signals: SignalSettings = field(default_factory=SignalSettings)

    url: str = TRADOVATE_URL
    dry_run: bool = True                   # MUST be flipped explicitly with --arm
    kill_switch_file: str = str(STATE_DIR / "STOP")
    log_level: str = "INFO"

    @classmethod
    def load(cls, overrides_path: Path | None = None) -> "Settings":
        """Build settings, then apply settings.local.json on top if present."""
        settings = cls()
        path = overrides_path or LOCAL_OVERRIDES
        if path and Path(path).is_file():
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            settings.apply(data)
        settings.apply_env()
        return settings

    def apply(self, data: dict[str, Any]) -> None:
        """Shallow-merge a nested dict onto the dataclass tree."""
        for key, value in data.items():
            if not hasattr(self, key):
                continue
            current = getattr(self, key)
            if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    if hasattr(current, sub_key):
                        setattr(current, sub_key, sub_value)
            else:
                setattr(self, key, value)

    def apply_env(self) -> None:
        """Env vars win over the file. TVBOT_BROWSER, TVBOT_PORT, TVBOT_LOG_LEVEL."""
        if os.getenv("TVBOT_BROWSER"):
            self.browser.browser = os.environ["TVBOT_BROWSER"]
        if os.getenv("TVBOT_PORT"):
            self.browser.port = int(os.environ["TVBOT_PORT"])
        if os.getenv("TVBOT_LOG_LEVEL"):
            self.log_level = os.environ["TVBOT_LOG_LEVEL"]

    def as_dict(self) -> dict:
        return asdict(self)

    def ensure_dirs(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        Path(self.browser.profile_dir).mkdir(parents=True, exist_ok=True)
