"""Application entrypoint for JARVIS."""

from __future__ import annotations

import logging
from telegram.ext import Application

import config
from database.repository import db
from services.telegram import TelegramService, build_telegram_app

log = config.setup_logging()


async def post_init(app: Application) -> None:
    """Initialize database schema, register bot commands, and notify owner on startup."""
    db.init()
    db.audit("startup", "Jarvis started")
    await app.bot.set_my_commands(TelegramService.bot_commands())
    if config.OWNER_ID:
        try:
            await app.bot.send_message(config.OWNER_ID, "Jarvis online.")
        except Exception:
            log.warning("Could not send startup notification to OWNER_ID=%s", config.OWNER_ID)


async def post_shutdown(app: Application) -> None:
    """Cleanly shut down background resources (including Playwright browser)."""
    try:
        from tools.browser import close_browser

        await close_browser()
    except Exception:
        log.warning("Error closing Playwright browser during shutdown", exc_info=True)
    db.audit("shutdown", "Jarvis stopped")


def create_service() -> TelegramService:
    """Instantiate the TelegramService and wire available subsystems."""
    respond_fn = None
    reset_fn = None
    briefing_fn = None
    phone_call_fn = None
    transcribe_fn = None
    tts_fn = None
    composio_enabled_fn = None
    context_factory = None

    try:
        from brain.agent import reset as brain_reset, respond as brain_respond

        respond_fn = brain_respond
        reset_fn = brain_reset
    except Exception:
        log.warning("Could not load brain.agent subsystem", exc_info=True)

    try:
        from tools.base import ToolContext

        context_factory = lambda bot, chat_id, job_queue, user_id: ToolContext(
            bot=bot, chat_id=chat_id, job_queue=job_queue, user_id=user_id
        )
    except Exception:
        log.warning("Could not load tools.base subsystem", exc_info=True)

    try:
        from services.briefing import send_briefing

        briefing_fn = send_briefing
    except Exception:
        log.warning("Could not load services.briefing subsystem", exc_info=True)

    try:
        from services.voice import phone_call, transcribe_audio, tts_file

        phone_call_fn = phone_call
        transcribe_fn = transcribe_audio
        tts_fn = tts_file
    except Exception:
        log.warning("Could not load services.voice subsystem", exc_info=True)

    try:
        from tools.integrations import composio_enabled

        composio_enabled_fn = composio_enabled
    except Exception:
        log.warning("Could not load tools.integrations subsystem", exc_info=True)

    return TelegramService(
        respond_fn=respond_fn,
        reset_fn=reset_fn,
        briefing_fn=briefing_fn,
        phone_call_fn=phone_call_fn,
        transcribe_fn=transcribe_fn,
        tts_fn=tts_fn,
        composio_enabled_fn=composio_enabled_fn,
        context_factory=context_factory,
    )


def main() -> None:
    if not config.TELEGRAM_TOKEN or not config.GEMINI_API_KEY:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and GEMINI_API_KEY in .env.")

    db.init()
    service = create_service()
    app = build_telegram_app(
        service,
        post_init=post_init,
        post_shutdown=post_shutdown,
    )
    log.info("Starting JARVIS Telegram polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
