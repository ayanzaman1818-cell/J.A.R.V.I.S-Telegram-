"""Long-term memory and preference tools backed by SQLite."""

from __future__ import annotations

from database.repository import db
from tools.base import ToolContext, tool


def load_memory(user_id: int | None = None) -> list[str]:
    return db.memories(limit=100, user_id=user_id)


@tool(
    "remember",
    "Save a lasting fact about the user (preferences, people, routines) to long-term memory.",
    {
        "fact": {"type": "string", "description": "The fact to remember."},
        "category": {"type": "string", "description": "Optional category (e.g. 'preference', 'work', 'general')."},
    },
    ["fact"],
)
async def remember(ctx: ToolContext, fact: str, category: str = "general") -> str:
    cleaned = fact.strip()
    if not cleaned:
        return "Nothing to remember."
    db.remember(cleaned, user_id=ctx.user_id, category=category)
    return "Noted."


@tool(
    "forget_memory",
    "Delete long-term memories that contain this text.",
    {"query": {"type": "string", "description": "Substring to match."}},
    ["query"],
)
async def forget_memory(ctx: ToolContext, query: str) -> str:
    return f"Deleted {db.forget(query, user_id=ctx.user_id)} memory item(s)."


@tool(
    "list_memories",
    "List remembered user facts, optionally filtered by text.",
    {"query": {"type": "string", "description": "Optional filter text."}},
)
async def list_memories(ctx: ToolContext, query: str = "") -> str:
    items = db.memories(query, user_id=ctx.user_id)
    return "\n".join(f"- {item}" for item in items) or "No matching memories."


@tool(
    "set_preference",
    "Save a structured user preference key/value pair.",
    {
        "key": {"type": "string", "description": "Preference key, e.g. 'briefing_style'."},
        "value": {"type": "string", "description": "Preference value."},
    },
    ["key", "value"],
)
async def set_preference(ctx: ToolContext, key: str, value: str) -> str:
    db.set_preference(key, value, user_id=ctx.user_id)
    return f"Preference '{key.strip().lower()}' saved."
