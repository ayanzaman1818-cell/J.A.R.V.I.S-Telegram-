"""APScheduler job callbacks and registration for reminders and morning briefings."""

from __future__ import annotations

import logging
from datetime import time as dtime
from typing import Any

import config
from database.repository import db
from services import briefing, voice

log = logging.getLogger("jarvis.scheduler")


async def morning_job(context: Any) -> None:
    if not config.OWNER_ID:
        return
    await briefing.send_briefing(context.bot, config.OWNER_ID, context.job_queue)


async def check_persistent_reminders(context: Any) -> None:
    """Deliver due reminders once, without repeating after optional call failures."""
    for reminder in db.due_reminders():
        reminder_id = reminder["id"]
        if not db.claim_reminder(reminder_id):
            continue

        try:
            await context.bot.send_message(
                reminder["chat_id"], f"Reminder: {reminder['message']}"
            )
        except Exception as exc:
            try:
                db.fail_reminder(reminder_id, exc)
            except Exception:
                log.exception("Could not mark reminder #%s as failed", reminder_id)
            log.exception("Telegram reminder delivery failed: #%s", reminder_id)
            continue

        try:
            db.complete_reminder(reminder)
        except Exception as exc:
            try:
                db.fail_reminder(reminder_id, exc)
            except Exception:
                log.exception("Could not mark reminder #%s as failed", reminder_id)
            log.exception("Could not complete reminder #%s", reminder_id)
            continue

        if reminder.get("call"):
            try:
                await voice.phone_call(f"Reminder. {reminder['message']}")
            except Exception:
                log.exception("Phone call failed for reminder #%s", reminder_id)
                try:
                    await context.bot.send_message(
                        reminder["chat_id"],
                        "(The reminder was delivered, but the phone call failed.)",
                    )
                except Exception:
                    log.exception(
                        "Could not report phone-call failure for #%s", reminder_id
                    )


def register_jobs(app: Any) -> None:
    """Attach daily briefing and repeating reminder checks to the Telegram job queue."""
    job_queue = getattr(app, "job_queue", None)
    if job_queue is None:
        log.warning("Telegram job_queue is unavailable; scheduled jobs are disabled.")
        return

    if config.OWNER_ID:
        try:
            hh, mm = (int(x) for x in config.BRIEFING_TIME.split(":"))
            job_queue.run_daily(
                morning_job,
                time=dtime(hh, mm, tzinfo=config.get_zoneinfo()),
                name="morning",
            )
            log.info(
                "Morning briefing scheduled for %s %s",
                config.BRIEFING_TIME,
                config.TIMEZONE,
            )
        except Exception:
            log.exception(
                "Invalid BRIEFING_TIME=%r; morning briefing not scheduled",
                config.BRIEFING_TIME,
            )

    job_queue.run_repeating(
        check_persistent_reminders,
        interval=30,
        first=10,
        name="reminders",
    )
