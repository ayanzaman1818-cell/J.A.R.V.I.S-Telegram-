"""Application entrypoint for JARVIS."""

from __future__ import annotations

import logging
from telegram.ext import Application

import config
from database.repository import db
from services.telegram import TelegramService, build_telegram_app

log = config.setup_logging()


async def post_init(app: Application) -> None:
    """Initialize database schema, register bot commands, launch desktop overlay if enabled, and notify owner."""
    db.init()
    db.audit("startup", "Jarvis started")
    await app.bot.set_my_commands(TelegramService.bot_commands())
    if config.DESKTOP_OVERLAY_ENABLED:
        try:
            import asyncio
            from services.desktop_overlay import start_desktop_overlay

            start_desktop_overlay(
                owner_id=config.OWNER_ID,
                bot=app.bot,
                job_queue=getattr(app, "job_queue", None),
                async_loop=asyncio.get_running_loop(),
            )
        except Exception:
            log.warning("Could not start Desktop Companion Overlay", exc_info=True)

    if config.TWILIO_WEBHOOK_ENABLED:
        try:
            from services.twilio_webhook import start_twilio_webhook_server

            await start_twilio_webhook_server(
                bot=app.bot,
                job_queue=getattr(app, "job_queue", None),
            )
        except Exception:
            log.warning("Could not start JARVIS 2-way voice call server", exc_info=True)

    try:
        from services.telegram_call import is_native_call_configured, start_native_telegram_call_service

        if is_native_call_configured():
            await start_native_telegram_call_service(
                bot=app.bot,
                job_queue=getattr(app, "job_queue", None),
            )
    except Exception:
        log.warning("Could not start Native Telegram VoIP Calling userbot", exc_info=True)

    if config.OWNER_ID:
        try:
            await app.bot.send_message(config.OWNER_ID, "Jarvis online.")
            from services import voice

            clip = voice.pick_openpeon_clip(category="session.start", query="welcome home")
            if clip and not config.is_quiet_hours():
                clip_path, label = clip
                with open(clip_path, "rb") as f:
                    await app.bot.send_voice(config.OWNER_ID, f, caption=f'🎩 "{label}"')
        except Exception:
            log.warning("Could not send startup notification to OWNER_ID=%s", config.OWNER_ID)


async def post_shutdown(app: Application) -> None:
    """Cleanly shut down background resources (including Desktop Overlay, Telegram VoIP userbot, Twilio webhook, and Playwright browser)."""
    try:
        from services.telegram_call import stop_native_telegram_call_service

        await stop_native_telegram_call_service()
    except Exception:
        log.debug("Error stopping Native Telegram VoIP service", exc_info=True)
    try:
        from services.twilio_webhook import stop_twilio_webhook_server

        await stop_twilio_webhook_server()
    except Exception:
        log.debug("Error stopping Twilio webhook server", exc_info=True)
    try:
        from services.desktop_overlay import stop_desktop_overlay

        stop_desktop_overlay()
    except Exception:
        log.debug("Error stopping Desktop Companion Overlay", exc_info=True)
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


def _terminate_stale_jarvis_instances() -> None:
    """Detect and terminate any previous main.py process left running in the background so it never locks SQLite or causes a Telegram polling conflict."""
    import os
    import subprocess

    my_pid = os.getpid()
    parent_pid = os.getppid()
    if os.name != "nt":
        return
    try:
        ps_cmd = (
            "Get-CimInstance Win32_Process -Filter \"Name LIKE 'python%' OR Name LIKE 'py%'\" | "
            "Where-Object { $_.CommandLine -match 'main\\.py' -and $_.ProcessId -ne "
            f"{my_pid} -and $_.ProcessId -ne {parent_pid} }} | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_cmd],
            capture_output=True,
            timeout=6,
        )
    except Exception:
        pass


def main() -> None:
    if not config.TELEGRAM_TOKEN or not config.GEMINI_API_KEY:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and GEMINI_API_KEY in .env.")

    _terminate_stale_jarvis_instances()
    db.init()
    service = create_service()
    app = build_telegram_app(
        service,
        post_init=post_init,
        post_shutdown=post_shutdown,
    )
    log.info("Starting JARVIS Telegram polling...")
    # bootstrap_retries=-1: if the PC boots/wakes before Wi-Fi is up, keep retrying
    # instead of exiting with "Failed run number 0 of 0. Aborting."
    try:
        app.run_polling(bootstrap_retries=-1)
    except KeyboardInterrupt:
        log.info("Ctrl+C received, shutting down JARVIS...")
    finally:
        try:
            from services.desktop_overlay import stop_desktop_overlay

            stop_desktop_overlay()
        except Exception:
            pass
        logging.shutdown()
        # Ensure C++/Win32 hook threads (PySide6, pynput, ThreadPoolExecutor) never hang the shell prompt
        import os

        os._exit(0)


if __name__ == "__main__":
    main()
