"""Centralized configuration and logging setup for JARVIS."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, time as dtime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)


def env(name: str, default: str = "") -> str:
    """Read and strip an environment variable."""
    """Read and strip an environment variable."""
    return os.getenv(name, default).strip()


def env_int(name: str, default: int = 0) -> int:
    """Read an integer environment variable safely."""
    raw = env(name, str(default))
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(name: str, default: float = 0.0) -> float:
    """Read a float environment variable safely."""
    raw = env(name, str(default))
    try:
        return float(raw)
    except ValueError:
        return default


def flag(name: str, default: str = "false") -> bool:
    """Read a boolean flag from environment variables."""
    return env(name, default).lower() in ("1", "true", "yes", "on")


def _parse_user_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit() and int(part) > 0:
            ids.add(int(part))
    return ids


# --- Telegram ---
TELEGRAM_TOKEN: str = env("TELEGRAM_BOT_TOKEN")
OWNER_ID: int = env_int("OWNER_ID", 0)
ALLOWED_USER_IDS: set[int] = _parse_user_ids(env("ALLOWED_USER_IDS", ""))
if OWNER_ID > 0:
    ALLOWED_USER_IDS.add(OWNER_ID)

# Optional Native Telegram VoIP Calling (MTProto Userbot via py-tgcalls + Telethon)
# Defaults to Telegram Desktop's official public API_ID/API_HASH if not overridden in .env.
TELEGRAM_API_ID: int = env_int("TELEGRAM_API_ID", 2040)
TELEGRAM_API_HASH: str = env("TELEGRAM_API_HASH", "b18441a1ff607e10a989891a5462e627")
TELEGRAM_CALL_SESSION: Path = Path(
    env("TELEGRAM_CALL_SESSION", str(BASE_DIR / "jarvis_call_userbot.session"))
)
if not TELEGRAM_CALL_SESSION.is_absolute():
    TELEGRAM_CALL_SESSION = BASE_DIR / TELEGRAM_CALL_SESSION

# --- Google Gemini ---
GEMINI_API_KEY: str = env("GEMINI_API_KEY")
LLM_MODEL: str = env("LLM_MODEL", "gemma-4-26b-a4b-it")
LLM_FAST_MODEL: str = env("LLM_FAST_MODEL", LLM_MODEL)
LLM_DEEP_MODEL: str = env("LLM_DEEP_MODEL", LLM_MODEL)
LLM_FALLBACK_MODEL: str = env("LLM_FALLBACK_MODEL", "gemini-3.5-flash-lite")

# Local LLM (LM Studio default http://127.0.0.1:1234/v1; also supports Ollama/llama.cpp)
# Prefix any model with "lmstudio:" or "local:" (e.g. LLM_MODEL=lmstudio:bonsai-27b)
LOCAL_LLM_BASE_URL: str = env("LOCAL_LLM_BASE_URL", "http://127.0.0.1:1234/v1")
LOCAL_LLM_API_KEY: str = env("LOCAL_LLM_API_KEY", "lm-studio")

LLM_TIMEOUT_SECONDS: int = max(5, env_int("LLM_TIMEOUT_SECONDS", 30))
# When a fallback model exists, give the primary this long before switching over.
LLM_PRIMARY_TIMEOUT_SECONDS: int = max(3, env_int("LLM_PRIMARY_TIMEOUT_SECONDS", 12))
LLM_TEMPERATURE: float = max(0.0, min(env_float("LLM_TEMPERATURE", 0.4), 2.0))
LLM_MAX_OUTPUT_TOKENS: int = max(256, env_int("LLM_MAX_OUTPUT_TOKENS", 2048))
# Thinking level for routine vs complex turns ("LOW" or "MINIMAL" on routine turns, "MEDIUM" on complex queries).
LLM_THINKING_LEVEL: str = env("LLM_THINKING_LEVEL", "LOW").upper()
LLM_DEEP_THINKING_LEVEL: str = env("LLM_DEEP_THINKING_LEVEL", "MEDIUM").upper()
GEMINI_RETRY_ATTEMPTS: int = max(1, min(env_int("GEMINI_RETRY_ATTEMPTS", 2), 3))
GEMINI_FALLBACK_RETRIES: int = max(1, min(env_int("GEMINI_FALLBACK_RETRIES", 2), 3))
MAX_TOOL_STEPS: int = max(1, min(env_int("MAX_TOOL_STEPS", 6), 15))

# --- Personalization & Schedule ---
USER_NAME: str = env("USER_NAME", "sir")
LOCATION: str = env("LOCATION", "Dhaka")
TIMEZONE: str = env("TIMEZONE", "Asia/Dhaka")
BRIEFING_TIME: str = env("BRIEFING_TIME", "06:40")
EVENING_BRIEFING_TIME: str = env("EVENING_BRIEFING_TIME", "21:30")
BRIEFING_VOICE: bool = flag("BRIEFING_VOICE", "true")
BRIEFING_CALL: bool = flag("BRIEFING_CALL", "false")
PROACTIVE_ALERTS: bool = flag("PROACTIVE_ALERTS", "true")
BATTERY_ALERT_THRESHOLD: int = max(5, min(env_int("BATTERY_ALERT_THRESHOLD", 20), 95))
QUIET_HOURS_START: str = env("QUIET_HOURS_START", "22:00")
QUIET_HOURS_END: str = env("QUIET_HOURS_END", "07:00")

# Autonomous Self-Study / Student Mode
AUTO_STUDY_ENABLED: bool = flag("AUTO_STUDY_ENABLED", "true")
AUTO_STUDY_INTERVAL_HOURS: int = max(1, min(env_int("AUTO_STUDY_INTERVAL_HOURS", 6), 48))
AUTO_STUDY_NOTIFY: bool = flag("AUTO_STUDY_NOTIFY", "false")

# Desktop Companion Overlay & Computer-Use
DESKTOP_OVERLAY_ENABLED: bool = flag("DESKTOP_OVERLAY_ENABLED", "false")
DESKTOP_OVERLAY_HOTKEY: str = env("DESKTOP_OVERLAY_HOTKEY", "ctrl+alt+space")
DESKTOP_AUTO_SEE_DEFAULT: bool = flag("DESKTOP_AUTO_SEE_DEFAULT", "false")
DESKTOP_VOICE_REPLY: bool = flag("DESKTOP_VOICE_REPLY", "false")

# --- Voice & Calls ---
GROQ_API_KEY: str = env("GROQ_API_KEY")
TTS_VOICE: str = env("TTS_VOICE", "en-GB-RyanNeural")
TTS_RATE: str = env("TTS_RATE", "+5%")
TTS_PITCH: str = env("TTS_PITCH", "-4Hz")
VOICE_CHAT_DEFAULT: bool = flag("VOICE_CHAT_DEFAULT", "false")

# Optional ElevenLabs TTS (for cloned Marvel JARVIS / Paul Bettany voice)
ELEVENLABS_API_KEY: str = env("ELEVENLABS_API_KEY")
# Default ElevenLabs British butler voice ID ("Daniel" / "George" or custom Paul Bettany clone ID)
ELEVENLABS_VOICE_ID: str = env("ELEVENLABS_VOICE_ID", "onwK4e9ZLuTAKqWW03F9")
ELEVENLABS_MODEL: str = env("ELEVENLABS_MODEL", "eleven_multilingual_v2")

# Optional Fish Audio TTS (fish.audio cloned JARVIS model)
FISH_API_KEY: str = env("FISH_API_KEY")
FISH_MODEL_ID: str = env("FISH_MODEL_ID") or env("FISH_VOICE_ID")
FISH_MODEL: str = env("FISH_MODEL", "s2.1-pro-free")

TWILIO_SID: str = env("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN: str = env("TWILIO_AUTH_TOKEN")
TWILIO_NUMBER: str = env("TWILIO_NUMBER")
MY_PHONE: str = env("MY_PHONE")
TWILIO_VOICE: str = env("TWILIO_VOICE", "Polly.Brian")
TWILIO_WEBHOOK_URL: str = env("TWILIO_WEBHOOK_URL")
TWILIO_WEBHOOK_HOST: str = env("TWILIO_WEBHOOK_HOST", "0.0.0.0")
TWILIO_WEBHOOK_PORT: int = max(1, min(env_int("TWILIO_WEBHOOK_PORT", 8787), 65535))
TWILIO_WEBHOOK_ENABLED: bool = flag("TWILIO_WEBHOOK_ENABLED", "true")
TWILIO_USE_CUSTOM_TTS: bool = flag("TWILIO_USE_CUSTOM_TTS", "true")

# --- Composio Integrations & Knowledge ---
COMPOSIO_API_KEY: str = env("COMPOSIO_API_KEY")
COMPOSIO_USER_ID: str = env("COMPOSIO_USER_ID")
COMPOSIO_GMAIL_ACCOUNT: str = env("COMPOSIO_GMAIL_ACCOUNT")
COMPOSIO_GOOGLEDRIVE_ACCOUNT: str = env("COMPOSIO_GOOGLEDRIVE_ACCOUNT")
COMPOSIO_GOOGLECALENDAR_ACCOUNT: str = env("COMPOSIO_GOOGLECALENDAR_ACCOUNT")
KNOWLEDGE_DRIVE_FOLDER: str = env("KNOWLEDGE_DRIVE_FOLDER")
COMPOSIO_TIMEOUT_SECONDS: int = max(5, env_int("COMPOSIO_TIMEOUT_SECONDS", 25))

KNOWLEDGE_LOCAL_DIR: Path = Path(env("KNOWLEDGE_LOCAL_DIR", str(BASE_DIR / "notes")))
if not KNOWLEDGE_LOCAL_DIR.is_absolute():
    KNOWLEDGE_LOCAL_DIR = BASE_DIR / KNOWLEDGE_LOCAL_DIR

INBOX_DIR: Path = Path(env("INBOX_DIR", str(BASE_DIR / "inbox")))
if not INBOX_DIR.is_absolute():
    INBOX_DIR = BASE_DIR / INBOX_DIR

# --- Database & Logging ---
DB_PATH: Path = Path(env("DB_PATH", str(BASE_DIR / "jarvis.db")))
if not DB_PATH.is_absolute():
    DB_PATH = BASE_DIR / DB_PATH

LOG_FILE: Path = Path(env("LOG_FILE", str(BASE_DIR / "jarvis.log")))
if not LOG_FILE.is_absolute():
    LOG_FILE = BASE_DIR / LOG_FILE

LOG_LEVEL: str = env("LOG_LEVEL", "INFO").upper()

# --- Backups ---
BACKUP_DIR: Path = Path(env("BACKUP_DIR", str(BASE_DIR / "backups")))
if not BACKUP_DIR.is_absolute():
    BACKUP_DIR = BASE_DIR / BACKUP_DIR
BACKUP_TIME: str = env("BACKUP_TIME", "03:30")
BACKUP_KEEP: int = max(1, min(env_int("BACKUP_KEEP", 7), 90))


def get_zoneinfo() -> ZoneInfo:
    """Return configured ZoneInfo or fall back to UTC if invalid."""
    try:
        return ZoneInfo(TIMEZONE)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def is_authorized(user_id: int | None) -> bool:
    """Return True if the Telegram user ID is the owner or in ALLOWED_USER_IDS."""
    if not user_id:
        return False
    if OWNER_ID and user_id == OWNER_ID:
        return True
    return user_id in ALLOWED_USER_IDS


def is_owner_user(user_id: int | None) -> bool:
    """Return True only if the Telegram user ID matches OWNER_ID."""
    return bool(user_id and OWNER_ID and user_id == OWNER_ID)


def is_quiet_hours(now_dt: datetime | None = None) -> bool:
    """Return True if current local time falls within QUIET_HOURS_START .. QUIET_HOURS_END."""
    try:
        sh, sm = (int(x) for x in QUIET_HOURS_START.split(":"))
        eh, em = (int(x) for x in QUIET_HOURS_END.split(":"))
        start_t = dtime(sh, sm)
        end_t = dtime(eh, em)
    except Exception:
        return False

    current = (now_dt or datetime.now(get_zoneinfo())).time()
    if start_t <= end_t:
        return start_t <= current < end_t
    return current >= start_t or current < end_t


def redact_secrets(text: str) -> str:
    """Scrub API keys, Telegram bot tokens, and Twilio credentials from log strings."""
    if not text:
        return ""
    cleaned = text
    for secret in (
        TELEGRAM_TOKEN,
        GEMINI_API_KEY,
        GROQ_API_KEY,
        TWILIO_TOKEN,
        COMPOSIO_API_KEY,
        ELEVENLABS_API_KEY,
        FISH_API_KEY,
    ):
        if secret and len(secret) > 6:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    cleaned = re.sub(r"(key=)[^&\s\"']+", r"\1[REDACTED]", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot[REDACTED]", cleaned)
    # Bare Telegram bot tokens (e.g. in "token `123:ABC...` was rejected" errors),
    # including old/revoked tokens that no longer match TELEGRAM_TOKEN.
    cleaned = re.sub(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b", "[REDACTED_BOT_TOKEN]", cleaned)
    return cleaned


class RedactingFormatter(logging.Formatter):
    """Formatter that also scrubs secrets from rendered tracebacks (exc_info / stack_info)."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record))


class SecretRedactingFilter(logging.Filter):
    """Logging filter that guarantees no secret tokens are written to disk or stdout."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    redact_secrets(str(a)) if isinstance(a, str) else a
                    for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: redact_secrets(str(v)) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
        return True


def setup_logging() -> logging.Logger:
    """Configure root and application loggers with rotation and secret redaction."""
    root = logging.getLogger()
    if getattr(root, "_jarvis_configured", False):
        return logging.getLogger("jarvis")

    level = getattr(logging, LOG_LEVEL, logging.INFO)
    root.setLevel(level)

    formatter = RedactingFormatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    redactor = SecretRedactingFilter()

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(redactor)
    root.addHandler(console_handler)

    try:
        file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redactor)
        root.addHandler(file_handler)
    except OSError as exc:
        root.warning("Could not attach file logger at %s: %s", LOG_FILE, exc)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.INFO)

    setattr(root, "_jarvis_configured", True)
    return logging.getLogger("jarvis")
