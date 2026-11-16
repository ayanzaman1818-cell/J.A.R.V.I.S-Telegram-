"""Voice note and phone call tools."""

from __future__ import annotations

import os

from services import voice
from tools.base import ToolContext, tool


@tool(
    "speak",
    "Send the user a spoken voice note in Telegram.",
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
    "Send an authentic Paul Bettany Marvel JARVIS voice clip from the bundled openpeon-jarvis sound pack.",
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
    "Phone the user right now and read a short message aloud. Use when asked to call, "
    "or for genuinely urgent things. One-way: the user cannot reply on the call.",
    {"message": {"type": "string", "description": "What to say. Keep it short."}},
    ["message"],
)
async def call_me(ctx: ToolContext, message: str) -> str:
    sid = await voice.phone_call(message)
    return f"Calling now (call id {sid})."
