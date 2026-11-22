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
    "Optionally repeat daily, on weekdays (Mon-Fri), weekly, or monthly.",
    {
        "when": {"type": "string"},
        "message": {"type": "string"},
        "recurrence": {"type": "string", "enum": ["daily", "weekdays", "weekly", "monthly"]},
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
    if recurrence == "weekdays":
        from datetime import timedelta

        while due.weekday() >= 5:
            due += timedelta(days=1)
    reminder_id = db.create_reminder(
        ctx.chat_id,
        due,
        message.strip(),
        call=bool(call),
        recurrence=recurrence,
        user_id=ctx.user_id,
    )
    repeat = f", repeating {recurrence}" if recurrence else ""
    return f"Reminder #{reminder_id} set for {_fmt_time(due)}{repeat}."


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


@tool(
    "start_focus_timer",
    "Start a Pomodoro / deep focus countdown timer that alerts the user when time is up.",
    {
        "minutes": {
            "type": "integer",
            "description": "Focus duration in minutes (default 25, 1..240).",
        },
        "label": {
            "type": "string",
            "description": "What the user is focusing on (e.g. 'Deep work on API').",
        },
    },
    [],
)
async def start_focus_timer(
    ctx: ToolContext, minutes: int = 25, label: str = "Deep focus session"
) -> str:
    dur = max(1, min(int(minutes or 25), 240))
    task_label = (label or "Deep focus session").strip()
    due = db.now() + timedelta(minutes=dur)
    msg = f"🎯 Focus session complete ({dur}m): {task_label}"
    reminder_id = db.create_reminder(
        ctx.chat_id, due, msg, call=False, user_id=ctx.user_id
    )
    return (
        f"🎯 Focus timer #{reminder_id} started for {dur} minutes: '{task_label}'. "
        f"I will notify you at {_fmt_time(due)}."
    )


def build_export_payload(
    dataset: str = "expenses", days: int = 90, user_id: int | None = None
) -> tuple[str, bytes, int]:
    """Build an export file (filename, utf8_bytes, row_count) for expenses, tasks, notes, or habits."""
    import csv
    import io

    kind = (dataset or "expenses").strip().lower()
    span = max(1, min(int(days or 90), 3650))

    if kind == "expenses":
        items = db.list_expenses(days=span, limit=2000, user_id=user_id)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "created_at", "amount", "currency", "category", "note"])
        for e in items:
            writer.writerow(
                [
                    e["id"],
                    e["created_at"],
                    f"{float(e['amount']):.2f}",
                    e["currency"],
                    e["category"],
                    e.get("note", ""),
                ]
            )
        return f"jarvis_expenses_{span}d.csv", buf.getvalue().encode("utf-8"), len(items)

    if kind == "tasks":
        open_tasks = db.list_tasks(status="open", limit=500, user_id=user_id)
        done_tasks = db.list_tasks(status="done", limit=500, user_id=user_id)
        all_tasks = open_tasks + done_tasks
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "status", "priority", "title", "due_at", "created_at"])
        for t in all_tasks:
            writer.writerow(
                [
                    t["id"],
                    t["status"],
                    t["priority"],
                    t["title"],
                    t.get("due_at") or "",
                    t.get("created_at") or "",
                ]
            )
        return "jarvis_tasks.csv", buf.getvalue().encode("utf-8"), len(all_tasks)

    if kind == "habits":
        summary = db.habit_summary(days=span, user_id=user_id)
        habits = summary.get("habits") or []
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["habit", "count_in_period", "active_days", "streak_days", "last_logged"])
        for h in habits:
            writer.writerow(
                [
                    h["habit"],
                    h["total_count"],
                    h["active_days"],
                    h.get("streak", 0),
                    h["last_logged"],
                ]
            )
        return f"jarvis_habits_{span}d.csv", buf.getvalue().encode("utf-8"), len(habits)

    if kind == "notes":
        notes = db.list_knowledge_notes(limit=500, user_id=user_id)
        lines = [f"# JARVIS Second Brain Export ({len(notes)} notes)\n"]
        for n in notes:
            lines.append(f"## #{n['id']} — {n['title']}")
            lines.append(f"- Created: {n.get('created_at', '')}")
            if n.get("tags"):
                lines.append(f"- Tags: {n['tags']}")
            if n.get("source_url"):
                lines.append(f"- Source: {n['source_url']}")
            lines.append(f"\n{n.get('content', '').strip()}\n\n---\n")
        return "jarvis_notes.md", "\n".join(lines).encode("utf-8"), len(notes)

    raise ValueError(f"Unsupported export dataset: {dataset}")


@tool(
    "export_data",
    "Export the user's expenses, tasks, habits, or Second Brain notes as a downloadable CSV/Markdown file in Telegram.",
    {
        "dataset": {
            "type": "string",
            "enum": ["expenses", "tasks", "habits", "notes"],
            "description": "Which dataset to export.",
        },
        "days": {
            "type": "integer",
            "description": "Lookback window in days for expenses/habits (default 90).",
        },
    },
    ["dataset"],
)
async def export_data(ctx: ToolContext, dataset: str, days: int = 90) -> str:
    import io

    filename, data_bytes, count = build_export_payload(
        dataset=dataset, days=days, user_id=ctx.user_id
    )
    if ctx.bot and ctx.chat_id:
        bio = io.BytesIO(data_bytes)
        bio.name = filename
        await ctx.bot.send_document(
            ctx.chat_id,
            document=bio,
            filename=filename,
            caption=f"📦 Exported {count} {dataset} record(s) ({filename}).",
        )
        return f"Sent {filename} ({count} records) to Telegram."
    return f"Prepared {filename} with {count} {dataset} record(s) ({len(data_bytes)} bytes)."
