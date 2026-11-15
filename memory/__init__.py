"""User memory and preference context formatting for JARVIS."""

from __future__ import annotations

from database.repository import db


def build_memory_context(user_id: int | None = None, limit: int = 100) -> str:
    """Format user preferences and remembered facts for injection into the system prompt."""
    prefs = db.list_preferences(user_id=user_id)
    facts = db.memories(limit=limit, user_id=user_id)

    lines: list[str] = []
    for k, v in prefs.items():
        lines.append(f"- [preference] {k}: {v}")
    for fact in facts:
        lines.append(f"- {fact}")

    return "\n".join(lines) or "(nothing yet)"
