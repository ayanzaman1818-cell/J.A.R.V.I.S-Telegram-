"""Centralized configuration and logging setup for JARVIS."""

from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)


def env(name: str, default: str = "") -> str:
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

# --- Google Gemini ---
GEMINI_API_KEY: str = env("GEMINI_API_KEY")
LLM_MODEL: str = env("LLM_MODEL", "gemini-2.5-flash")
LLM_FAST_MODEL: str = env("LLM_FAST_MODEL", LLM_MODEL)
LLM_DEEP_MODEL: str = env("LLM_DEEP_MODEL", LLM_MODEL)
LLM_FALLBACK_MODEL: str = env("LLM_FALLBACK_MODEL", "gemini-2.5-flash-lite")

LLM_TIMEOUT_SECONDS: int = max(5, env_int("LLM_TIMEOUT_SECONDS", 30))
LLM_TEMPERATURE: float = max(0.0, min(env_float("LLM_TEMPERATURE", 0.4), 2.0))
LLM_MAX_OUTPUT_TOKENS: int = max(256, env_int("LLM_MAX_OUTPUT_TOKENS", 2048))
GEMINI_RETRY_ATTEMPTS: int = max(1, min(env_int("GEMINI_RETRY_ATTEMPTS", 2), 3))
GEMINI_FALLBACK_RETRIES: int = max(1, min(env_int("GEMINI_FALLBACK_RETRIES", 2), 3))
MAX_TOOL_STEPS: int = max(1, min(env_int("MAX_TOOL_STEPS", 6), 15))

# --- Personalization & Schedule ---
USER_NAME: str = env("USER_NAME", "sir")
LOCATION: str = env("LOCATION", "Dhaka")
TIMEZONE: str = env("TIMEZONE", "Asia/Dhaka")
BRIEFING_TIME: str = env("BRIEFING_TIME", "06:40")
BRIEFING_VOICE: bool = flag("BRIEFING_VOICE", "true")
BRIEFING_CALL: bool = flag("BRIEFING_CALL", "false")
QUIET_HOURS_START: str = env("QUIET_HOURS_START", "22:00")
QUIET_HOURS_END: str = env("QUIET_HOURS_END", "07:00")

# --- Voice & Calls ---
GROQ_API_KEY: str = env("GROQ_API_KEY")
TTS_VOICE: str = env("TTS_VOICE", "en-GB-RyanNeural")

TWILIO_SID: str = env("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN: str = env("TWILIO_AUTH_TOKEN")
TWILIO_NUMBER: str = env("TWILIO_NUMBER")
MY_PHONE: str = env("MY_PHONE")
TWILIO_VOICE: str = env("TWILIO_VOICE", "alice")

# --- Composio Integrations ---
COMPOSIO_API_KEY: str = env("COMPOSIO_API_KEY")
COMPOSIO_USER_ID: str = env("COMPOSIO_USER_ID")
COMPOSIO_GMAIL_ACCOUNT: str = env("COMPOSIO_GMAIL_ACCOUNT")
COMPOSIO_GOOGLEDRIVE_ACCOUNT: str = env("COMPOSIO_GOOGLEDRIVE_ACCOUNT")
COMPOSIO_GOOGLECALENDAR_ACCOUNT: str = env("COMPOSIO_GOOGLECALENDAR_ACCOUNT")
KNOWLEDGE_DRIVE_FOLDER: str = env("KNOWLEDGE_DRIVE_FOLDER")
COMPOSIO_TIMEOUT_SECONDS: int = max(5, env_int("COMPOSIO_TIMEOUT_SECONDS", 25))

# --- Database & Logging ---
DB_PATH: Path = Path(env("DB_PATH", str(BASE_DIR / "jarvis.db")))
if not DB_PATH.is_absolute():
    DB_PATH = BASE_DIR / DB_PATH

LOG_FILE: Path = Path(env("LOG_FILE", str(BASE_DIR / "jarvis.log")))
if not LOG_FILE.is_absolute():
    LOG_FILE = BASE_DIR / LOG_FILE

LOG_LEVEL: str = env("LOG_LEVEL", "INFO").upper()


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
    ):
        if secret and len(secret) > 6:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    cleaned = re.sub(r"(key=)[^&\s\"']+", r"\1[REDACTED]", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot[REDACTED]", cleaned)
    return cleaned


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

    formatter = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
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
