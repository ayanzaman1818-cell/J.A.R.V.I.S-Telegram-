"""Durable SQLite repository with WAL concurrency, user isolation, and safe migrations."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import config
from database.models import (
    ExpenseItem,
    HabitItem,
    MemoryItem,
    PendingAction,
    ReminderItem,
    SystemStatus,
    TaskItem,
)

log = logging.getLogger("jarvis.database")


class DatabaseRepository:
    """Manages SQLite persistence for memories, preferences, tasks, reminders, history, and confirmations."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path: Path = db_path or config.DB_PATH

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("PRAGMA busy_timeout=10000;")
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def now() -> datetime:
        return datetime.now(config.get_zoneinfo())

    def init(self) -> None:
        """Create tables and apply safe additive migrations for existing jarvis.db files."""
        default_owner = config.OWNER_ID or 0
        with self._connect() as conn:
            conn.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL DEFAULT {default_owner},
                    fact TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'general',
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, fact)
                );

                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL DEFAULT {default_owner},
                    chat_id INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    call INTEGER NOT NULL DEFAULT 0,
                    recurrence TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    delivery_state TEXT NOT NULL DEFAULT 'pending',
                    claimed_at TEXT,
                    last_error TEXT
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL DEFAULT {default_owner},
                    title TEXT NOT NULL,
                    due_at TEXT,
                    priority TEXT NOT NULL DEFAULT 'normal',
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workflows (
                    id INTEGER PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    description TEXT NOT NULL,
                    action TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS preferences (
                    user_id INTEGER NOT NULL DEFAULT {default_owner},
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY (user_id, key)
                );

                CREATE TABLE IF NOT EXISTS pending_actions (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversation_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_conv_chat_id
                    ON conversation_history(chat_id, id);

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL DEFAULT {default_owner},
                    created_at TEXT NOT NULL,
                    event TEXT NOT NULL,
                    detail TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS expenses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL DEFAULT {default_owner},
                    amount REAL NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'BDT',
                    category TEXT NOT NULL DEFAULT 'general',
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_expenses_user_created
                    ON expenses(user_id, created_at);

                CREATE TABLE IF NOT EXISTS habits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL DEFAULT {default_owner},
                    habit TEXT NOT NULL,
                    value REAL NOT NULL DEFAULT 1.0,
                    unit TEXT NOT NULL DEFAULT 'count',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_habits_user_created
                    ON habits(user_id, created_at);
                """
            )

            self._ensure_columns(
                conn,
                "memories",
                {
                    "user_id": f"INTEGER NOT NULL DEFAULT {default_owner}",
                    "category": "TEXT NOT NULL DEFAULT 'general'",
                },
            )
            self._ensure_columns(
                conn,
                "reminders",
                {
                    "user_id": f"INTEGER NOT NULL DEFAULT {default_owner}",
                    "delivery_state": "TEXT NOT NULL DEFAULT 'pending'",
                    "claimed_at": "TEXT",
                    "last_error": "TEXT",
                },
            )
            self._ensure_columns(
                conn,
                "tasks",
                {
                    "user_id": f"INTEGER NOT NULL DEFAULT {default_owner}",
                },
            )
            self._ensure_columns(
                conn,
                "preferences",
                {
                    "user_id": f"INTEGER NOT NULL DEFAULT {default_owner}",
                },
            )
            self._ensure_columns(
                conn,
                "audit_log",
                {
                    "user_id": f"INTEGER NOT NULL DEFAULT {default_owner}",
                },
            )

    @staticmethod
    def _ensure_columns(
        conn: sqlite3.Connection, table: str, definitions: dict[str, str]
    ) -> None:
        existing = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for column, col_def in definitions.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")

    def _resolve_user_id(self, user_id: int | None) -> int:
        if user_id is not None and user_id > 0:
            return int(user_id)
        return config.OWNER_ID or 0

    def audit(self, event: str, detail: str, user_id: int | None = None) -> None:
        uid = self._resolve_user_id(user_id)
        safe_detail = config.redact_secrets(detail)[:2000]
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO audit_log(user_id, created_at, event, detail) VALUES (?, ?, ?, ?)",
                (uid, self.now().isoformat(), event[:80], safe_detail),
            )

    def remember(
        self, fact: str, user_id: int | None = None, category: str = "general"
    ) -> bool:
        uid = self._resolve_user_id(user_id)
        cleaned = fact.strip()
        if not cleaned:
            return False
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM memories WHERE user_id = ? AND LOWER(fact) = LOWER(?)",
                (uid, cleaned),
            ).fetchone()
            if existing:
                return False
            conn.execute(
                "INSERT INTO memories(user_id, fact, category, created_at) VALUES (?, ?, ?, ?)",
                (uid, cleaned, category.strip() or "general", self.now().isoformat()),
            )
        self.audit("memory_saved", cleaned, user_id=uid)
        return True

    def list_memory_items(
        self, query: str = "", limit: int = 25, user_id: int | None = None
    ) -> list[MemoryItem]:
        uid = self._resolve_user_id(user_id)
        with self._connect() as conn:
            if query.strip():
                rows = conn.execute(
                    """SELECT id, user_id, fact, category, created_at
                       FROM memories
                       WHERE user_id = ? AND fact LIKE ?
                       ORDER BY id DESC LIMIT ?""",
                    (uid, f"%{query.strip()}%", limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, user_id, fact, category, created_at
                       FROM memories
                       WHERE user_id = ?
                       ORDER BY id DESC LIMIT ?""",
                    (uid, limit),
                ).fetchall()
        return [
            MemoryItem(
                id=row["id"],
                user_id=row["user_id"],
                fact=row["fact"],
                category=row["category"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def memories(
        self, query: str = "", limit: int = 25, user_id: int | None = None
    ) -> list[str]:
        return [
            item.fact
            for item in self.list_memory_items(query=query, limit=limit, user_id=user_id)
        ]

    def forget(self, query: str, user_id: int | None = None) -> int:
        uid = self._resolve_user_id(user_id)
        cleaned = query.strip()
        if not cleaned:
            return 0
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM memories WHERE user_id = ? AND fact LIKE ?",
                (uid, f"%{cleaned}%"),
            )
        self.audit("memory_deleted", f"{cleaned} (count={cur.rowcount})", user_id=uid)
        return cur.rowcount

    def set_preference(
        self, key: str, value: Any, user_id: int | None = None
    ) -> None:
        uid = self._resolve_user_id(user_id)
        norm_key = key.strip().lower()
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM preferences WHERE user_id = ? AND key = ?",
                (uid, norm_key),
            )
            conn.execute(
                "INSERT OR REPLACE INTO preferences(user_id, key, value) VALUES (?, ?, ?)",
                (uid, norm_key, json.dumps(value)),
            )
        self.audit("preference_set", f"{norm_key}={value}", user_id=uid)

    def get_preference(
        self, key: str, default: Any = None, user_id: int | None = None
    ) -> Any:
        uid = self._resolve_user_id(user_id)
        norm_key = key.strip().lower()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM preferences WHERE user_id = ? AND key = ?",
                (uid, norm_key),
            ).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            return row["value"]

    def list_preferences(self, user_id: int | None = None) -> dict[str, Any]:
        uid = self._resolve_user_id(user_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM preferences WHERE user_id = ? ORDER BY key",
                (uid,),
            ).fetchall()
        result: dict[str, Any] = {}
        for row in rows:
            try:
                result[row["key"]] = json.loads(row["value"])
            except (TypeError, json.JSONDecodeError):
                result[row["key"]] = row["value"]
        return result

    def add_task(
        self,
        title: str,
        due_at: str = "",
        priority: str = "normal",
        user_id: int | None = None,
    ) -> int:
        uid = self._resolve_user_id(user_id)
        norm_priority = priority.lower() if priority.lower() in ("low", "normal", "high") else "normal"
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO tasks(user_id, title, due_at, priority, created_at) VALUES (?, ?, ?, ?, ?)",
                (uid, title.strip(), due_at.strip() or None, norm_priority, self.now().isoformat()),
            )
            task_id = int(cur.lastrowid)
        self.audit("task_created", f"#{task_id} {title}", user_id=uid)
        return task_id

    def list_tasks(
        self, status: str = "open", limit: int = 20, user_id: int | None = None
    ) -> list[dict[str, Any]]:
        uid = self._resolve_user_id(user_id)
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT id, user_id, title, due_at, priority, status, created_at
                   FROM tasks
                   WHERE user_id = ? AND status = ?
                   ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
                            due_at IS NULL, due_at, id
                   LIMIT ?""",
                (uid, status, limit),
            ).fetchall()
        return [
            TaskItem(
                id=row["id"],
                user_id=row["user_id"],
                title=row["title"],
                due_at=row["due_at"],
                priority=row["priority"],
                status=row["status"],
                created_at=row["created_at"],
            ).to_dict()
            for row in rows
        ]

    def complete_task(self, task_id: int, user_id: int | None = None) -> bool:
        uid = self._resolve_user_id(user_id)
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status = 'done' WHERE id = ? AND user_id = ? AND status = 'open'",
                (int(task_id), uid),
            )
        if cur.rowcount:
            self.audit("task_completed", f"#{task_id}", user_id=uid)
            return True
        return False

    def create_reminder(
        self,
        chat_id: int,
        due_at: datetime,
        message: str,
        call: bool = False,
        recurrence: str = "",
        user_id: int | None = None,
    ) -> int:
        uid = self._resolve_user_id(user_id or chat_id)
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO reminders(
                       user_id, chat_id, message, due_at, call, recurrence, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    uid,
                    chat_id,
                    message.strip(),
                    due_at.isoformat(),
                    int(bool(call)),
                    recurrence.strip().lower(),
                    self.now().isoformat(),
                ),
            )
            reminder_id = int(cur.lastrowid)
        self.audit(
            "reminder_created",
            f"#{reminder_id} {due_at.isoformat()} {message}",
            user_id=uid,
        )
        return reminder_id

    def due_reminders(self, until: datetime | None = None) -> list[dict[str, Any]]:
        target = until or self.now()
        stale_cutoff = (target - timedelta(minutes=5)).isoformat()
        with self._connect() as conn:
            conn.execute(
                """UPDATE reminders
                   SET delivery_state = 'pending', claimed_at = NULL
                   WHERE active = 1 AND delivery_state = 'sending'
                     AND (claimed_at IS NULL OR claimed_at <= ?)""",
                (stale_cutoff,),
            )
            rows = conn.execute(
                """SELECT * FROM reminders
                   WHERE active = 1 AND delivery_state = 'pending' AND due_at <= ?
                   ORDER BY due_at""",
                (target.isoformat(),),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_reminder(self, reminder_id: int) -> bool:
        """Atomically claim a due reminder so only one worker processes it."""
        current = self.now().isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE reminders
                   SET delivery_state = 'sending', claimed_at = ?, last_error = NULL
                   WHERE id = ? AND active = 1 AND delivery_state = 'pending' AND due_at <= ?""",
                (current, int(reminder_id), current),
            )
        return cur.rowcount == 1

    def fail_reminder(self, reminder_id: int, error: str | Exception) -> None:
        message = config.redact_secrets(str(error))[:2000]
        with self._connect() as conn:
            conn.execute(
                """UPDATE reminders
                   SET delivery_state = 'failed', claimed_at = NULL, last_error = ?
                   WHERE id = ? AND delivery_state = 'sending'""",
                (message, int(reminder_id)),
            )
        self.audit("reminder_failed", f"#{reminder_id}: {message[:300]}")

    def complete_reminder(self, reminder: dict[str, Any]) -> None:
        """Mark a delivered reminder complete or advance its next recurrence."""
        recurrence = (reminder.get("recurrence") or "").strip().lower()
        reminder_id = int(reminder["id"])

        with self._connect() as conn:
            if recurrence in {"daily", "weekly"}:
                interval = timedelta(days=1 if recurrence == "daily" else 7)
                due = datetime.fromisoformat(reminder["due_at"])
                current = self.now()
                while due <= current:
                    due += interval
                conn.execute(
                    """UPDATE reminders
                       SET due_at = ?, delivery_state = 'pending',
                           claimed_at = NULL, last_error = NULL
                       WHERE id = ? AND delivery_state = 'sending'""",
                    (due.isoformat(), reminder_id),
                )
            else:
                conn.execute(
                    """UPDATE reminders
                       SET active = 0, delivery_state = 'sent',
                           claimed_at = NULL, last_error = NULL
                       WHERE id = ? AND delivery_state = 'sending'""",
                    (reminder_id,),
                )
        self.audit("reminder_fired", f"#{reminder_id}", user_id=reminder.get("user_id"))

    def upcoming_reminders(
        self, limit: int = 10, user_id: int | None = None
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if user_id is not None:
                uid = self._resolve_user_id(user_id)
                rows = conn.execute(
                    """SELECT * FROM reminders
                       WHERE active = 1 AND (user_id = ? OR chat_id = ?)
                       ORDER BY due_at LIMIT ?""",
                    (uid, uid, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM reminders WHERE active = 1 ORDER BY due_at LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def cancel_reminder(self, reminder_id: int, user_id: int | None = None) -> bool:
        uid = self._resolve_user_id(user_id)
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE reminders
                   SET active = 0, delivery_state = 'cancelled'
                   WHERE id = ? AND active = 1 AND (user_id = ? OR chat_id = ?)""",
                (int(reminder_id), uid, uid),
            )
        if cur.rowcount:
            self.audit("reminder_cancelled", f"#{reminder_id}", user_id=uid)
            return True
        return False

    def snooze_reminder(
        self,
        reminder_id: int,
        minutes: int = 10,
        user_id: int | None = None,
    ) -> datetime | None:
        uid = self._resolve_user_id(user_id)
        due = self.now() + timedelta(minutes=max(1, int(minutes)))
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM reminders WHERE id = ? AND (user_id = ? OR chat_id = ?)",
                (int(reminder_id), uid, uid),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                """UPDATE reminders
                   SET active = 1,
                       delivery_state = 'pending',
                       due_at = ?,
                       claimed_at = NULL,
                       last_error = NULL
                   WHERE id = ?""",
                (due.isoformat(), int(reminder_id)),
            )
        self.audit(
            "reminder_snoozed",
            f"#{reminder_id} +{minutes}m -> {due.isoformat()}",
            user_id=uid,
        )
        return due

    def get_latest_pending_action(self, chat_id: int) -> PendingAction | None:
        now_ts = time.time()
        with self._connect() as conn:
            conn.execute("DELETE FROM pending_actions WHERE expires_at < ?", (now_ts,))
            row = conn.execute(
                """SELECT * FROM pending_actions
                   WHERE chat_id = ? AND expires_at >= ?
                   ORDER BY created_at DESC LIMIT 1""",
                (chat_id, now_ts),
            ).fetchone()
        if not row:
            return None
        return PendingAction(
            token=row["token"],
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            kind=row["kind"],
            payload=json.loads(row["payload"]),
            summary=row["summary"],
            expires_at=float(row["expires_at"]),
            created_at=row["created_at"],
        )

    def add_expense(
        self,
        amount: float,
        currency: str = "BDT",
        category: str = "general",
        note: str = "",
        user_id: int | None = None,
    ) -> int:
        uid = self._resolve_user_id(user_id)
        norm_curr = (currency or "BDT").strip().upper() or "BDT"
        norm_cat = (category or "general").strip().lower() or "general"
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO expenses(user_id, amount, currency, category, note, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    uid,
                    float(amount),
                    norm_curr,
                    norm_cat,
                    note.strip(),
                    self.now().isoformat(),
                ),
            )
            exp_id = int(cur.lastrowid)
        self.audit(
            "expense_logged",
            f"#{exp_id} {amount} {norm_curr} [{norm_cat}] {note}",
            user_id=uid,
        )
        return exp_id

    def list_expenses(
        self,
        days: int = 30,
        category: str = "",
        limit: int = 25,
        user_id: int | None = None,
    ) -> list[dict[str, Any]]:
        uid = self._resolve_user_id(user_id)
        cutoff = (self.now() - timedelta(days=max(1, int(days)))).isoformat()
        with self._connect() as conn:
            if category.strip():
                rows = conn.execute(
                    """SELECT * FROM expenses
                       WHERE user_id = ? AND created_at >= ? AND LOWER(category) = LOWER(?)
                       ORDER BY id DESC LIMIT ?""",
                    (uid, cutoff, category.strip(), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM expenses
                       WHERE user_id = ? AND created_at >= ?
                       ORDER BY id DESC LIMIT ?""",
                    (uid, cutoff, limit),
                ).fetchall()
        return [
            ExpenseItem(
                id=row["id"],
                user_id=row["user_id"],
                amount=float(row["amount"]),
                currency=row["currency"],
                category=row["category"],
                note=row["note"],
                created_at=row["created_at"],
            ).to_dict()
            for row in rows
        ]

    def expense_summary(
        self, days: int = 30, user_id: int | None = None
    ) -> dict[str, Any]:
        uid = self._resolve_user_id(user_id)
        cutoff = (self.now() - timedelta(days=max(1, int(days)))).isoformat()
        with self._connect() as conn:
            by_cat = conn.execute(
                """SELECT currency, category, ROUND(SUM(amount), 2) AS total, COUNT(*) AS count
                   FROM expenses
                   WHERE user_id = ? AND created_at >= ?
                   GROUP BY currency, category
                   ORDER BY total DESC""",
                (uid, cutoff),
            ).fetchall()
            totals = conn.execute(
                """SELECT currency, ROUND(SUM(amount), 2) AS total, COUNT(*) AS count
                   FROM expenses
                   WHERE user_id = ? AND created_at >= ?
                   GROUP BY currency""",
                (uid, cutoff),
            ).fetchall()
        return {
            "days": int(days),
            "totals": [dict(r) for r in totals],
            "by_category": [dict(r) for r in by_cat],
        }

    def delete_expense(self, expense_id: int, user_id: int | None = None) -> bool:
        uid = self._resolve_user_id(user_id)
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM expenses WHERE id = ? AND user_id = ?",
                (int(expense_id), uid),
            )
        if cur.rowcount:
            self.audit("expense_deleted", f"#{expense_id}", user_id=uid)
            return True
        return False

    def log_habit(
        self,
        habit: str,
        value: float = 1.0,
        unit: str = "count",
        user_id: int | None = None,
    ) -> int:
        uid = self._resolve_user_id(user_id)
        norm_habit = habit.strip().lower()
        norm_unit = (unit or "count").strip() or "count"
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO habits(user_id, habit, value, unit, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (uid, norm_habit, float(value), norm_unit, self.now().isoformat()),
            )
            habit_id = int(cur.lastrowid)
        self.audit(
            "habit_logged",
            f"#{habit_id} {norm_habit}: {value} {norm_unit}",
            user_id=uid,
        )
        return habit_id

    def habit_summary(
        self, days: int = 7, user_id: int | None = None
    ) -> list[dict[str, Any]]:
        uid = self._resolve_user_id(user_id)
        cutoff = (self.now() - timedelta(days=max(1, int(days)))).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT habit, unit, ROUND(SUM(value), 2) AS total_value, COUNT(*) AS entries
                   FROM habits
                   WHERE user_id = ? AND created_at >= ?
                   GROUP BY habit, unit
                   ORDER BY entries DESC, habit ASC""",
                (uid, cutoff),
            ).fetchall()
        return [dict(r) for r in rows]

    def create_pending_action(
        self,
        token: str,
        user_id: int,
        chat_id: int,
        kind: str,
        payload: dict[str, Any],
        summary: str,
        ttl_seconds: int = 900,
    ) -> PendingAction:
        expires_at = time.time() + ttl_seconds
        created_at = self.now().isoformat()
        with self._connect() as conn:
            conn.execute("DELETE FROM pending_actions WHERE expires_at < ?", (time.time(),))
            conn.execute(
                """INSERT INTO pending_actions(
                       token, user_id, chat_id, kind, payload, summary, expires_at, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    token,
                    user_id,
                    chat_id,
                    kind,
                    json.dumps(payload),
                    summary,
                    expires_at,
                    created_at,
                ),
            )
        self.audit("action_prepared", f"{kind} token={token}: {summary}", user_id=user_id)
        return PendingAction(
            token=token,
            user_id=user_id,
            chat_id=chat_id,
            kind=kind,
            payload=payload,
            summary=summary,
            expires_at=expires_at,
            created_at=created_at,
        )

    def pop_pending_action(self, chat_id: int, token: str) -> PendingAction | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_actions WHERE token = ? AND chat_id = ?",
                (token.strip(), chat_id),
            ).fetchone()
            if not row:
                return None
            conn.execute("DELETE FROM pending_actions WHERE token = ?", (token.strip(),))

        if time.time() > float(row["expires_at"]):
            return None

        return PendingAction(
            token=row["token"],
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            kind=row["kind"],
            payload=json.loads(row["payload"]),
            summary=row["summary"],
            expires_at=float(row["expires_at"]),
            created_at=row["created_at"],
        )

    def clear_pending_actions(self, chat_id: int) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM pending_actions WHERE chat_id = ?", (chat_id,)
            )
        return cur.rowcount

    def append_history_messages(
        self,
        chat_id: int,
        messages: list[dict[str, Any]],
        user_id: int | None = None,
        max_rows: int = 60,
    ) -> None:
        if not messages:
            return
        uid = self._resolve_user_id(user_id or chat_id)
        now_iso = self.now().isoformat()
        with self._connect() as conn:
            conn.executemany(
                """INSERT INTO conversation_history(user_id, chat_id, role, payload, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    (
                        uid,
                        chat_id,
                        str(msg.get("role", "user")),
                        json.dumps(msg, default=str),
                        now_iso,
                    )
                    for msg in messages
                ],
            )
            conn.execute(
                """DELETE FROM conversation_history
                   WHERE chat_id = ? AND id NOT IN (
                       SELECT id FROM conversation_history
                       WHERE chat_id = ? ORDER BY id DESC LIMIT ?
                   )""",
                (chat_id, chat_id, max_rows),
            )

    def save_history_messages(
        self, chat_id: int, messages: list[dict[str, Any]], user_id: int | None = None
    ) -> None:
        uid = self._resolve_user_id(user_id or chat_id)
        now_iso = self.now().isoformat()
        with self._connect() as conn:
            conn.execute("DELETE FROM conversation_history WHERE chat_id = ?", (chat_id,))
            conn.executemany(
                """INSERT INTO conversation_history(user_id, chat_id, role, payload, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    (
                        uid,
                        chat_id,
                        str(msg.get("role", "user")),
                        json.dumps(msg, default=str),
                        now_iso,
                    )
                    for msg in messages
                ],
            )

    def load_history_messages(self, chat_id: int, limit: int = 30) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT payload FROM (
                       SELECT id, payload FROM conversation_history
                       WHERE chat_id = ? ORDER BY id DESC LIMIT ?
                   ) ORDER BY id ASC""",
                (chat_id, limit),
            ).fetchall()
        messages: list[dict[str, Any]] = []
        for row in rows:
            try:
                messages.append(json.loads(row["payload"]))
            except (TypeError, json.JSONDecodeError):
                continue
        return messages

    def clear_history_messages(self, chat_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM conversation_history WHERE chat_id = ?", (chat_id,))

    def status(self, user_id: int | None = None) -> dict[str, int]:
        uid = self._resolve_user_id(user_id)
        with self._connect() as conn:
            memories_count = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE user_id = ?", (uid,)
            ).fetchone()[0]
            tasks_count = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE user_id = ? AND status = 'open'",
                (uid,),
            ).fetchone()[0]
            reminders_count = conn.execute(
                "SELECT COUNT(*) FROM reminders WHERE active = 1 AND (user_id = ? OR chat_id = ?)",
                (uid, uid),
            ).fetchone()[0]
            prefs_count = conn.execute(
                "SELECT COUNT(*) FROM preferences WHERE user_id = ?", (uid,)
            ).fetchone()[0]
        return {
            "memories": int(memories_count),
            "open_tasks": int(tasks_count),
            "active_reminders": int(reminders_count),
            "preferences": int(prefs_count),
        }


db = DatabaseRepository()
