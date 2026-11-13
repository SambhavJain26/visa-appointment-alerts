"""Configuration loaded from environment / .env. No secrets are hardcoded here."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(v) if v and v.strip() else default


def _float(name: str, default: float) -> float:
    v = os.getenv(name)
    return float(v) if v and v.strip() else default


def _list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass(frozen=True)
class Recipient:
    phone: str
    apikey: str
    name: str = ""

    @property
    def masked(self) -> str:
        label = self.name or "recipient"
        return f"{label}(…{self.phone[-4:]})"


@dataclass(frozen=True)
class BotChat:
    chat_id: str
    name: str = ""

    @property
    def masked(self) -> str:
        return f"{self.name or 'chat'}(…{self.chat_id[-3:]})"


def _parse_recipients(raw: str) -> list[Recipient]:
    """Format: name|+919999999999|apikey, name2|+1555...|apikey2 (name optional).
    Incomplete entries (missing phone digits or key) are ignored."""
    out: list[Recipient] = []
    for item in raw.split(","):
        parts = [p.strip() for p in item.strip().split("|")]
        if len(parts) == 2:
            name, phone, key = "", parts[0], parts[1]
        elif len(parts) == 3:
            name, phone, key = parts
        else:
            continue
        if len(re.sub(r"\D", "", phone)) >= 8 and key:
            out.append(Recipient(name=name, phone=phone, apikey=key))
    return out


def _parse_bot_chats(raw: str) -> list[BotChat]:
    """Format: me|123456789, brother|987654321 (name optional)."""
    out: list[BotChat] = []
    for item in raw.split(","):
        parts = [p.strip() for p in item.strip().split("|")]
        name, cid = (parts[0], parts[1]) if len(parts) == 2 else ("", parts[0])
        if re.fullmatch(r"-?\d{3,}", cid or ""):
            out.append(BotChat(chat_id=cid, name=name))
    return out


@dataclass(frozen=True)
class Config:
    data_dir: Path
    dry_run: bool
    log_level: str

    # Telegram
    tg_api_id: int | None
    tg_api_hash: str | None
    tg_phone: str | None
    tg_session: Path
    tg_groups: list[str]
    tg_poll_seconds: float
    tg_enabled: bool

    # OpenAI
    openai_api_key: str | None
    openai_model: str
    openai_fallback_model: str
    ai_timeout: float
    ai_image_timeout: float
    max_image_bytes: int

    # Alerts: Telegram bot (main) + optional WhatsApp / CallMeBot
    tg_bot_token: str | None
    bot_chats: list[BotChat]
    feed_bot_token: str | None  # optional second bot: copy of every message read + decision
    feed_chats: list[BotChat]
    recipients: list[Recipient]
    callmebot_min_gap: float

    # CheckVisaSlots
    cvs_enabled: bool
    cvs_url: str
    cvs_poll_seconds: float      # minimum gap between two checks (seconds)
    cvs_refresh_seconds: float   # how often the site rebuilds its table; checks are timed to it
    cvs_browser_headed: bool
    cvs_feed_check_summaries: bool
    cvs_sync_checks: bool
    cvs_max_alert_age_minutes: float
    cvs_network_controls_enabled: bool

    # Housekeeping
    timezones: list[str]
    heartbeat_time: str
    source_fail_minutes: int
    dup_summary_window: int
    max_alert_age_minutes: int
    same_sender_window: int

    extra: dict = field(default_factory=dict)


DEFAULT_GROUPS = (
    "h1bh4_dropbox_visa_slots,StrictlyH1_H4_regular_visa_slots,"
    "Regular_H1B_H4_VisaSlotsChecking"
)
CVS_MIN_INTERVAL = 25.0  # minimum polling interval (absolute floor)
CVS_DEFAULT_GAP = 115.0  # synced checks: about once per site refresh, never closer than this
DEFAULT_CVS_URL = "https://checkvisaslots.com/latest-us-visa-availability/h-1b-regular/"


def load_config(env_file: str | None = None) -> Config:
    load_dotenv(env_file or os.getenv("ENV_FILE") or ".env", override=False)
    data_dir = Path(os.getenv("DATA_DIR", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    api_id = os.getenv("TELEGRAM_API_ID", "").strip()
    return Config(
        data_dir=data_dir,
        dry_run=_bool("DRY_RUN", True),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        tg_api_id=int(api_id) if api_id else None,
        tg_api_hash=os.getenv("TELEGRAM_API_HASH") or None,
        tg_phone=os.getenv("TELEGRAM_PHONE") or None,
        tg_session=Path(os.getenv("TELEGRAM_SESSION", str(data_dir / "telegram"))),
        tg_groups=_list("TELEGRAM_GROUPS", DEFAULT_GROUPS),
        tg_poll_seconds=_float("TELEGRAM_POLL_SECONDS", 25),
        tg_enabled=_bool("TELEGRAM_ENABLED", True),
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-6-luna"),
        openai_fallback_model=os.getenv("OPENAI_FALLBACK_MODEL", "gpt-6.1-terra"),
        ai_timeout=_float("AI_TIMEOUT_SECONDS", 10),
        ai_image_timeout=_float("AI_IMAGE_TIMEOUT_SECONDS", 20),
        max_image_bytes=_int("MAX_IMAGE_BYTES", 5_000_000),
        tg_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        bot_chats=_parse_bot_chats(os.getenv("TELEGRAM_ALERT_CHATS", "")),
        feed_bot_token=os.getenv("TELEGRAM_FEED_BOT_TOKEN") or None,
        # default: same people as the alert bot (a chat id is the same for every bot)
        feed_chats=(_parse_bot_chats(os.getenv("TELEGRAM_FEED_CHATS", ""))
                    or _parse_bot_chats(os.getenv("TELEGRAM_ALERT_CHATS", ""))[:1]),
        recipients=_parse_recipients(os.getenv("WHATSAPP_RECIPIENTS", "")),
        callmebot_min_gap=_float("CALLMEBOT_MIN_GAP_SECONDS", 10),
        cvs_enabled=_bool("CVS_ENABLED", False),
        cvs_url=os.getenv("CVS_URL", DEFAULT_CVS_URL),
        # Checks are timed to land just after each site refresh (~every 2 min). Two checks never
        # start closer than CVS_MIN_GAP_SECONDS, and never closer than the 25 s floor whatever
        # is configured. (The old fixed-rhythm CVS_POLL_SECONDS setting is no longer used.)
        cvs_poll_seconds=max(CVS_MIN_INTERVAL, _float("CVS_MIN_GAP_SECONDS", CVS_DEFAULT_GAP)),
        cvs_refresh_seconds=max(60.0, _float("CVS_REFRESH_SECONDS", 120)),
        cvs_browser_headed=_bool("CVS_BROWSER_HEADED", True),
        cvs_feed_check_summaries=_bool("CVS_FEED_CHECK_SUMMARIES", False),
        cvs_sync_checks=_bool("CVS_SYNC_CHECKS", True),
        cvs_max_alert_age_minutes=_float("CVS_MAX_ALERT_AGE_MINUTES", 5),
        cvs_network_controls_enabled=_bool("CVS_NETWORK_CONTROLS_ENABLED", False),
        timezones=_list("DISPLAY_TIMEZONES", "Asia/Kolkata"),
        heartbeat_time=os.getenv("HEARTBEAT_TIME_IST", "09:00"),
        source_fail_minutes=_int("SOURCE_FAIL_ALERT_MINUTES", 15),
        dup_summary_window=_int("DUPLICATE_SUMMARY_WINDOW_SECONDS", 120),
        max_alert_age_minutes=_int("MAX_ALERT_AGE_MINUTES", 30),
        same_sender_window=_int("SAME_SENDER_WINDOW_SECONDS", 300),
    )
