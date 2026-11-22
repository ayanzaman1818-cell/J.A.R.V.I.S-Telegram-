"""Typed dataclasses representing persistent entities in JARVIS."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class MemoryItem:
    id: int
    user_id: int
    fact: str
    category: str
    created_at: str


@dataclass(slots=True)
class TaskItem:
    id: int
    user_id: int
    title: str
    due_at: str | None
    priority: str
    status: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "title": self.title,
            "due_at": self.due_at,
            "priority": self.priority,
            "status": self.status,
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class ReminderItem:
    id: int
    user_id: int
    chat_id: int
    message: str
    due_at: str
    call: bool
    recurrence: str
    active: bool
    created_at: str
    delivery_state: str
    claimed_at: str | None = None
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "message": self.message,
            "due_at": self.due_at,
            "call": self.call,
            "recurrence": self.recurrence,
            "active": self.active,
            "created_at": self.created_at,
            "delivery_state": self.delivery_state,
            "claimed_at": self.claimed_at,
            "last_error": self.last_error,
        }


@dataclass(slots=True)
class PendingAction:
    token: str
    user_id: int
    chat_id: int
    kind: str
    payload: dict[str, Any]
    summary: str
    expires_at: float
    created_at: str


@dataclass(slots=True)
class AuditEntry:
    id: int
    user_id: int
    created_at: str
    event: str
    detail: str


@dataclass(slots=True)
class ExpenseItem:
    id: int
    user_id: int
    amount: float
    currency: str
    category: str
    note: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "amount": self.amount,
            "currency": self.currency,
            "category": self.category,
            "note": self.note,
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class HabitItem:
    id: int
    user_id: int
    habit: str
    value: float
    unit: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "habit": self.habit,
            "value": self.value,
            "unit": self.unit,
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class KnowledgeNote:
    id: int
    user_id: int
    title: str
    content: str
    source_url: str
    tags: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "title": self.title,
            "content": self.content,
            "source_url": self.source_url,
            "tags": self.tags,
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class PriceWatchItem:
    id: int
    user_id: int
    chat_id: int
    url: str
    product_name: str
    target_price: float
    currency: str
    last_price: float | None
    active: bool
    created_at: str
    last_checked_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "url": self.url,
            "product_name": self.product_name,
            "target_price": self.target_price,
            "currency": self.currency,
            "last_price": self.last_price,
            "active": self.active,
            "created_at": self.created_at,
            "last_checked_at": self.last_checked_at,
        }


@dataclass(slots=True)
class BudgetItem:
    id: int
    user_id: int
    category: str
    monthly_limit: float
    currency: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "category": self.category,
            "monthly_limit": self.monthly_limit,
            "currency": self.currency,
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class StudyTopic:
    id: int
    user_id: int
    subject: str
    level: int
    lessons_completed: int
    next_subtopic: str
    status: str
    created_at: str
    last_studied_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "subject": self.subject,
            "level": self.level,
            "lessons_completed": self.lessons_completed,
            "next_subtopic": self.next_subtopic,
            "status": self.status,
            "created_at": self.created_at,
            "last_studied_at": self.last_studied_at,
        }


@dataclass(slots=True)
class SystemStatus:
    memories: int
    open_tasks: int
    active_reminders: int
    preferences: int
