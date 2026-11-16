"""APScheduler job callbacks and registration for reminders and morning briefings."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import time as dtime
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import config
from database.repository import db
from services import briefing, voice

log = logging.getLogger("jarvis.scheduler")

_LAST_ALERT_TS: dict[str, float] = {}


async def morning_job(context: Any) -> None:
    if not config.OWNER_ID:
        return
    await briefing.send_briefing(context.bot, config.OWNER_ID, context.job_queue)


async def evening_job(context: Any) -> None:
    if not config.OWNER_ID:
        return
    await briefing.send_evening_briefing(context.bot, config.OWNER_ID, context.job_queue)


async def proactive_monitor_job(context: Any) -> None:
    """Check PC battery/RAM/disk thresholds and active price watches in the background."""
    if config.OWNER_ID and config.PROACTIVE_ALERTS:
        try:
            from tools.pc_control import get_pc_metrics

            metrics = await asyncio.to_thread(get_pc_metrics)
            now_ts = time.time()

            batt = metrics.get("battery_pct")
            if (
                batt is not None
                and metrics.get("on_battery")
                and batt <= config.BATTERY_ALERT_THRESHOLD
                and now_ts - _LAST_ALERT_TS.get("battery", 0.0) >= 3600
            ):
                _LAST_ALERT_TS["battery"] = now_ts
                await context.bot.send_message(
                    config.OWNER_ID,
                    f"🔋 Battery Alert: {config.USER_NAME}, your PC battery is at {batt}% (unplugged). "
                    "Please connect the charger.",
                )

            ram_pct = float(metrics.get("ram_pct", 0.0))
            if ram_pct >= 93.0 and now_ts - _LAST_ALERT_TS.get("ram", 0.0) >= 7200:
                _LAST_ALERT_TS["ram"] = now_ts
                await context.bot.send_message(
                    config.OWNER_ID,
                    f"⚠️ High Memory Alert: PC RAM usage is at {ram_pct:.0f}%.",
                )

            disk_pct = float(metrics.get("disk_pct", 0.0))
            if disk_pct >= 93.0 and now_ts - _LAST_ALERT_TS.get("disk", 0.0) >= 21600:
                _LAST_ALERT_TS["disk"] = now_ts
                await context.bot.send_message(
                    config.OWNER_ID,
                    f"⚠️ Low Disk Space Alert: PC disk is {disk_pct:.0f}% full.",
                )
        except Exception:
            log.debug("Proactive PC monitor check failed", exc_info=True)

    try:
        from tools.watchdog import evaluate_all_price_watches

        await evaluate_all_price_watches(context.bot)
    except Exception:
        log.debug("Background price watch check failed", exc_info=True)


async def check_persistent_reminders(context: Any) -> None:
    """Deliver due reminders once, without repeating after optional call failures."""
    for reminder in db.due_reminders():
        reminder_id = reminder["id"]
        if not db.claim_reminder(reminder_id):
            continue

        try:
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Done", callback_data=f"rem_done:{reminder_id}"
                        ),
                        InlineKeyboardButton(
                            "💤 Snooze 10m",
                            callback_data=f"rem_snooze:{reminder_id}:10",
                        ),
                    ]
                ]
            )
            await context.bot.send_message(
                reminder["chat_id"],
                f"Reminder: {reminder['message']}",
                reply_markup=markup,
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

        try:
            eh, em = (int(x) for x in config.EVENING_BRIEFING_TIME.split(":"))
            job_queue.run_daily(
                evening_job,
                time=dtime(eh, em, tzinfo=config.get_zoneinfo()),
                name="evening",
            )
            log.info(
                "Evening briefing scheduled for %s %s",
                config.EVENING_BRIEFING_TIME,
                config.TIMEZONE,
            )
        except Exception:
            log.exception(
                "Invalid EVENING_BRIEFING_TIME=%r; evening briefing not scheduled",
                config.EVENING_BRIEFING_TIME,
            )

    job_queue.run_repeating(
        check_persistent_reminders,
        interval=30,
        first=10,
        name="reminders",
    )
    job_queue.run_repeating(
        proactive_monitor_job,
        interval=600,
        first=90,
        name="proactive_monitor",
    )
