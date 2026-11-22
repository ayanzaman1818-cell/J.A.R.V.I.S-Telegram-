"""Voice note and phone call tools."""

from __future__ import annotations

import os

from services import voice
from tools.base import ToolContext, tool


@tool(
    "speak",
    "Send the user a spoken voice note in Telegram ONLY when the user explicitly asks you to speak, send a voice note, or read something aloud.",
    {"text": {"type": "string"}},
    ["text"],
)
async def speak(ctx: ToolContext, text: str) -> str:
    path = await voice.tts_file(text, user_id=ctx.user_id)
    try:
        if ctx.bot:
            with open(path, "rb") as f:
                await ctx.bot.send_voice(ctx.chat_id, f)
    finally:
        if os.path.exists(path):
            os.unlink(path)
    return "Voice note sent."


@tool(
    "set_voice_chat_mode",
    "Enable or disable continuous two-way Telegram voice conversation mode (where JARVIS replies with spoken voice notes automatically), and optionally switch the TTS voice preset (jarvis, sonia, christopher, jenny, aria, natasha).",
    {
        "enabled": {
            "type": "boolean",
            "description": "True to enable two-way voice chat mode, False to return to text-only default.",
        },
        "voice_preset": {
            "type": "string",
            "description": "Optional voice preset ('jarvis', 'sonia', 'christopher', 'jenny', 'aria', 'natasha').",
        },
    },
    ["enabled"],
)
async def set_voice_chat_mode(
    ctx: ToolContext, enabled: bool, voice_preset: str = ""
) -> str:
    voice.set_voice_mode(bool(enabled), user_id=ctx.user_id)
    v_str = ""
    if voice_preset.strip():
        chosen = voice.set_user_tts_voice(voice_preset.strip(), user_id=ctx.user_id)
        v_str = f" Voice set to {chosen}."
    state = "enabled (I will reply with spoken voice messages)" if enabled else "disabled (text-only unless you send a voice note)"
    return f"Two-way Telegram voice chat mode {state}.{v_str}"


@tool(
    "jarvis_voice_clip",
    "Send an authentic Paul Bettany Marvel JARVIS voice clip ONLY when the user explicitly asks to hear a Marvel JARVIS sound clip or quote.",
    {
        "category": {
            "type": "string",
            "enum": [
                "session.start",
                "task.acknowledge",
                "task.complete",
                "task.error",
                "input.required",
                "resource.limit",
                "user.spam",
            ],
            "description": "Event category of the Marvel JARVIS clip (default 'task.acknowledge').",
        },
        "query": {
            "type": "string",
            "description": "Optional phrase search (e.g. 'welcome home', 'at your service', 'new element').",
        },
    },
)
async def jarvis_voice_clip(
    ctx: ToolContext, category: str = "task.acknowledge", query: str = ""
) -> str:
    picked = voice.pick_openpeon_clip(category=category, query=query)
    if not picked:
        return "Marvel JARVIS sound pack is not available."
    clip_path, label = picked
    if ctx.bot and os.path.exists(clip_path):
        with open(clip_path, "rb") as f:
            await ctx.bot.send_voice(ctx.chat_id, f, caption=f'🎩 "{label}"')
    return f'Sent Marvel JARVIS voice clip: "{label}"'


@tool(
    "call_me",
    "Ring the user right now with a real Native 1-on-1 Telegram VoIP Call (if JARVIS's Telegram Call account is logged in) or a live two-way voice call link. Use when asked to call, or for urgent alerts.",
    {"message": {"type": "string", "description": "What to say when the user picks up. Keep it concise."}},
    ["message"],
)
async def call_me(ctx: ToolContext, message: str) -> str:
    from services.telegram_call import is_native_call_configured, place_native_telegram_call

    if is_native_call_configured():
        try:
            res = await place_native_telegram_call(
                target_user_id=ctx.user_id,
                greeting=message,
            )
            return res
        except Exception:
            pass

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from services.twilio_webhook import get_web_call_url, start_twilio_webhook_server

    await start_twilio_webhook_server(bot=ctx.bot, job_queue=ctx.job_queue)
    call_url = get_web_call_url(greeting=message)

    if ctx.bot and ctx.chat_id:
        markup = (
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("📞 Answer Live Voice Call with JARVIS", url=call_url)]]
            )
            if call_url.startswith("https://")
            else None
        )
        await ctx.bot.send_message(
            ctx.chat_id,
            f"📞 Incoming Live Voice Call from J.A.R.V.I.S.:\n\"{message}\"\n\n{call_url}",
            reply_markup=markup,
        )

    if voice.calls_enabled():
        try:
            sid = await voice.phone_call(message)
            return f"Sent Live Voice Call button and rang phone via Twilio (call id {sid})."
        except Exception:
            pass
    return f"Sent Live 2-Way Voice Call link to Telegram ({call_url})."
