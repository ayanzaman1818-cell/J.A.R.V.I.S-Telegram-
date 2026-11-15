"""Persistent reminders and tasks backed by SQLite."""

from __future__ import annotations

from datetime import datetime, timedelta

import config
from database.repository import db
from tools.base import ToolContext, tool


def _fmt_time(dt: datetime) -> str:
    """Cross-platform 12-hour time formatting without leading zero (Windows-safe)."""
    time_part = dt.strftime("%I:%M %p").lstrip("0")
    return f"{dt.strftime('%a %d %b')}, {time_part}"


@tool(
    "set_reminder",
    "Set a reminder that messages the user (and optionally phones them) after a delay. "
    "Reminders are stored in SQLite and survive bot restarts.",
    {
        "minutes": {"type": "number", "description": "Minutes from now (must be > 0)."},
        "message": {"type": "string"},
        "call": {"type": "boolean", "description": "Also phone the user."},
    },
    ["minutes", "message"],
)
async def set_reminder(
    ctx: ToolContext, minutes: float, message: str, call: bool = False
) -> str:
    if minutes <= 0:
        return "Reminder delay must be greater than 0 minutes."
    due = db.now() + timedelta(minutes=float(minutes))
    reminder_id = db.create_reminder(
        ctx.chat_id, due, message.strip(), call=bool(call), user_id=ctx.user_id
    )
    return f"Reminder #{reminder_id} set for {_fmt_time(due)}."


@tool(
    "set_reminder_at",
    "Set a persistent reminder at a natural-language time such as 'tomorrow at 8pm'. "
    "Optionally repeat daily or weekly.",
    {
        "when": {"type": "string"},
        "message": {"type": "string"},
        "recurrence": {"type": "string", "enum": ["daily", "weekly"]},
        "call": {"type": "boolean"},
    },
    ["when", "message"],
)
async def set_reminder_at(
    ctx: ToolContext, when: str, message: str, recurrence: str = "", call: bool = False
) -> str:
    try:
        import dateparser

        due = dateparser.parse(
            when,
            settings={
                "TIMEZONE": config.TIMEZONE,
                "RETURN_AS_TIMEZONE_AWARE": True,
                "PREFER_DATES_FROM": "future",
            },
        )
    except ImportError:
        due = None
    if not due:
        return "I couldn't parse that time. Please give a specific date and time."
    reminder_id = db.create_reminder(
        ctx.chat_id,
        due,
        message.strip(),
        call=bool(call),
        recurrence=recurrence,
        user_id=ctx.user_id,
    )
    return f"Reminder #{reminder_id} set for {_fmt_time(due)}."


@tool("list_reminders", "List upcoming persistent reminders.")
async def list_reminders(ctx: ToolContext) -> str:
    items = db.upcoming_reminders(user_id=ctx.user_id)
    return (
        "\n".join(f"#{r['id']} {r['due_at']}: {r['message']}" for r in items)
        or "No upcoming reminders."
    )


@tool(
    "cancel_reminder",
    "Cancel an active persistent reminder by its ID.",
    {"reminder_id": {"type": "integer"}},
    ["reminder_id"],
)
async def cancel_reminder(ctx: ToolContext, reminder_id: int) -> str:
    return (
        f"Reminder #{reminder_id} cancelled."
        if db.cancel_reminder(reminder_id, user_id=ctx.user_id)
        else f"No active reminder with ID #{reminder_id}."
    )


@tool(
    "add_task",
    "Add a personal task to Jarvis's persistent task inbox.",
    {
        "title": {"type": "string"},
        "due_at": {"type": "string", "description": "Optional due date/time text."},
        "priority": {"type": "string", "enum": ["low", "normal", "high"]},
    },
    ["title"],
)
async def add_task(
    ctx: ToolContext, title: str, due_at: str = "", priority: str = "normal"
) -> str:
    task_id = db.add_task(
        title.strip(), due_at=due_at, priority=priority, user_id=ctx.user_id
    )
    return f"Task #{task_id} added."


@tool("list_tasks", "List open personal tasks.")
async def list_tasks(ctx: ToolContext) -> str:
    tasks = db.list_tasks(user_id=ctx.user_id)
    return (
        "\n".join(
            f"#{t['id']} [{t['priority']}] {t['title']}"
            + (f" (due {t['due_at']})" if t.get("due_at") else "")
            for t in tasks
        )
        or "No open tasks."
    )


@tool(
    "complete_task",
    "Mark a task complete by its ID.",
    {"task_id": {"type": "integer"}},
    ["task_id"],
)
async def complete_task(ctx: ToolContext, task_id: int) -> str:
    return (
        "Task completed."
        if db.complete_task(task_id, user_id=ctx.user_id)
        else "No open task with that ID."
    )
