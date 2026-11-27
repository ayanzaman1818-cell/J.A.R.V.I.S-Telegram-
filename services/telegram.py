"""Professional Telegram interface for JARVIS with non-blocking execution, vision/document support, and interactive inline buttons."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from typing import Any, Callable, Coroutine

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from database.repository import db

log = logging.getLogger("jarvis.telegram")

# Process-wide health counters surfaced by /health.
HEALTH: dict[str, float] = {
    "started": time.time(),
    "errors": 0,
    "network_errors": 0,
    "last_network_error": 0.0,
}

AgentResponder = Callable[..., Coroutine[Any, Any, str]]
ResetHandler = Callable[[int], Any]
BriefingSender = Callable[[Any, int, Any], Coroutine[Any, Any, None]]
PhoneCaller = Callable[[str], Coroutine[Any, Any, str]]
VoiceTranscriber = Callable[[bytes], Coroutine[Any, Any, str]]
TtsGenerator = Callable[[str], Coroutine[Any, Any, str]]
ComposioChecker = Callable[[], bool]


class TelegramService:
    """Manages Telegram bot commands, text/voice/photo/document handlers, inline callbacks, and authorization."""

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
    def _confirmation_markup(chat_id: int, text: str) -> InlineKeyboardMarkup | None:
        """Attach Approve / Cancel buttons if a pending action token is referenced in the reply."""
        pending = db.get_latest_pending_action(chat_id)
        if not pending or pending.token not in (text or ""):
            return None
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Approve", callback_data=f"act_ok:{pending.token}"
                    ),
                    InlineKeyboardButton(
                        "❌ Cancel", callback_data=f"act_no:{pending.token}"
                    ),
                ]
            ]
        )

    @classmethod
    async def send_long(
        cls,
        bot: Any,
        chat_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        """Split responses longer than 4,000 characters and retry transient Telegram network errors."""
        payload = (text or "").strip() or "(No response generated.)"
        markup = reply_markup or cls._confirmation_markup(chat_id, payload)
        chunks = [payload[i : i + 4000] for i in range(0, len(payload), 4000)]
        for idx, chunk in enumerate(chunks):
            is_last = idx == len(chunks) - 1
            chunk_markup = markup if is_last else None
            for attempt in range(3):
                try:
                    await bot.send_message(
                        chat_id, chunk, reply_markup=chunk_markup
                    )
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
            f"At your service, {config.USER_NAME}. Ask me anything, send a voice note/photo/document/YouTube link, or use:\n"
            "/help - show all available commands\n"
            "/voicemode - toggle two-way Marvel JARVIS voice chat mode\n"
            "/briefing - morning debrief right now\n"
            "/evening - evening reflection & wrap-up\n"
            "/tasks - view & complete open tasks\n"
            "/reminders - view & manage reminders\n"
            "/expenses - view 30-day expense report & visual chart\n"
            "/habits - view habit streaks & visual heatmap\n"
            "/notes - search or view Second Brain knowledge vault\n"
            "/watch - view active price-drop watches\n"
            "/pc - check PC status & quick remote controls\n"
            "/clip - read or copy text to PC clipboard\n"
            "/memory - view saved memories & preferences\n"
            "/status - check system health & model settings\n"
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
            "/voicemode (or /voice) - toggle two-way Telegram voice chat & Marvel JARVIS voice settings\n"
            "/briefing - generate and deliver your morning briefing now\n"
            "/evening - generate and deliver your evening reflection now\n"
            "/tasks - list open personal tasks with quick-complete buttons\n"
            "/plan <goal> - decompose a high-level goal into tasks & Second Brain roadmap\n"
            "/research <topic> - run autonomous multi-query web research & archive report\n"
            "/study [now|quiz|<subject>] (or /learn) - autonomous student mode: self-study & flashcards\n"
            "/automate [when | prompt] (or /jobs) - schedule or manage standing background agent jobs\n"
            "/focus [mins] [task] - start a Pomodoro / deep focus countdown timer\n"
            "/reminders - list active scheduled reminders with cancel buttons\n"
            "/expenses - view 30-day expense breakdown and chart button\n"
            "/habits (or /stats) - view habit streaks and heatmap chart button\n"
            "/notes [query] (or /brain) - list or search your Second Brain knowledge vault\n"
            "/export [dataset] - download expenses, tasks, habits, or notes as CSV/Markdown\n"
            "/watch - list active product price-drop watches\n"
            "/pc (or /pcstatus) - PC hardware status, clipboard, focus modes & remote controls\n"
            "/top - inspect top memory-hungry PC processes with one-tap Kill buttons\n"
            "/download <url> - download any file URL directly to your PC Downloads folder\n"
            "/clip [text] - view PC clipboard or copy <text> to PC clipboard\n"
            "/lock - lock your Windows PC immediately (owner only)\n"
            "/memory (or /memories) - list remembered facts and user preferences\n"
            "/status - display system health, voice engine, models, and database counts\n"
            "/health (or /diag) - live diagnostics: model breaker, latency, voice, backups, errors\n"
            "/backup [send] - back up the database now; 'send' also delivers the file (owner only)\n"
            "/model - inspect active Gemini models and circuit-breaker status\n"
            "/reset (or /clear) - clear current conversation context while keeping long-term memory\n"
            "/call <message> - ring your phone with a spoken message (owner only)\n"
            "/myid - display your numeric Telegram user ID\n\n"
            "Tip: Send any file or photo with caption '/save' to save it directly to your PC Inbox folder."
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

        try:
            from services import voice as voice_svc

            tts_state = voice_svc.tts_engine_status(user_id=user_id)
            v_mode = "ON (two-way spoken replies)" if voice_svc.is_voice_mode_enabled(user_id) else "OFF"
        except Exception:
            tts_state = config.TTS_VOICE
            v_mode = "OFF"

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
                f"- Voice TTS: {tts_state}\n"
                f"- Two-Way Voice Mode: {v_mode}\n"
                f"- Composio: {composio_state}"
            )

    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        if not update.message:
            return
        from brain.gemini import model_health, set_active_model

        if context.args and self._is_owner(update):
            chosen = set_active_model(" ".join(context.args).strip())
            await update.message.reply_text(f"🧠 Switched active JARVIS LLM model to: `{chosen}`")

        try:
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
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🤖 JARVIS Qwen 7B (Custom)", callback_data="model_set:jarvis7b"),
                    InlineKeyboardButton("💻 LM Studio: Active Model", callback_data="model_set:lmstudio"),
                ],
                [
                    InlineKeyboardButton("☁️ Gemma 4 26B", callback_data="model_set:gemma26b"),
                    InlineKeyboardButton("⚡ Gemini 3.5 Lite", callback_data="model_set:flashlite"),
                ],
                [
                    InlineKeyboardButton("☁️ Gemini 3.8 Flash", callback_data="model_set:flash38"),
                    InlineKeyboardButton("☁️ Gemma 4 31B", callback_data="model_set:gemma31b"),
                ],
                [
                    InlineKeyboardButton("⚡ LM Studio: Qwen3 8B", callback_data="model_set:qwen38b"),
                    InlineKeyboardButton("🌿 LM Studio: Bonsai 27B", callback_data="model_set:bonsai27b"),
                ],
                [
                    InlineKeyboardButton("💻 LM Studio: Qwen 2.5 7B", callback_data="model_set:qwen7b"),
                ],
            ]
        )
        await update.message.reply_text(
            "🧠 **JARVIS LLM Model Switcher**\n"
            f"- Active Primary: `{health['primary']}` ({state})\n"
            f"- Fast Model: `{health['fast']}`\n"
            f"- Deep Model: `{health['deep']}`\n"
            f"- Cloud Fallback: `{health['fallback']}`\n"
            f"- LM Studio Server: `{health.get('local_base_url', 'http://127.0.0.1:1234/v1')}`\n\n"
            "Tap a preset below to switch models live, or send `/model lmstudio:<model_name>`:",
            reply_markup=markup,
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
        buttons = [
            [
                InlineKeyboardButton(
                    f"✅ Complete #{t['id']}: {t['title'][:28]}",
                    callback_data=f"task_done:{t['id']}",
                )
            ]
            for t in tasks[:8]
        ]
        markup = InlineKeyboardMarkup(buttons) if buttons else None
        if update.message:
            await update.message.reply_text(text, reply_markup=markup)

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
        buttons = [
            [
                InlineKeyboardButton(
                    f"❌ Cancel #{r['id']}: {r['message'][:28]}",
                    callback_data=f"rem_cancel:{r['id']}",
                )
            ]
            for r in reminders[:8]
        ]
        markup = InlineKeyboardMarkup(buttons) if buttons else None
        if update.message:
            await update.message.reply_text(text, reply_markup=markup)

    async def cmd_expenses(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update) or not update.message:
            return
        from tools.tracker import expense_report

        ctx = self._make_ctx(context, update.effective_chat.id, update.effective_user.id)
        text = await expense_report(ctx, days=30)
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("📊 Generate Expense Chart", callback_data="chart:expense")]]
        )
        await update.message.reply_text(text, reply_markup=markup)

    async def cmd_habits(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update) or not update.message:
            return
        from tools.tracker import expense_report, habit_report

        ctx = self._make_ctx(context, update.effective_chat.id, update.effective_user.id)
        h_text = await habit_report(ctx, days=7)
        e_text = await expense_report(ctx, days=7)
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔥 Habit Streak Chart", callback_data="chart:habit"),
                    InlineKeyboardButton("📊 Expense Chart", callback_data="chart:expense"),
                ]
            ]
        )
        await update.message.reply_text(f"{h_text}\n\n{e_text}", reply_markup=markup)

    async def cmd_pc(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            if update.message:
                await update.message.reply_text("Only the bot owner can access PC controls.")
            return
        if not update.message:
            return
        from tools.pc_control import pc_status

        ctx = self._make_ctx(context, update.effective_chat.id, update.effective_user.id)
        status_text = await pc_status(ctx)
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("📸 Smart Screenshot", callback_data="pc_act:screenshot"),
                    InlineKeyboardButton("👁 AI Screen Inspect", callback_data="pc_act:inspect"),
                ],
                [
                    InlineKeyboardButton("🪟 Open Windows", callback_data="pc_act:windows"),
                    InlineKeyboardButton("⚙️ Top Processes", callback_data="pc_act:top"),
                ],
                [
                    InlineKeyboardButton("⏯ Play/Pause", callback_data="pc_act:play_pause"),
                    InlineKeyboardButton("🔇 Mute", callback_data="pc_act:mute"),
                ],
                [
                    InlineKeyboardButton("☕ Toggle Keep-Awake", callback_data="pc_act:caffeine"),
                    InlineKeyboardButton("🖥 Display Off", callback_data="pc_act:display_off"),
                ],
                [
                    InlineKeyboardButton("📋 Read Clipboard", callback_data="pc_act:clipboard"),
                    InlineKeyboardButton("🗂 Tidy Downloads", callback_data="pc_act:organize"),
                ],
                [
                    InlineKeyboardButton("🌐 Network Info", callback_data="pc_act:network"),
                    InlineKeyboardButton("🔒 Lock PC", callback_data="pc_act:lock"),
                ],
                [
                    InlineKeyboardButton("💼 Work Mode", callback_data="pc_act:work"),
                    InlineKeyboardButton("🌙 Night Mode", callback_data="pc_act:night"),
                ],
            ]
        )
        await update.message.reply_text(status_text, reply_markup=markup)

    async def cmd_lock(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            if update.message:
                await update.message.reply_text("Only the bot owner can lock the PC.")
            return
        from tools.pc_control import pc_lock

        ctx = self._make_ctx(context, update.effective_chat.id, update.effective_user.id)
        res = await pc_lock(ctx)
        if update.message:
            await update.message.reply_text(res)

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

    async def cmd_evening(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        chat_id = update.effective_chat.id
        from services.briefing import send_evening_briefing

        await context.bot.send_chat_action(chat_id, "typing")
        await send_evening_briefing(context.bot, chat_id, context.job_queue)

    async def cmd_voicemode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update) or not update.message:
            return
        user_id = update.effective_user.id
        from services import voice as voice_svc

        args = [a.lower() for a in (context.args or [])]
        if args:
            if args[0] in ("on", "true", "1", "enable"):
                voice_svc.set_voice_mode(True, user_id=user_id)
            elif args[0] in ("off", "false", "0", "disable"):
                voice_svc.set_voice_mode(False, user_id=user_id)
            elif args[0] in voice_svc.VOICE_PRESETS:
                voice_svc.set_user_tts_voice(args[0], user_id=user_id)

        active = voice_svc.is_voice_mode_enabled(user_id=user_id)
        engine = voice_svc.tts_engine_status(user_id=user_id)
        state_str = "ON (two-way spoken replies active)" if active else "OFF (voice replies only when you send audio)"
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🎙 Enable 2-Way Voice", callback_data="voice_mode:on"),
                    InlineKeyboardButton("🔇 Disable 2-Way Voice", callback_data="voice_mode:off"),
                ],
                [
                    InlineKeyboardButton("🎩 Marvel JARVIS Voice", callback_data="voice_set:jarvis"),
                    InlineKeyboardButton("🇬🇧 Sonia (UK)", callback_data="voice_set:sonia"),
                ],
                [
                    InlineKeyboardButton("🇺🇸 Christopher (US)", callback_data="voice_set:christopher"),
                    InlineKeyboardButton("🔊 Test TTS Voice", callback_data="voice_test:jarvis"),
                ],
                [
                    InlineKeyboardButton("🎬 MCU Greeting", callback_data="jarvis_clip:session.start"),
                    InlineKeyboardButton("🎬 MCU Complete", callback_data="jarvis_clip:task.complete"),
                    InlineKeyboardButton("🎬 MCU Dry Wit", callback_data="jarvis_clip:task.error"),
                ],
            ]
        )
        await update.message.reply_text(
            f"🎙 Two-Way Voice Chat Mode: {state_str}\n"
            f"Voice Engine: {engine}\n\n"
            "Send any voice note to talk with me, or enable 2-Way Voice so I speak every response aloud.",
            reply_markup=markup,
        )

    async def cmd_clip(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update) or not update.message:
            if update.message:
                await update.message.reply_text("Only the bot owner can access the PC clipboard.")
            return
        from tools.pc_control import pc_clipboard_get, pc_clipboard_set

        ctx = self._make_ctx(context, update.effective_chat.id, update.effective_user.id)
        text_arg = " ".join(context.args or []).strip()
        if text_arg:
            res = await pc_clipboard_set(ctx, text=text_arg)
        else:
            res = await pc_clipboard_get(ctx)
        await update.message.reply_text(res)

    async def cmd_notes(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update) or not update.message:
            return
        from tools.knowledge import list_notes, search_knowledge

        ctx = self._make_ctx(context, update.effective_chat.id, update.effective_user.id)
        query = " ".join(context.args or []).strip()
        res = await (search_knowledge(ctx, query=query) if query else list_notes(ctx))
        await self.send_long(context.bot, update.effective_chat.id, res)

    async def cmd_watch(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update) or not update.message:
            return
        from tools.watchdog import list_price_watches

        ctx = self._make_ctx(context, update.effective_chat.id, update.effective_user.id)
        res = await list_price_watches(ctx)
        await self.send_long(context.bot, update.effective_chat.id, res)

    async def cmd_call(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            if update.message:
                await update.message.reply_text("Only the bot owner can place phone calls.")
            return
        text = " ".join(context.args or []) or f"At your service, {config.USER_NAME}. How may I assist you?"

        # 1. Preferred: Real Native 1-on-1 Telegram VoIP Call (if jarvis_call_userbot.session is logged in)
        from services.telegram_call import is_native_call_configured, place_native_telegram_call

        if is_native_call_configured():
            try:
                status_msg = await place_native_telegram_call(
                    target_user_id=update.effective_user.id,
                    greeting=text,
                )
                if update.message:
                    await update.message.reply_text(
                        f"📞 **Ringing your Telegram app now!**\n{status_msg}\n\n"
                        "Pick up the incoming Telegram call to talk back and forth with JARVIS."
                    )
                return
            except Exception as exc:
                log.warning("Native Telegram VoIP call failed, falling back: %s", exc)

        # 2. Fallback: Free Interactive Web Voice Call link + instructions for Native Telegram Call setup
        from services.twilio_webhook import get_web_call_url, start_twilio_webhook_server

        await start_twilio_webhook_server(bot=context.bot, job_queue=context.job_queue)
        call_url = get_web_call_url(greeting=text)

        twilio_note = ""
        if self.phone_call_fn:
            from services.voice import calls_enabled

            if calls_enabled():
                try:
                    await self.phone_call_fn(text)
                    twilio_note = "\n\n📲 Also ringing your phone via Twilio."
                except Exception as exc:
                    twilio_note = f"\n\n(Twilio carrier call skipped: {exc})"

        if update.message:
            is_https = call_url.startswith("https://")
            markup = (
                InlineKeyboardMarkup(
                    [[InlineKeyboardButton("📞 Answer Live Voice Call with JARVIS", url=call_url)]]
                )
                if is_https
                else None
            )
            await update.message.reply_text(
                f"📞 **Incoming Live Voice Call from J.A.R.V.I.S.**\n\n"
                f"{call_url}{twilio_note}\n\n"
                f"💡 *Want a real native Telegram VoIP Call that rings your Telegram app directly?*\n"
                f"Run `py setup_telegram_call.py` once in your terminal and log in with JARVIS's secondary Telegram account!",
                reply_markup=markup,
            )

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query:
            return
        if not self._is_authorized(update):
            await query.answer("Unauthorized.", show_alert=True)
            return

        data = query.data or ""
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        ctx = self._make_ctx(context, chat_id, user_id)

        try:
            if data.startswith("task_done:"):
                task_id = int(data.split(":", 1)[1])
                if db.complete_task(task_id, user_id=user_id):
                    await query.answer(f"Task #{task_id} completed!")
                    await query.message.reply_text(f"✅ Task #{task_id} marked complete.")
                else:
                    await query.answer("Task already completed or not found.")
                return

            if data.startswith("rem_cancel:"):
                rem_id = int(data.split(":", 1)[1])
                if db.cancel_reminder(rem_id, user_id=user_id):
                    await query.answer(f"Reminder #{rem_id} cancelled!")
                    await query.message.reply_text(f"❌ Reminder #{rem_id} cancelled.")
                else:
                    await query.answer("Reminder already inactive.")
                return

            if data.startswith("rem_done:"):
                rem_id = int(data.split(":", 1)[1])
                db.cancel_reminder(rem_id, user_id=user_id)
                await query.answer("Marked done!")
                await query.edit_message_reply_markup(reply_markup=None)
                return

            if data.startswith("rem_snooze:"):
                parts = data.split(":")
                rem_id = int(parts[1])
                mins = int(parts[2]) if len(parts) > 2 else 10
                new_due = db.snooze_reminder(rem_id, minutes=mins, user_id=user_id)
                if new_due:
                    await query.answer(f"Snoozed for {mins} minutes!")
                    await query.edit_message_reply_markup(reply_markup=None)
                    await query.message.reply_text(
                        f"💤 Snoozed reminder #{rem_id} for {mins} minutes."
                    )
                else:
                    await query.answer("Could not snooze reminder.")
                return

            if data.startswith("act_ok:"):
                token = data.split(":", 1)[1]
                from tools.integrations import confirm_action

                await query.answer("Executing confirmed action...")
                await query.edit_message_reply_markup(reply_markup=None)
                res = await confirm_action(ctx, token=token)
                await self.send_long(context.bot, chat_id, f"✅ Confirmed:\n{res}")
                return

            if data.startswith("act_no:"):
                db.clear_pending_actions(chat_id)
                await query.answer("Action cancelled.")
                await query.edit_message_reply_markup(reply_markup=None)
                await query.message.reply_text("❌ Pending action cancelled.")
                return

            if data.startswith("pc_act:"):
                if not self._is_owner(update):
                    await query.answer("Owner only.", show_alert=True)
                    return
                act = data.split(":", 1)[1]
                from tools import pc_control as pc_mod
                from tools.pc_control import (
                    pc_clipboard_get,
                    pc_focus_mode,
                    pc_inspect_window,
                    pc_list_windows,
                    pc_lock,
                    pc_media_control,
                    pc_network_info,
                    pc_organize_downloads,
                    pc_power_control,
                    pc_screenshot,
                    pc_stay_awake,
                    pc_top_processes,
                )

                await query.answer(f"Running {act}...")
                if act == "screenshot":
                    await pc_screenshot(ctx, hide_telegram=True)
                elif act == "inspect":
                    res = await pc_inspect_window(ctx)
                    await self.send_long(context.bot, chat_id, res)
                elif act == "windows":
                    res = await pc_list_windows(ctx)
                    await query.message.reply_text(res)
                elif act == "top":
                    res = await pc_top_processes(ctx, limit=8)
                    await query.message.reply_text(res)
                elif act == "caffeine":
                    res = await pc_stay_awake(ctx, enabled=not pc_mod._CAFFEINE_ACTIVE)
                    await query.message.reply_text(res)
                elif act == "display_off":
                    res = await pc_power_control(ctx, action="display_off")
                    await query.message.reply_text(res)
                elif act == "organize":
                    res = await pc_organize_downloads(ctx, folder="downloads")
                    await query.message.reply_text(res)
                elif act == "network":
                    res = await pc_network_info(ctx)
                    await query.message.reply_text(res)
                elif act == "lock":
                    res = await pc_lock(ctx)
                    await query.message.reply_text(res)
                elif act in ("play_pause", "mute"):
                    res = await pc_media_control(ctx, action=act)
                    await query.message.reply_text(res)
                elif act == "clipboard":
                    res = await pc_clipboard_get(ctx)
                    await query.message.reply_text(res)
                elif act in ("work", "night", "chill", "meeting"):
                    res = await pc_focus_mode(ctx, mode=act)
                    await query.message.reply_text(res)
                return

            if data.startswith("pc_kill:"):
                if not self._is_owner(update):
                    await query.answer("Owner only.", show_alert=True)
                    return
                pid_str = data.split(":", 1)[1]
                from tools.pc_control import pc_kill_process

                await query.answer(f"Terminating PID {pid_str}...")
                res = await pc_kill_process(ctx, target=pid_str)
                await query.message.reply_text(res)
                return

            if data.startswith("chart:"):
                kind = data.split(":", 1)[1]
                from tools.charts import expense_chart, habit_chart

                await query.answer("Generating visual chart...")
                if kind == "expense":
                    await expense_chart(ctx, days=30)
                elif kind == "habit":
                    await habit_chart(ctx, days=14)
                return

            if data.startswith("model_set:"):
                if not self._is_owner(update):
                    await query.answer("Owner only.", show_alert=True)
                    return
                preset = data.split(":", 1)[1]
                from brain.gemini import set_active_model

                chosen = set_active_model(preset)
                await query.answer(f"Model set to {chosen}")
                await query.message.reply_text(f"🧠 Active JARVIS LLM model switched to: `{chosen}`")
                return

            if data.startswith("voice_mode:"):
                mode_val = data.split(":", 1)[1] == "on"
                from services import voice as voice_svc

                voice_svc.set_voice_mode(mode_val, user_id=user_id)
                state_txt = "ON (two-way spoken replies)" if mode_val else "OFF"
                await query.answer(f"2-Way Voice Mode: {state_txt}")
                await query.message.reply_text(f"🎙 Two-Way Voice Chat Mode is now {state_txt}.")
                return

            if data.startswith("voice_set:"):
                preset = data.split(":", 1)[1]
                from services import voice as voice_svc

                chosen = voice_svc.set_user_tts_voice(preset, user_id=user_id)
                await query.answer(f"Voice set to {chosen}")
                await query.message.reply_text(
                    f"🎩 Voice updated to {preset.upper()} ({chosen})."
                )
                return

            if data.startswith("voice_test:"):
                await query.answer("Synthesizing Marvel JARVIS voice sample...")
                sample_line = (
                    f"At your service, {config.USER_NAME}. All systems are online and operating at peak efficiency."
                )
                await self._send_tts_reply(context.bot, chat_id, sample_line, user_id=user_id)
                return

            if data.startswith("jarvis_clip:"):
                cat = data.split(":", 1)[1]
                from tools.comms import jarvis_voice_clip

                await query.answer("Playing MCU JARVIS voice clip...")
                await jarvis_voice_clip(ctx, category=cat)
                return

            if data.startswith("export:"):
                dataset = data.split(":", 1)[1]
                from tools.productivity import export_data

                await query.answer(f"Exporting {dataset}...")
                await export_data(ctx, dataset=dataset, days=90)
                return

            if data.startswith("focus:"):
                mins = int(data.split(":", 1)[1])
                from tools.productivity import start_focus_timer

                label = "Short break" if mins <= 10 else "Deep focus session"
                await query.answer(f"Starting {mins}m timer...")
                msg = await start_focus_timer(ctx, minutes=mins, label=label)
                await query.message.reply_text(msg)
                return

            if data.startswith("study_now:"):
                arg = data.split(":", 1)[1]
                from tools.student import run_study_session

                await query.answer("Starting autonomous study session...")
                await query.message.reply_text("🎓 Sitting down to study on the web and take notes...")
                tid = int(arg) if arg.isdigit() and int(arg) > 0 else None
                report = await run_study_session(ctx, topic_id=tid, auto_discover=True)
                await self.send_long(context.bot, chat_id, report)
                return

            if data.startswith("study_quiz:"):
                subj = data.split(":", 1)[1]
                from tools.student import quiz_from_study_notes

                await query.answer("Preparing flashcard review...")
                quiz = await quiz_from_study_notes(ctx, subject="" if subj == "all" else subj)
                await self.send_long(context.bot, chat_id, quiz)
                return

            if data.startswith("study_toggle:"):
                tid = int(data.split(":", 1)[1])
                topic = db.get_study_topic(tid, user_id=user_id)
                if topic:
                    new_st = "paused" if topic["status"] == "active" else "active"
                    db.set_study_topic_status(tid, new_st, user_id=user_id)
                    await query.answer(f"Topic #{tid} {new_st}")
                    await query.message.reply_text(
                        f"🎓 Study topic #{tid} ('{topic['subject']}') is now {new_st}."
                    )
                return

            await query.answer()
        except Exception:
            log.exception("Callback handler failed: data=%s", data)
            await query.answer("Action failed.", show_alert=True)

    async def _send_tts_reply(
        self, bot: Any, chat_id: int, text: str, user_id: int | None = None
    ) -> None:
        """Synthesize and send a spoken voice note in Telegram."""
        if not self.tts_fn:
            return
        try:
            try:
                await bot.send_chat_action(chat_id, "record_voice")
            except Exception:
                pass
            sig = inspect.signature(self.tts_fn)
            if "user_id" in sig.parameters:
                path = await self.tts_fn(text, user_id=user_id)
            else:
                path = await self.tts_fn(text)
            try:
                with open(path, "rb") as f:
                    await bot.send_voice(chat_id, f)
            finally:
                if os.path.exists(path):
                    os.unlink(path)
        except Exception:
            log.exception("Voice reply TTS failed")

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

            # If Two-Way Voice Chat Mode is active, also speak the response aloud
            try:
                from services import voice as voice_svc

                if voice_svc.is_voice_mode_enabled(user_id):
                    await self._send_tts_reply(context.bot, chat_id, reply, user_id=user_id)
            except Exception:
                log.debug("Two-way voice check skipped", exc_info=True)
        except Exception:
            log.exception(
                "Text handler failed after %.2fs",
                time.perf_counter() - message_started,
            )
            await update.message.reply_text(
                "Something went wrong while processing your message. Please try again."
            )

    async def handle_media(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Process incoming photos, screenshots, receipts, PDFs, text documents, and '/save' PC inbox transfers."""
        if not self._is_authorized(update) or not update.message:
            return
        if not self.respond_fn:
            await update.message.reply_text("Jarvis brain is still initializing.")
            return

        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        caption = (update.message.caption or "").strip()

        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(
            self.keep_typing(context.bot, chat_id, stop_typing)
        )
        try:
            media_bytes: bytes | None = None
            mime_type: str | None = None
            prompt = caption
            save_filename: str | None = None

            if update.message.photo:
                photo = update.message.photo[-1]
                tg_file = await photo.get_file()
                media_bytes = bytes(await tg_file.download_as_bytearray())
                mime_type = "image/jpeg"
                save_filename = f"photo_{update.message.message_id}.jpg"
                if not prompt:
                    prompt = (
                        "Analyze this image thoroughly. If it's a receipt or bill, summarize the items "
                        "and total. If it contains text, notes, or a problem, extract and explain it."
                    )
            elif update.message.document:
                doc = update.message.document
                if doc.file_size and doc.file_size > 15 * 1024 * 1024:
                    await update.message.reply_text("That file is too large (max 15 MB).")
                    return
                tg_file = await doc.get_file()
                raw_bytes = bytes(await tg_file.download_as_bytearray())
                fname = (doc.file_name or f"file_{update.message.message_id}").strip()
                save_filename = os.path.basename(fname)
                fname_lower = fname.lower()
                doc_mime = (doc.mime_type or "application/octet-stream").lower()

                if caption.lower().startswith("/save") and self._is_owner(update):
                    config.INBOX_DIR.mkdir(parents=True, exist_ok=True)
                    dest = config.INBOX_DIR / save_filename
                    dest.write_bytes(raw_bytes)
                    await update.message.reply_text(
                        f"💾 Saved '{save_filename}' ({len(raw_bytes) / 1024:.1f} KB) to PC Inbox:\n{dest}"
                    )
                    return

                if doc_mime.startswith("text/") or fname_lower.endswith(
                    (".txt", ".md", ".py", ".json", ".csv", ".log", ".yaml", ".yml")
                ):
                    decoded = raw_bytes.decode("utf-8", errors="replace")[:12000]
                    user_instruction = caption or "Summarize and analyze this document."
                    prompt = f"{user_instruction}\n\nDocument ({doc.file_name}):\n{decoded}"
                elif doc_mime.startswith("image/") or doc_mime == "application/pdf":
                    media_bytes = raw_bytes
                    mime_type = doc_mime
                    if not prompt:
                        prompt = f"Analyze and summarize this attached file ({doc.file_name})."
                else:
                    await update.message.reply_text(
                        f"Unsupported file type ({doc_mime}). Send photos, PDFs, or text/code files (or use caption '/save' to save any file to PC)."
                    )
                    return
            else:
                return

            if caption.lower().startswith("/save") and media_bytes and save_filename and self._is_owner(update):
                config.INBOX_DIR.mkdir(parents=True, exist_ok=True)
                dest = config.INBOX_DIR / save_filename
                dest.write_bytes(media_bytes)
                await update.message.reply_text(
                    f"💾 Saved '{save_filename}' ({len(media_bytes) / 1024:.1f} KB) to PC Inbox:\n{dest}"
                )
                return

            ctx = self._make_ctx(context, chat_id, user_id)
            sig = inspect.signature(self.respond_fn)
            if "media_bytes" in sig.parameters:
                reply = await self.respond_fn(
                    ctx, prompt, media_bytes=media_bytes, mime_type=mime_type
                )
            else:
                reply = await self.respond_fn(ctx, prompt)
            await self.send_long(context.bot, chat_id, reply)
        except Exception:
            log.exception("Media handler failed")
            await update.message.reply_text(
                "Couldn't process that file or image right now. Please try again."
            )
        finally:
            stop_typing.set()
            typing_task.cancel()

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
            await update.message.reply_text(f"🎙 Heard: {text}")
            if not self.respond_fn:
                return
            ctx = self._make_ctx(context, chat_id, user_id)
            reply = await self.respond_fn(ctx, text)
        finally:
            stop_typing.set()
            typing_task.cancel()

        await self.send_long(context.bot, chat_id, reply)
        await self._send_tts_reply(context.bot, chat_id, reply, user_id=user_id)

    @staticmethod
    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log transient network blips as one-line warnings; keep tracebacks for real bugs."""
        from telegram.error import Conflict, NetworkError, RetryAfter, TimedOut

        err = context.error
        HEALTH["errors"] += 1
        if isinstance(err, Conflict):
            log.error(
                "Telegram polling conflict: another JARVIS instance is using this bot token. "
                "Stop the duplicate process."
            )
        elif isinstance(err, RetryAfter):
            log.warning("Telegram flood control: retry after %ss", err.retry_after)
        elif isinstance(err, (TimedOut, NetworkError)):
            HEALTH["network_errors"] += 1
            HEALTH["last_network_error"] = time.time()
            log.warning("Telegram network issue (will retry automatically): %s", err)
        else:
            log.error("Unhandled Telegram error", exc_info=err)

    async def cmd_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Live diagnostics: uptime, model breaker state & latency, voice, backups, errors."""
        if not self._is_authorized(update) or not update.message:
            return
        up = int(time.time() - HEALTH["started"])
        lines = [f"🩺 JARVIS health — up {up // 86400}d {up % 86400 // 3600}h {up % 3600 // 60}m"]
        try:
            from brain.gemini import model_health

            h = model_health()
            if h["primary_paused_seconds"]:
                state = f"paused {h['primary_paused_seconds']}s (failures: {h['primary_failures']})"
            elif h["primary_failures"]:
                state = "probing for recovery" if h["probing"] else "awaiting probe"
            else:
                state = "healthy"
            lines.append(f"- Primary {h['primary']}: {state}")
            lines.append(f"- Fallback {h['fallback']}")
            for model, s in h["stats"].items():
                med = f"{s['median_s']}s" if s["median_s"] is not None else "n/a"
                err = f", last error {s['last_error']}" if s["fail"] and s["last_error"] else ""
                lines.append(f"  · {model}: {s['ok']} ok / {s['fail']} failed, median {med}{err}")
        except Exception:
            lines.append("- Gemini stats unavailable")
        try:
            from services import voice as voice_svc

            lines.append(f"- Fish Audio voice: {voice_svc.fish_status()}")
        except Exception:
            pass
        try:
            from services.backup import backup_summary

            lines.append(f"- Backups: {backup_summary()}")
        except Exception:
            pass
        try:
            size_kb = config.DB_PATH.stat().st_size / 1024
            lines.append(f"- Database: {size_kb:.0f} KB")
        except OSError:
            pass
        net = HEALTH["network_errors"]
        last = HEALTH["last_network_error"]
        ago = f", last {int((time.time() - last) // 60)}m ago" if last else ""
        lines.append(f"- Telegram network blips: {net}{ago}; other errors: {HEALTH['errors'] - net}")
        await update.message.reply_text("\n".join(lines))

    async def cmd_backup(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Back up the SQLite database now; '/backup send' also delivers the file here."""
        if not self._is_owner(update) or not update.message:
            return
        from services.backup import backup_database

        try:
            path = await asyncio.to_thread(backup_database)
        except Exception as exc:
            log.exception("Manual backup failed")
            await update.message.reply_text(f"Backup failed: {type(exc).__name__}")
            return
        size_kb = path.stat().st_size / 1024
        await update.message.reply_text(f"💾 Backup saved: {path.name} ({size_kb:.0f} KB)")
        if context.args and context.args[0].lower() == "send":
            with open(path, "rb") as f:
                await update.message.reply_document(
                    f, filename=path.name, caption="Private JARVIS database backup. Keep it safe."
                )

    async def cmd_focus(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Start a Pomodoro / deep focus countdown timer (/focus [minutes] [label])."""
        if not self._is_authorized(update) or not update.message:
            return
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        ctx = self._make_ctx(context, chat_id, user_id)

        if not context.args:
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("🎯 25m Pomodoro", callback_data="focus:25"),
                        InlineKeyboardButton("🔥 50m Deep Work", callback_data="focus:50"),
                        InlineKeyboardButton("☕ 5m Break", callback_data="focus:5"),
                    ]
                ]
            )
            await update.message.reply_text(
                "🎯 Select a focus timer preset, or send `/focus <minutes> [task]` (e.g. `/focus 25 API refactor`):",
                reply_markup=markup,
            )
            return

        from tools.productivity import start_focus_timer

        args = list(context.args)
        if args[0].isdigit():
            minutes = int(args[0])
            label = " ".join(args[1:]).strip() or "Deep focus session"
        else:
            minutes = 25
            label = " ".join(args).strip()

        msg = await start_focus_timer(ctx, minutes=minutes, label=label)
        await update.message.reply_text(msg)

    async def cmd_export(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Export expenses, tasks, habits, or Second Brain notes as downloadable CSV/Markdown."""
        if not self._is_authorized(update) or not update.message:
            return
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        ctx = self._make_ctx(context, chat_id, user_id)

        valid = ("expenses", "tasks", "habits", "notes")
        if context.args and context.args[0].lower().rstrip("s") + "s" in valid:
            raw = context.args[0].lower()
            dataset = raw if raw in valid else raw.rstrip("s") + "s"
            days = int(context.args[1]) if len(context.args) > 1 and context.args[1].isdigit() else 90
            from tools.productivity import export_data

            await export_data(ctx, dataset=dataset, days=days)
            return

        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("💸 Expenses (CSV)", callback_data="export:expenses"),
                    InlineKeyboardButton("✅ Tasks (CSV)", callback_data="export:tasks"),
                ],
                [
                    InlineKeyboardButton("🔥 Habits (CSV)", callback_data="export:habits"),
                    InlineKeyboardButton("🧠 Notes (Markdown)", callback_data="export:notes"),
                ],
            ]
        )
        await update.message.reply_text(
            "📦 Choose a dataset to export as a file, or use `/export <expenses|tasks|habits|notes> [days]`:",
            reply_markup=markup,
        )

    async def cmd_research(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Run an autonomous multi-source deep research workflow (/research <topic>)."""
        if not self._is_authorized(update) or not update.message:
            return
        topic = " ".join(context.args or []).strip()
        if not topic:
            await update.message.reply_text(
                "🔬 Usage: `/research <topic or question>`\n"
                "Example: `/research Rust vs Go for high-throughput microservices`"
            )
            return

        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        ctx = self._make_ctx(context, chat_id, user_id)
        await update.message.reply_text(f"🔬 Launching autonomous deep research on: '{topic}'...")

        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(self.keep_typing(context.bot, chat_id, stop_typing))
        try:
            from tools.agentic import deep_research

            report = await deep_research(ctx, topic=topic, save_to_vault=True)
        finally:
            stop_typing.set()
            typing_task.cancel()

        await self.send_long(context.bot, chat_id, report)

    async def cmd_plan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Autonomously decompose a goal into actionable tasks & roadmap (/plan <goal>)."""
        if not self._is_authorized(update) or not update.message:
            return
        goal = " ".join(context.args or []).strip()
        if not goal:
            await update.message.reply_text(
                "🗺 Usage: `/plan <goal or project>`\n"
                "Example: `/plan Launch my portfolio website by Friday`"
            )
            return

        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        ctx = self._make_ctx(context, chat_id, user_id)
        from tools.agentic import plan_goal

        result = await plan_goal(ctx, goal=goal)
        await self.send_long(context.bot, chat_id, result)

    async def cmd_automate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List or schedule autonomous background agent jobs (/automate [when | instruction])."""
        if not self._is_authorized(update) or not update.message:
            return
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        ctx = self._make_ctx(context, chat_id, user_id)

        raw = " ".join(context.args or []).strip()
        if not raw:
            from tools.agentic import AGENT_JOB_PREFIX

            items = [
                r
                for r in db.upcoming_reminders(limit=20, user_id=user_id)
                if str(r.get("message", "")).startswith(AGENT_JOB_PREFIX)
            ]
            if not items:
                await update.message.reply_text(
                    "🤖 No active autonomous agent jobs.\n\n"
                    "Schedule one with:\n"
                    "`/automate <when> | <instruction>`\n"
                    "Examples:\n"
                    "- `/automate in 30 minutes | Check my PC CPU and RAM usage`\n"
                    "- `/automate daily 09:00 | Check top Hacker News AI stories and summarize`\n"
                    "- `/automate weekdays 08:30 | Summarize my high-priority tasks and weather`"
                )
                return

            lines = ["🤖 Active Autonomous Agent Jobs:"]
            buttons = []
            for r in items[:10]:
                instr = str(r["message"])[len(AGENT_JOB_PREFIX) :]
                rep = f" [{r['recurrence']}]" if r.get("recurrence") else ""
                lines.append(f"#{r['id']} ({r['due_at']}){rep}: {instr}")
                buttons.append(
                    [
                        InlineKeyboardButton(
                            f"❌ Cancel Job #{r['id']}",
                            callback_data=f"rem_cancel:{r['id']}",
                        )
                    ]
                )
            await update.message.reply_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
            )
            return

        from tools.agentic import schedule_agent_job

        recurrence = ""
        if "|" in raw:
            when_part, instr_part = (p.strip() for p in raw.split("|", 1))
        else:
            parts = raw.split(None, 1)
            if len(parts) < 2:
                await update.message.reply_text(
                    "Usage: `/automate <when> | <instruction>` (e.g. `/automate in 20m | Check PC status`)"
                )
                return
            when_part, instr_part = parts[0], parts[1]

        first_tok = when_part.split()[0].lower() if when_part.split() else ""
        if first_tok in ("daily", "weekdays", "weekly", "monthly"):
            recurrence = first_tok
            when_part = when_part[len(first_tok) :].strip() or "09:00"

        res = await schedule_agent_job(
            ctx, when=when_part, instruction=instr_part, recurrence=recurrence
        )
        await update.message.reply_text(res)

    async def cmd_study(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Manage JARVIS's Autonomous Student & Self-Learning Engine (/study [now|quiz|<subject>])."""
        if not self._is_authorized(update) or not update.message:
            return
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        ctx = self._make_ctx(context, chat_id, user_id)
        arg = " ".join(context.args or []).strip()

        from tools.student import (
            enroll_study_topic,
            list_study_curriculum,
            quiz_from_study_notes,
            run_study_session,
        )

        if not arg:
            text = await list_study_curriculum(ctx)
            topics = db.list_study_topics(status="all", user_id=user_id)
            buttons = [
                [
                    InlineKeyboardButton(
                        "📖 Study Next Lesson Now", callback_data="study_now:0"
                    ),
                    InlineKeyboardButton(
                        "🧠 Flashcard Review", callback_data="study_quiz:all"
                    ),
                ]
            ]
            for t in topics[:4]:
                toggle_lbl = "⏸ Pause" if t["status"] == "active" else "▶️ Resume"
                buttons.append(
                    [
                        InlineKeyboardButton(
                            f"📖 Study #{t['id']} ({t['subject'][:18]})",
                            callback_data=f"study_now:{t['id']}",
                        ),
                        InlineKeyboardButton(
                            f"{toggle_lbl} #{t['id']}",
                            callback_data=f"study_toggle:{t['id']}",
                        ),
                    ]
                )
            await update.message.reply_text(
                f"{text}\n\n"
                "Commands:\n"
                "- `/study now` — study a lesson right now (or self-discover a new topic)\n"
                "- `/study <subject>` — enroll me in a subject & study Lesson #1\n"
                "- `/study quiz [subject]` — review flashcards from my study notes",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return

        low = arg.lower()
        if low == "now":
            await update.message.reply_text("🎓 Studying next lesson on the web and writing notes...")
            stop_typing = asyncio.Event()
            typing_task = asyncio.create_task(self.keep_typing(context.bot, chat_id, stop_typing))
            try:
                report = await run_study_session(ctx, auto_discover=True)
            finally:
                stop_typing.set()
                typing_task.cancel()
            await self.send_long(context.bot, chat_id, report)
            return

        if low.startswith("quiz"):
            subj = arg[4:].strip()
            quiz = await quiz_from_study_notes(ctx, subject=subj)
            await self.send_long(context.bot, chat_id, quiz)
            return

        await update.message.reply_text(f"🎓 Enrolling in '{arg}' and starting Lesson #1...")
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(self.keep_typing(context.bot, chat_id, stop_typing))
        try:
            report = await enroll_study_topic(ctx, subject=arg, study_now=True)
        finally:
            stop_typing.set()
            typing_task.cancel()
        await self.send_long(context.bot, chat_id, report)

    async def cmd_top(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List top memory-consuming PC processes with one-tap Kill buttons (/top)."""
        if not self._is_owner(update) or not update.message:
            if update.message:
                await update.message.reply_text("Only the bot owner can manage PC processes.")
            return
        from tools.pc_control import get_top_processes_list, pc_top_processes

        ctx = self._make_ctx(context, update.effective_chat.id, update.effective_user.id)
        text = await pc_top_processes(ctx, limit=8)
        procs = await asyncio.to_thread(get_top_processes_list, 6)
        my_pid = os.getpid()
        buttons = []
        for p in procs:
            if p["pid"] not in (0, 4, my_pid) and p["name"].lower() not in ("explorer", "svchost", "system", "lsass", "csrss"):
                buttons.append(
                    [
                        InlineKeyboardButton(
                            f"🛑 Kill {p['name'][:18]} (PID {p['pid']} · {p['mem_mb']:.0f}MB)",
                            callback_data=f"pc_kill:{p['pid']}",
                        )
                    ]
                )
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
        )

    async def cmd_download(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Download a public file URL directly to the PC Downloads folder (/download <url>)."""
        if not self._is_owner(update) or not update.message:
            if update.message:
                await update.message.reply_text("Only the bot owner can download files to the PC.")
            return
        url = " ".join(context.args or []).strip()
        if not url:
            await update.message.reply_text("📥 Usage: `/download <https://...>`")
            return
        from tools.pc_control import pc_download_url

        ctx = self._make_ctx(context, update.effective_chat.id, update.effective_user.id)
        res = await pc_download_url(ctx, url=url, folder="downloads")
        await update.message.reply_text(res)

    @staticmethod
    def bot_commands() -> list[BotCommand]:
        return [
            BotCommand("start", "Greeting and quick overview"),
            BotCommand("help", "Show available commands"),
            BotCommand("voicemode", "Toggle 2-way Marvel JARVIS voice chat"),
            BotCommand("briefing", "Morning debrief now"),
            BotCommand("evening", "Evening reflection & wrap-up"),
            BotCommand("tasks", "List & complete open tasks"),
            BotCommand("plan", "Decompose a goal into tasks & roadmap"),
            BotCommand("research", "Autonomous multi-source deep research"),
            BotCommand("study", "Autonomous self-learning student mode & syllabus"),
            BotCommand("automate", "Schedule or list autonomous agent jobs"),
            BotCommand("focus", "Start a Pomodoro / deep focus timer"),
            BotCommand("reminders", "List & cancel reminders"),
            BotCommand("expenses", "30-day expense report & chart"),
            BotCommand("habits", "Habit streaks & heatmap chart"),
            BotCommand("notes", "Second Brain knowledge vault"),
            BotCommand("export", "Export expenses, tasks, habits, or notes"),
            BotCommand("watch", "Active price-drop watches"),
            BotCommand("pc", "PC status & remote control"),
            BotCommand("top", "Top PC processes with Kill buttons"),
            BotCommand("download", "Download a URL directly to PC Downloads"),
            BotCommand("clip", "Read or set PC clipboard"),
            BotCommand("lock", "Lock Windows PC screen"),
            BotCommand("memory", "List saved memories & preferences"),
            BotCommand("status", "Show Jarvis status"),
            BotCommand("health", "Diagnostics: models, latency, voice, backups"),
            BotCommand("backup", "Back up the database now (/backup send)"),
            BotCommand("model", "Show active Gemini models"),
            BotCommand("reset", "Reset conversation history"),
            BotCommand("call", "Ring my phone"),
            BotCommand("myid", "Show my Telegram ID"),
        ]

    def register_handlers(self, app: Application) -> None:
        """Attach all command, callback, and message handlers with block=False for responsiveness."""
        app.add_handler(CommandHandler("start", self.cmd_start, block=False))
        app.add_handler(CommandHandler("help", self.cmd_help, block=False))
        app.add_handler(CommandHandler("myid", self.cmd_myid, block=False))
        app.add_handler(CommandHandler(["voicemode", "voice"], self.cmd_voicemode, block=False))
        app.add_handler(CommandHandler("briefing", self.cmd_briefing, block=False))
        app.add_handler(CommandHandler("evening", self.cmd_evening, block=False))
        app.add_handler(CommandHandler("call", self.cmd_call, block=False))
        app.add_handler(CommandHandler(["reset", "clear"], self.cmd_reset, block=False))
        app.add_handler(CommandHandler("status", self.cmd_status, block=False))
        app.add_handler(CommandHandler(["health", "diag"], self.cmd_health, block=False))
        app.add_handler(CommandHandler("backup", self.cmd_backup, block=False))
        app.add_handler(CommandHandler(["focus", "pomodoro"], self.cmd_focus, block=False))
        app.add_handler(CommandHandler("export", self.cmd_export, block=False))
        app.add_handler(CommandHandler("research", self.cmd_research, block=False))
        app.add_handler(CommandHandler("plan", self.cmd_plan, block=False))
        app.add_handler(CommandHandler(["study", "learn"], self.cmd_study, block=False))
        app.add_handler(CommandHandler(["automate", "jobs"], self.cmd_automate, block=False))
        app.add_handler(CommandHandler("model", self.cmd_model, block=False))
        app.add_handler(CommandHandler("tasks", self.cmd_tasks, block=False))
        app.add_handler(CommandHandler("reminders", self.cmd_reminders, block=False))
        app.add_handler(CommandHandler("expenses", self.cmd_expenses, block=False))
        app.add_handler(CommandHandler(["habits", "stats"], self.cmd_habits, block=False))
        app.add_handler(CommandHandler(["notes", "brain"], self.cmd_notes, block=False))
        app.add_handler(CommandHandler(["watch", "watches"], self.cmd_watch, block=False))
        app.add_handler(CommandHandler(["pc", "pcstatus"], self.cmd_pc, block=False))
        app.add_handler(CommandHandler("top", self.cmd_top, block=False))
        app.add_handler(CommandHandler("download", self.cmd_download, block=False))
        app.add_handler(CommandHandler(["clip", "clipboard"], self.cmd_clip, block=False))
        app.add_handler(CommandHandler("lock", self.cmd_lock, block=False))
        app.add_handler(CommandHandler(["memory", "memories"], self.cmd_memory, block=False))
        app.add_handler(CallbackQueryHandler(self.handle_callback, block=False))
        app.add_handler(
            MessageHandler(filters.VOICE | filters.AUDIO, self.handle_voice, block=False)
        )
        app.add_handler(
            MessageHandler(filters.PHOTO | filters.Document.ALL, self.handle_media, block=False)
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
        # Bounded timeouts so a flaky connection fails fast and retries instead of hanging.
        .connect_timeout(10)
        .read_timeout(20)
        .write_timeout(30)
        .media_write_timeout(60)
        .pool_timeout(10)
        .get_updates_connect_timeout(10)
        .get_updates_read_timeout(30)
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
