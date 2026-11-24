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
                try:
                    from services.desktop_overlay import notify_desktop_overlay
                    notify_desktop_overlay("High Memory Alert", f"PC RAM usage is at {ram_pct:.0f}%.", "alert")
                except Exception:
                    pass
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
    """Deliver due reminders once, or execute autonomous [AGENT] background jobs."""
    from tools.agentic import AGENT_JOB_PREFIX

    for reminder in db.due_reminders():
        reminder_id = reminder["id"]
        if not db.claim_reminder(reminder_id):
            continue

        raw_msg = str(reminder.get("message") or "")
        is_agent_job = raw_msg.startswith(AGENT_JOB_PREFIX)

        try:
            if is_agent_job:
                instruction = raw_msg[len(AGENT_JOB_PREFIX) :].strip()
                from brain.agent import respond as brain_respond
                from services.telegram import TelegramService
                from tools.base import ToolContext

                ctx = ToolContext(
                    bot=context.bot,
                    chat_id=reminder["chat_id"],
                    job_queue=getattr(context, "job_queue", None),
                    user_id=reminder.get("user_id") or reminder["chat_id"],
                )
                reply = await brain_respond(ctx, instruction)
                await TelegramService.send_long(
                    context.bot,
                    reminder["chat_id"],
                    f"🤖 Autonomous Agent Report (#{reminder_id}):\n{reply}",
                )
            else:
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
                try:
                    from services.desktop_overlay import notify_desktop_overlay
                    notify_desktop_overlay("Reminder", raw_msg, "reminder")
                except Exception:
                    pass
                await context.bot.send_message(
                    reminder["chat_id"],
                    f"Reminder: {raw_msg}",
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


async def backup_job(context: Any) -> None:
    """Nightly SQLite backup; alerts the owner only if it fails."""
    try:
        from services.backup import backup_database

        await asyncio.to_thread(backup_database)
    except Exception as exc:
        log.exception("Nightly database backup failed")
        if config.OWNER_ID:
            try:
                await context.bot.send_message(
                    config.OWNER_ID,
                    f"⚠️ Nightly database backup failed: {type(exc).__name__}. Run /backup to retry.",
                )
            except Exception:
                log.debug("Could not report backup failure", exc_info=True)


async def auto_study_job(context: Any) -> None:
    """Background self-study session: JARVIS studies the next lesson or discovers a new topic on its own."""
    if not config.OWNER_ID or not config.AUTO_STUDY_ENABLED or config.is_quiet_hours():
        return
    try:
        from brain.gemini import is_local_model
        if is_local_model(config.LLM_MODEL):
            # When using a local LLM in LM Studio, use 0-token casual reading refresh instead of multi-pass LLM synthesis
            # so background learning never hogs the CPU or blocks LM Studio slots!
            await casual_reading_job(context)
            return
        from services.telegram import TelegramService
        from tools.base import ToolContext
        from tools.student import run_study_session

        ctx = ToolContext(
            bot=context.bot,
            chat_id=config.OWNER_ID,
            job_queue=getattr(context, "job_queue", None),
            user_id=config.OWNER_ID,
        )
        report = await run_study_session(ctx, auto_discover=True)
        log.info("Silent background auto-study session completed")
        if config.AUTO_STUDY_NOTIFY and report:
            await TelegramService.send_long(context.bot, config.OWNER_ID, report)
    except Exception:
        log.warning("Background auto_study_job failed", exc_info=True)


async def casual_reading_job(context: Any) -> None:
    """Periodic casual reading refresh: JARVIS quietly browses a topic (like picking up a book or magazine)
    to keep its local LLM world knowledge fresh over time without spamming the user.
    """
    if not config.OWNER_ID or config.is_quiet_hours():
        return
    try:
        from tools.base import ToolContext
        from tools.student import run_casual_reading_refresh

        ctx = ToolContext(
            bot=context.bot,
            chat_id=config.OWNER_ID,
            job_queue=getattr(context, "job_queue", None),
            user_id=config.OWNER_ID,
        )
        await run_casual_reading_refresh(ctx)
    except Exception:
        log.debug("Background casual_reading_job skipped", exc_info=True)


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
    try:
        bh, bm = (int(x) for x in config.BACKUP_TIME.split(":"))
        job_queue.run_daily(
            backup_job,
            time=dtime(bh, bm, tzinfo=config.get_zoneinfo()),
            name="backup",
        )
        log.info("Nightly database backup scheduled for %s %s", config.BACKUP_TIME, config.TIMEZONE)
    except Exception:
        log.exception("Invalid BACKUP_TIME=%r; nightly backup not scheduled", config.BACKUP_TIME)

    if config.OWNER_ID and config.AUTO_STUDY_ENABLED:
        study_interval = max(1, int(config.AUTO_STUDY_INTERVAL_HOURS)) * 3600
        job_queue.run_repeating(
            auto_study_job,
            interval=study_interval,
            first=600,
            name="auto_study",
        )
        log.info(
            "Autonomous self-study loop scheduled every %sh",
            config.AUTO_STUDY_INTERVAL_HOURS,
        )

    if config.OWNER_ID:
        # Casual reading loop: every 3 hours (and 2 minutes after startup), JARVIS quietly reads up on
        # current world events, games, tech, and topics the user recently talked about.
        job_queue.run_repeating(
            casual_reading_job,
            interval=3 * 3600,
            first=120,
            name="casual_reading",
        )
        log.info("Casual reading knowledge-refresh loop scheduled every 3h")
