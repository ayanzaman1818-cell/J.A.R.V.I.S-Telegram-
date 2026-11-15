"""Expense and habit tracking tools backed by SQLite."""

from __future__ import annotations

from database.repository import db
from tools.base import ToolContext, tool


@tool(
    "log_expense",
    "Record a personal expense in SQLite (e.g. 'Spent 450 BDT on groceries' or from a receipt photo).",
    {
        "amount": {"type": "number", "description": "Numeric amount spent (must be > 0)."},
        "currency": {"type": "string", "description": "Currency code, default 'BDT'."},
        "category": {
            "type": "string",
            "description": "Category such as 'food', 'groceries', 'transport', 'bills', 'shopping', 'general'.",
        },
        "note": {"type": "string", "description": "Short description or merchant name."},
    },
    ["amount"],
)
async def log_expense(
    ctx: ToolContext,
    amount: float,
    currency: str = "BDT",
    category: str = "general",
    note: str = "",
) -> str:
    if amount <= 0:
        return "Expense amount must be greater than 0."
    exp_id = db.add_expense(
        amount=amount,
        currency=currency,
        category=category,
        note=note,
        user_id=ctx.user_id,
    )
    curr = (currency or "BDT").strip().upper()
    cat = (category or "general").strip().lower()
    desc = f" ({note.strip()})" if note.strip() else ""
    return f"Logged expense #{exp_id}: {amount:.2f} {curr} under [{cat}]{desc}."


@tool(
    "expense_report",
    "Get a summary of recent expenses grouped by category, plus recent individual items.",
    {
        "days": {"type": "integer", "description": "Number of days to look back (default 30)."},
        "category": {"type": "string", "description": "Optional category filter."},
    },
)
async def expense_report(
    ctx: ToolContext, days: int = 30, category: str = ""
) -> str:
    days = max(1, min(int(days), 365))
    summary = db.expense_summary(days=days, user_id=ctx.user_id)
    items = db.list_expenses(
        days=days, category=category, limit=15, user_id=ctx.user_id
    )
    if not items and not summary["totals"]:
        return f"No expenses recorded in the last {days} days."

    lines = [f"Expenses (last {days} days):"]
    for tot in summary["totals"]:
        lines.append(f"Total: {tot['total']} {tot['currency']} ({tot['count']} items)")
    if summary["by_category"]:
        lines.append("\nBy category:")
        for c in summary["by_category"]:
            lines.append(f"- {c['category']}: {c['total']} {c['currency']} ({c['count']})")
    if items:
        lines.append("\nRecent entries:")
        for it in items[:10]:
            date_short = str(it["created_at"])[:10]
            note_part = f" - {it['note']}" if it.get("note") else ""
            lines.append(
                f"#{it['id']} [{date_short}] {it['amount']} {it['currency']} ({it['category']}){note_part}"
            )
    return "\n".join(lines)


@tool(
    "delete_expense",
    "Delete a logged expense by its ID.",
    {"expense_id": {"type": "integer"}},
    ["expense_id"],
)
async def delete_expense(ctx: ToolContext, expense_id: int) -> str:
    if db.delete_expense(expense_id, user_id=ctx.user_id):
        return f"Expense #{expense_id} deleted."
    return f"No expense found with ID #{expense_id}."


@tool(
    "log_habit",
    "Log a daily habit or workout activity (e.g. 'workout 30 mins', 'water 2 liters', 'reading 20 pages').",
    {
        "habit": {"type": "string", "description": "Habit name, e.g. 'workout', 'reading', 'water'."},
        "value": {"type": "number", "description": "Amount completed (default 1)."},
        "unit": {"type": "string", "description": "Unit such as 'mins', 'pages', 'glasses', 'count'."},
    },
    ["habit"],
)
async def log_habit(
    ctx: ToolContext, habit: str, value: float = 1.0, unit: str = "count"
) -> str:
    if not habit.strip():
        return "Please provide a habit name."
    hid = db.log_habit(
        habit=habit, value=value, unit=unit, user_id=ctx.user_id
    )
    return f"Logged habit #{hid}: {habit.strip().lower()} ({value} {unit})."


@tool(
    "habit_report",
    "View habit completion totals and streaks over the past N days.",
    {"days": {"type": "integer", "description": "Days to look back (default 7)."}},
)
async def habit_report(ctx: ToolContext, days: int = 7) -> str:
    days = max(1, min(int(days), 90))
    rows = db.habit_summary(days=days, user_id=ctx.user_id)
    if not rows:
        return f"No habits logged in the last {days} days."
    lines = [f"Habit summary (last {days} days):"]
    for r in rows:
        lines.append(
            f"- {r['habit']}: {r['total_value']} {r['unit']} across {r['entries']} session(s)"
        )
    return "\n".join(lines)
