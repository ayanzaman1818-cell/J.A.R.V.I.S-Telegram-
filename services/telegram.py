"""Professional Telegram interface for JARVIS with non-blocking execution and user isolation."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from typing import Any, Callable, Coroutine

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from database.repository import db

log = logging.getLogger("jarvis.telegram")

AgentResponder = Callable[[Any, str], Coroutine[Any, Any, str]]
ResetHandler = Callable[[int], Any]
BriefingSender = Callable[[Any, int, Any], Coroutine[Any, Any, None]]
PhoneCaller = Callable[[str], Coroutine[Any, Any, str]]
VoiceTranscriber = Callable[[bytes], Coroutine[Any, Any, str]]
TtsGenerator = Callable[[str], Coroutine[Any, Any, str]]
ComposioChecker = Callable[[], bool]


class TelegramService:
    """Manages Telegram bot commands, text/voice handlers, typing indicators, and authorization."""

    def __init__(
        self,
        *,
        respond_fn: AgentResponder | None = None,
        reset_fn: ResetHandler | None = None,
        briefing_fn: BriefingSender | None = None,
        phone_call_fn: PhoneCaller | None = None,
        transcribe_fn: VoiceTranscriber | None = None,
        tts_fn: TtsGenerator | None = None,
        composio_enabled_fn: ComposioChecker | None = None,
        context_factory: Callable[[Any, int, Any, int], Any] | None = None,
    ) -> None:
        self.respond_fn = respond_fn
        self.reset_fn = reset_fn
        self.briefing_fn = briefing_fn
        self.phone_call_fn = phone_call_fn
        self.transcribe_fn = transcribe_fn
        self.tts_fn = tts_fn
        self.composio_enabled_fn = composio_enabled_fn or (lambda: bool(config.COMPOSIO_API_KEY))
        self.context_factory = context_factory

    def _is_authorized(self, update: Update) -> bool:
        user = update.effective_user
        return config.is_authorized(user.id if user else None)

    def _is_owner(self, update: Update) -> bool:
        user = update.effective_user
        return config.is_owner_user(user.id if user else None)

    async def _reject_unauthorized(self, update: Update) -> None:
        user = update.effective_user
        uid = user.id if user else "unknown"
        if update.message:
            await update.message.reply_text(
                f"Not configured for your account. Your Telegram ID is {uid}; "
                "set it in OWNER_ID (or ALLOWED_USER_IDS) in .env and restart."
            )

    def _make_ctx(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> Any:
        if self.context_factory:
            return self.context_factory(context.bot, chat_id, context.job_queue, user_id)
        return type(
            "Ctx",
            (),
            {
                "bot": context.bot,
                "chat_id": chat_id,
                "job_queue": context.job_queue,
                "user_id": user_id,
            },
        )()

    @staticmethod
    async def keep_typing(bot: Any, chat_id: int, stop: asyncio.Event) -> None:
        """Refresh Telegram's typing indicator every 4 seconds while a task is running."""
        while not stop.is_set():
            try:
                await bot.send_chat_action(chat_id, "typing")
            except Exception:
                log.debug("Typing indicator refresh failed", exc_info=True)
            try:
                await asyncio.wait_for(stop.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                continue

    @staticmethod
    async def send_long(bot: Any, chat_id: int, text: str) -> None:
        """Split responses longer than 4,000 characters and retry transient Telegram network errors."""
        payload = (text or "").strip() or "(No response generated.)"
        for i in range(0, len(payload), 4000):
            chunk = payload[i : i + 4000]
            for attempt in range(3):
                try:
                    await bot.send_message(chat_id, chunk)
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(2 * (attempt + 1))

    async def cmd_myid(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message and update.effective_user:
            await update.message.reply_text(
                f"Your Telegram ID is {update.effective_user.id}"
            )

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            await self._reject_unauthorized(update)
            return
        if not update.message:
            return
        await update.message.reply_text(
            f"At your service, {config.USER_NAME}. Ask me anything, or use:\n"
            "/help - show all available commands\n"
            "/briefing - morning debrief right now\n"
            "/tasks - view your open tasks\n"
            "/reminders - view upcoming reminders\n"
            "/memory - view saved memories & preferences\n"
            "/status - check system health & model settings\n"
            "/model - inspect Gemini model routing & cooldowns\n"
            "/reset - clear short-term conversation context"
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            await self._reject_unauthorized(update)
            return
        if not update.message:
            return
        await update.message.reply_text(
            "JARVIS Commands:\n"
            "/start - greeting and quick overview\n"
            "/help - list available commands and capabilities\n"
            "/reset (or /clear) - clear current conversation context while keeping long-term memory\n"
            "/status - display system health, models, and database counts\n"
            "/model - inspect active Gemini models and circuit-breaker status\n"
            "/memory (or /memories) - list remembered facts and user preferences\n"
            "/tasks - list open personal tasks\n"
            "/reminders - list active scheduled reminders\n"
            "/briefing - generate and deliver your daily briefing now\n"
            "/call <message> - ring your phone with a spoken message (owner only)\n"
            "/myid - display your numeric Telegram user ID"
        )

    async def cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        chat_id = update.effective_chat.id
        if self.reset_fn:
            res = self.reset_fn(chat_id)
            if inspect.isawaitable(res):
                await res
        else:
            db.clear_history_messages(chat_id)
            db.clear_pending_actions(chat_id)
        if update.message:
            await update.message.reply_text(
                "Memory of this conversation wiped. Long-term notes kept."
            )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        user_id = update.effective_user.id
        state = db.status(user_id=user_id)
        composio_state = "configured" if self.composio_enabled_fn() else "not configured"
        if config.GROQ_API_KEY:
            voice_state = "configured (Groq Whisper + Gemini fallback)"
        elif config.GEMINI_API_KEY:
            voice_state = "configured (Gemini multimodal STT)"
        else:
            voice_state = "not configured"
        if update.message:
            await update.message.reply_text(
                "Jarvis status: online\n"
                f"- Primary model: {config.LLM_MODEL}\n"
                f"- Fallback model: {config.LLM_FALLBACK_MODEL}\n"
                f"- Persistent memories: {state['memories']}\n"
                f"- Saved preferences: {state['preferences']}\n"
                f"- Open tasks: {state['open_tasks']}\n"
                f"- Active reminders: {state['active_reminders']}\n"
                f"- Voice STT: {voice_state}\n"
                f"- Composio: {composio_state}"
            )

    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        if not update.message:
            return
        try:
            from brain.gemini import model_health

            health = model_health()
        except Exception:
            health = {
                "primary": config.LLM_MODEL,
                "fast": config.LLM_FAST_MODEL,
                "deep": config.LLM_DEEP_MODEL,
                "fallback": config.LLM_FALLBACK_MODEL,
                "primary_paused_seconds": 0,
            }
        paused = health["primary_paused_seconds"]
        state = f"paused ({paused}s remaining)" if paused else "healthy"
        await update.message.reply_text(
            "Gemini Model Routing:\n"
            f"- Default: {health['primary']} ({state})\n"
            f"- Fast: {health['fast']}\n"
            f"- Deep: {health['deep']}\n"
            f"- Fallback: {health['fallback']}"
        )

    async def cmd_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        user_id = update.effective_user.id
        tasks = db.list_tasks(user_id=user_id)
        lines = [
            f"#{t['id']} [{t['priority']}] {t['title']}"
            + (f" (due {t['due_at']})" if t.get("due_at") else "")
            for t in tasks
        ]
        text = "\n".join(lines) or "No open tasks."
        if update.message:
            await update.message.reply_text(text)

    async def cmd_reminders(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        user_id = update.effective_user.id
        reminders = db.upcoming_reminders(limit=15, user_id=user_id)
        lines = []
        for r in reminders:
            rec = f" ({r['recurrence']})" if r.get("recurrence") else ""
            call_flag = " [call]" if r.get("call") else ""
            lines.append(f"#{r['id']} {r['due_at']}{rec}{call_flag}: {r['message']}")
        text = "\n".join(lines) or "No active reminders."
        if update.message:
            await update.message.reply_text(text)

    async def cmd_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        user_id = update.effective_user.id
        memories = db.memories(limit=30, user_id=user_id)
        prefs = db.list_preferences(user_id=user_id)

        sections: list[str] = []
        if prefs:
            pref_lines = [f"- {k}: {v}" for k, v in prefs.items()]
            sections.append("Preferences:\n" + "\n".join(pref_lines))
        if memories:
            mem_lines = [f"- {m}" for m in memories]
            sections.append("Saved Memories:\n" + "\n".join(mem_lines))

        text = "\n\n".join(sections) or "No saved memories."
        if update.message:
            await update.message.reply_text(text)

    async def cmd_briefing(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        chat_id = update.effective_chat.id
        if not self.briefing_fn:
            if update.message:
                await update.message.reply_text("Briefing service is not initialized yet.")
            return
        await context.bot.send_chat_action(chat_id, "typing")
        await self.briefing_fn(context.bot, chat_id, context.job_queue)

    async def cmd_call(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            if update.message:
                await update.message.reply_text("Only the bot owner can place phone calls.")
            return
        if not self.phone_call_fn:
            if update.message:
                await update.message.reply_text("Voice call service is not initialized yet.")
            return
        text = " ".join(context.args or []) or "Jarvis here. Just testing the line."
        try:
            await self.phone_call_fn(text)
            if update.message:
                await update.message.reply_text("Calling you now.")
        except Exception as exc:
            if update.message:
                await update.message.reply_text(f"Couldn't place the call: {exc}")

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update) or not update.message:
            return
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        text = update.message.text or ""
        message_started = time.perf_counter()

        log.info(
            "Telegram text received: chat_id=%s user_id=%s message_id=%s chars=%s",
            chat_id,
            user_id,
            update.message.message_id,
            len(text),
        )

        if not self.respond_fn:
            await update.message.reply_text("Jarvis brain is still initializing.")
            return

        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(
            self.keep_typing(context.bot, chat_id, stop_typing)
        )
        try:
            ctx = self._make_ctx(context, chat_id, user_id)
            brain_started = time.perf_counter()
            try:
                reply = await self.respond_fn(ctx, text)
            finally:
                stop_typing.set()
                typing_task.cancel()
            log.info(
                "Agent response finished: chat_id=%s elapsed=%.2fs",
                chat_id,
                time.perf_counter() - brain_started,
            )
            await self.send_long(context.bot, chat_id, reply)
        except Exception:
            log.exception(
                "Text handler failed after %.2fs",
                time.perf_counter() - message_started,
            )
            await update.message.reply_text(
                "Something went wrong while processing your message. Please try again."
            )

    async def handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update) or not update.message:
            return
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id

        if not self.transcribe_fn or not (config.GROQ_API_KEY or config.GEMINI_API_KEY):
            await update.message.reply_text(
                "I can't hear voice messages yet. Add GROQ_API_KEY or GEMINI_API_KEY to .env."
            )
            return

        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(
            self.keep_typing(context.bot, chat_id, stop_typing)
        )
        try:
            voice_obj = update.message.voice or update.message.audio
            if not voice_obj:
                return
            tg_file = await voice_obj.get_file()
            data = bytes(await tg_file.download_as_bytearray())
            text = (await self.transcribe_fn(data)).strip()
            if not text:
                await update.message.reply_text("I didn't catch anything in that.")
                return
            await update.message.reply_text(f"Heard: {text}")
            if not self.respond_fn:
                return
            ctx = self._make_ctx(context, chat_id, user_id)
            reply = await self.respond_fn(ctx, text)
        finally:
            stop_typing.set()
            typing_task.cancel()

        await self.send_long(context.bot, chat_id, reply)

        if self.tts_fn:
            try:
                path = await self.tts_fn(reply)
                try:
                    with open(path, "rb") as f:
                        await context.bot.send_voice(chat_id, f)
                finally:
                    if os.path.exists(path):
                        os.unlink(path)
            except Exception:
                log.exception("Voice reply TTS failed")

    @staticmethod
    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("Unhandled Telegram error", exc_info=context.error)

    @staticmethod
    def bot_commands() -> list[BotCommand]:
        return [
            BotCommand("start", "Greeting and quick overview"),
            BotCommand("help", "Show available commands"),
            BotCommand("briefing", "Morning debrief now"),
            BotCommand("tasks", "List open tasks"),
            BotCommand("reminders", "List upcoming reminders"),
            BotCommand("memory", "List saved memories & preferences"),
            BotCommand("status", "Show Jarvis status"),
            BotCommand("model", "Show active Gemini models"),
            BotCommand("reset", "Reset conversation history"),
            BotCommand("call", "Ring my phone"),
            BotCommand("myid", "Show my Telegram ID"),
        ]

    def register_handlers(self, app: Application) -> None:
        """Attach all command and message handlers with block=False for responsiveness."""
        app.add_handler(CommandHandler("start", self.cmd_start, block=False))
        app.add_handler(CommandHandler("help", self.cmd_help, block=False))
        app.add_handler(CommandHandler("myid", self.cmd_myid, block=False))
        app.add_handler(CommandHandler("briefing", self.cmd_briefing, block=False))
        app.add_handler(CommandHandler("call", self.cmd_call, block=False))
        app.add_handler(CommandHandler(["reset", "clear"], self.cmd_reset, block=False))
        app.add_handler(CommandHandler("status", self.cmd_status, block=False))
        app.add_handler(CommandHandler("model", self.cmd_model, block=False))
        app.add_handler(CommandHandler("tasks", self.cmd_tasks, block=False))
        app.add_handler(CommandHandler("reminders", self.cmd_reminders, block=False))
        app.add_handler(CommandHandler(["memory", "memories"], self.cmd_memory, block=False))
        app.add_handler(
            MessageHandler(filters.VOICE | filters.AUDIO, self.handle_voice, block=False)
        )
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text, block=False)
        )
        app.add_error_handler(self.on_error)


def build_telegram_app(
    service: TelegramService,
    *,
    post_init: Callable[[Application], Coroutine[Any, Any, None]] | None = None,
    post_shutdown: Callable[[Application], Coroutine[Any, Any, None]] | None = None,
) -> Application:
    """Build and configure the python-telegram-bot Application."""
    builder = (
        ApplicationBuilder()
        .token(config.TELEGRAM_TOKEN)
        .concurrent_updates(8)
    )
    if post_init:
        builder = builder.post_init(post_init)
    if post_shutdown:
        builder = builder.post_shutdown(post_shutdown)

    app = builder.build()
    service.register_handlers(app)

    try:
        from services.scheduler import register_jobs

        register_jobs(app)
    except Exception:
        log.warning("Could not register background scheduler jobs", exc_info=True)

    return app
