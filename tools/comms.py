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
    path = await voice.tts_file(text)
    try:
        with open(path, "rb") as f:
            await ctx.bot.send_voice(ctx.chat_id, f)
    finally:
        if os.path.exists(path):
            os.unlink(path)
    return "Voice note sent."


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
