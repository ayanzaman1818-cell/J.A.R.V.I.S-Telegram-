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
            "/reminders - list active scheduled reminders with cancel buttons\n"
            "/expenses - view 30-day expense breakdown and chart button\n"
            "/habits (or /stats) - view habit streaks and heatmap chart button\n"
            "/notes [query] (or /brain) - list or search your Second Brain knowledge vault\n"
            "/watch - list active product price-drop watches\n"
            "/pc (or /pcstatus) - PC hardware status, clipboard, focus modes & remote controls\n"
            "/clip [text] - view PC clipboard or copy <text> to PC clipboard\n"
            "/lock - lock your Windows PC immediately (owner only)\n"
            "/memory (or /memories) - list remembered facts and user preferences\n"
            "/status - display system health, voice engine, models, and database counts\n"
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
                    InlineKeyboardButton("📸 Screenshot", callback_data="pc_act:screenshot"),
                    InlineKeyboardButton("⏯ Play/Pause", callback_data="pc_act:play_pause"),
                ],
                [
                    InlineKeyboardButton("🔇 Mute", callback_data="pc_act:mute"),
                    InlineKeyboardButton("🔒 Lock PC", callback_data="pc_act:lock"),
                ],
                [
                    InlineKeyboardButton("📋 Read Clipboard", callback_data="pc_act:clipboard"),
                    InlineKeyboardButton("💼 Work Mode", callback_data="pc_act:work"),
                ],
                [
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
                voice_svc.set_voice_mode(True, user_id=user_id)

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
                from tools.pc_control import (
                    pc_clipboard_get,
                    pc_focus_mode,
                    pc_lock,
                    pc_media_control,
                    pc_screenshot,
                )

                await query.answer(f"Running {act}...")
                if act == "screenshot":
                    await pc_screenshot(ctx)
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

            if data.startswith("chart:"):
                kind = data.split(":", 1)[1]
                from tools.charts import expense_chart, habit_chart

                await query.answer("Generating visual chart...")
                if kind == "expense":
                    await expense_chart(ctx, days=30)
                elif kind == "habit":
                    await habit_chart(ctx, days=14)
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
                voice_svc.set_voice_mode(True, user_id=user_id)
                await query.answer(f"Voice set to {chosen}")
                await query.message.reply_text(
                    f"🎩 Voice updated to {preset.upper()} ({chosen}). Two-way voice chat is active."
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
        log.error("Unhandled Telegram error", exc_info=context.error)

    @staticmethod
    def bot_commands() -> list[BotCommand]:
        return [
            BotCommand("start", "Greeting and quick overview"),
            BotCommand("help", "Show available commands"),
            BotCommand("voicemode", "Toggle 2-way Marvel JARVIS voice chat"),
            BotCommand("briefing", "Morning debrief now"),
            BotCommand("evening", "Evening reflection & wrap-up"),
            BotCommand("tasks", "List & complete open tasks"),
            BotCommand("reminders", "List & cancel reminders"),
            BotCommand("expenses", "30-day expense report & chart"),
            BotCommand("habits", "Habit streaks & heatmap chart"),
            BotCommand("notes", "Second Brain knowledge vault"),
            BotCommand("watch", "Active price-drop watches"),
            BotCommand("pc", "PC status & remote control"),
            BotCommand("clip", "Read or set PC clipboard"),
            BotCommand("lock", "Lock Windows PC screen"),
            BotCommand("memory", "List saved memories & preferences"),
            BotCommand("status", "Show Jarvis status"),
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
        app.add_handler(CommandHandler("model", self.cmd_model, block=False))
        app.add_handler(CommandHandler("tasks", self.cmd_tasks, block=False))
        app.add_handler(CommandHandler("reminders", self.cmd_reminders, block=False))
        app.add_handler(CommandHandler("expenses", self.cmd_expenses, block=False))
        app.add_handler(CommandHandler(["habits", "stats"], self.cmd_habits, block=False))
        app.add_handler(CommandHandler(["notes", "brain"], self.cmd_notes, block=False))
        app.add_handler(CommandHandler(["watch", "watches"], self.cmd_watch, block=False))
        app.add_handler(CommandHandler(["pc", "pcstatus"], self.cmd_pc, block=False))
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
