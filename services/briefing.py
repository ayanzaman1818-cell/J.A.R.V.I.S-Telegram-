"""Morning debrief builder and multi-channel delivery (Telegram text, voice, phone call)."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any

import config
from brain import agent
from database.repository import db
from services import voice
from tools.base import ToolContext
from tools.integrations import calendar_today, gmail_search
from tools.news_weather import get_weather, tech_news

log = logging.getLogger("jarvis.briefing")


async def build_briefing(ctx: ToolContext) -> str:
    weather, news, calendar, inbox = await asyncio.gather(
        get_weather(ctx, config.LOCATION),
        tech_news(ctx),
        calendar_today(ctx),
        gmail_search(ctx, "is:unread", max_results=10),
        return_exceptions=True,
    )
    weather_txt = weather if not isinstance(weather, Exception) else "Weather unavailable."
    news_txt = news if not isinstance(news, Exception) else "News unavailable."
    calendar_txt = calendar if not isinstance(calendar, Exception) else "Calendar unavailable."
    inbox_txt = inbox if not isinstance(inbox, Exception) else "Inbox unavailable."
    reminders = db.upcoming_reminders(limit=10, user_id=ctx.user_id)
    reminder_text = "\n".join(f"- {r['due_at']}: {r['message']}" for r in reminders) or "None."
    exp_sum = db.expense_summary(days=7, user_id=ctx.user_id)
    exp_text = (
        ", ".join(f"{t['total']} {t['currency']} ({t['count']} items)" for t in exp_sum["totals"])
        or "None logged in last 7 days."
    )
    habits = db.habit_summary(days=7, user_id=ctx.user_id)
    habit_text = (
        ", ".join(f"{h['habit']}: {h['total_value']} {h['unit']}" for h in habits)
        or "None logged in last 7 days."
    )
    today = datetime.now(config.get_zoneinfo()).strftime("%A %d %B")

    prompt = f"""Deliver the user's morning debrief for {today}.

Weather data:
{weather_txt}

Tech headlines (with links):
{news_txt}

Calendar today:
{calendar_txt}

Unread email data:
{inbox_txt}

Upcoming reminders:
{reminder_text}

7-day expenses:
{exp_text}

7-day habits:
{habit_text}

Format: a brief greeting in character, then one line on the weather with a practical tip
(umbrella, jacket, etc.), then today's appointments and reminders if present, a one-line
unread-email priority, a quick note on 7-day expenses/habits if any exist, then the 5 most
important or interesting tech stories, each one line plus its link. Pick variety over
repetition across sources. Close by asking if there is anything he'd like you to dig into.
Under 280 words. Plain text only, no markdown."""
    return await agent.simple(prompt, user_id=ctx.user_id)


async def send_briefing(bot: Any, chat_id: int, job_queue: Any = None) -> None:
    ctx = ToolContext(bot=bot, chat_id=chat_id, job_queue=job_queue, user_id=chat_id)
    text = await build_briefing(ctx)
    for i in range(0, max(1, len(text)), 4000):
        await bot.send_message(chat_id, text[i : i + 4000] or "Good morning.")

    if config.BRIEFING_VOICE:
        try:
            path = await voice.tts_file(text)
            try:
                with open(path, "rb") as f:
                    await bot.send_voice(chat_id, f)
            finally:
                if os.path.exists(path):
                    os.unlink(path)
        except Exception:
            log.exception("Briefing voice note failed")

    if config.BRIEFING_CALL and voice.calls_enabled():
        try:
            await voice.phone_call(text)
        except Exception:
            log.exception("Briefing call failed")
