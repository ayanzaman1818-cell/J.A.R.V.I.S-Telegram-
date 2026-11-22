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
    BudgetItem,
    ExpenseItem,
    HabitItem,
    KnowledgeNote,
    MemoryItem,
    PendingAction,
    PriceWatchItem,
    ReminderItem,
    StudyTopic,
    SystemStatus,
    TaskItem,
)

log = logging.getLogger("jarvis.database")

RECURRENCES = ("daily", "weekdays", "weekly", "monthly")


def _add_month(dt: datetime) -> datetime:
    """Same day next month, clamped to the month's last day (Jan 31 -> Feb 28/29)."""
    import calendar

    year, month = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def next_occurrence(due: datetime, recurrence: str, now: datetime) -> datetime:
    """Advance a recurring reminder's due time to the first slot strictly after `now`."""
    recurrence = (recurrence or "").strip().lower()
    if recurrence == "monthly":
        anchor_day = due.day
        while due <= now:
            due = _add_month(due)
            # Restore the original day where possible (e.g. 31st after a short month).
            try:
                due = due.replace(day=anchor_day)
            except ValueError:
                pass
        return due
    step = timedelta(days=7 if recurrence == "weekly" else 1)
    while due <= now or (recurrence == "weekdays" and due.weekday() >= 5):
        due += step
    return due


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

                CREATE TABLE IF NOT EXISTS budgets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL DEFAULT {default_owner},
                    category TEXT NOT NULL,
                    monthly_limit REAL NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'BDT',
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, category, currency)
                );

                CREATE TABLE IF NOT EXISTS knowledge_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL DEFAULT {default_owner},
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_url TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '',
                    embedding TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_knowledge_user_created
                    ON knowledge_notes(user_id, created_at);

                CREATE TABLE IF NOT EXISTS price_watches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL DEFAULT {default_owner},
                    chat_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    target_price REAL NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'USD',
                    last_price REAL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_checked_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_price_watches_active
                    ON price_watches(active, user_id);

                CREATE TABLE IF NOT EXISTS study_topics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL DEFAULT {default_owner},
                    subject TEXT NOT NULL,
                    level INTEGER NOT NULL DEFAULT 1,
                    lessons_completed INTEGER NOT NULL DEFAULT 0,
                    next_subtopic TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    last_studied_at TEXT,
                    UNIQUE(user_id, subject)
                );

                CREATE INDEX IF NOT EXISTS idx_study_topics_user_status
                    ON study_topics(user_id, status);
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
            if recurrence in RECURRENCES:
                due = next_occurrence(
                    datetime.fromisoformat(reminder["due_at"]), recurrence, self.now()
                )
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

    def daily_expense_series(
        self, days: int = 14, user_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Return daily expense totals for the past N days (chronological)."""
        uid = self._resolve_user_id(user_id)
        n_days = max(1, min(int(days), 90))
        now_dt = self.now()
        cutoff = (now_dt - timedelta(days=n_days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT SUBSTR(created_at, 1, 10) AS day, currency, ROUND(SUM(amount), 2) AS total
                   FROM expenses
                   WHERE user_id = ? AND created_at >= ?
                   GROUP BY day, currency
                   ORDER BY day ASC""",
                (uid, cutoff.isoformat()),
            ).fetchall()
        by_day: dict[str, float] = {}
        for r in rows:
            by_day[r["day"]] = by_day.get(r["day"], 0.0) + float(r["total"])

        result: list[dict[str, Any]] = []
        for i in range(n_days):
            d_str = (cutoff + timedelta(days=i)).strftime("%Y-%m-%d")
            result.append({"date": d_str, "total": round(by_day.get(d_str, 0.0), 2)})
        return result

    def daily_habit_matrix(
        self, days: int = 14, user_id: int | None = None
    ) -> dict[str, Any]:
        """Return per-habit daily completion counts and current streaks for the past N days."""
        uid = self._resolve_user_id(user_id)
        n_days = max(1, min(int(days), 60))
        now_dt = self.now()
        cutoff = (now_dt - timedelta(days=n_days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        date_labels = [
            (cutoff + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n_days)
        ]
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT habit, unit, SUBSTR(created_at, 1, 10) AS day, ROUND(SUM(value), 2) AS val
                   FROM habits
                   WHERE user_id = ? AND created_at >= ?
                   GROUP BY habit, unit, day
                   ORDER BY habit ASC, day ASC""",
                (uid, cutoff.isoformat()),
            ).fetchall()

        habits_map: dict[str, dict[str, Any]] = {}
        for r in rows:
            h_name = r["habit"]
            info = habits_map.setdefault(
                h_name,
                {"habit": h_name, "unit": r["unit"], "days": {}, "total": 0.0},
            )
            info["days"][r["day"]] = float(r["val"])
            info["total"] = round(info["total"] + float(r["val"]), 2)

        for info in habits_map.values():
            streak = 0
            for d_str in reversed(date_labels):
                if info["days"].get(d_str, 0.0) > 0:
                    streak += 1
                elif d_str == date_labels[-1]:
                    # Allow today not yet logged if yesterday was logged
                    continue
                else:
                    break
            info["streak"] = streak

        return {
            "dates": date_labels,
            "habits": list(habits_map.values()),
        }

    def set_budget(
        self,
        category: str,
        monthly_limit: float,
        currency: str = "BDT",
        user_id: int | None = None,
    ) -> int:
        uid = self._resolve_user_id(user_id)
        norm_cat = (category or "general").strip().lower() or "general"
        norm_curr = (currency or "BDT").strip().upper() or "BDT"
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO budgets(user_id, category, monthly_limit, currency, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, category, currency)
                   DO UPDATE SET monthly_limit = excluded.monthly_limit""",
                (uid, norm_cat, float(monthly_limit), norm_curr, self.now().isoformat()),
            )
            row = conn.execute(
                "SELECT id FROM budgets WHERE user_id = ? AND category = ? AND currency = ?",
                (uid, norm_cat, norm_curr),
            ).fetchone()
            bid = int(row["id"]) if row else 0
        self.audit("budget_set", f"{norm_cat}: {monthly_limit} {norm_curr}", user_id=uid)
        return bid

    def list_budgets(self, user_id: int | None = None) -> list[dict[str, Any]]:
        uid = self._resolve_user_id(user_id)
        month_start = self.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM budgets WHERE user_id = ? ORDER BY category ASC",
                (uid,),
            ).fetchall()
            results: list[dict[str, Any]] = []
            for r in rows:
                spent_row = conn.execute(
                    """SELECT COALESCE(SUM(amount), 0.0) AS spent
                       FROM expenses
                       WHERE user_id = ? AND category = ? AND currency = ? AND created_at >= ?""",
                    (uid, r["category"], r["currency"], month_start),
                ).fetchone()
                spent = round(float(spent_row["spent"] if spent_row else 0.0), 2)
                limit_val = float(r["monthly_limit"])
                pct = round((spent / limit_val) * 100, 1) if limit_val > 0 else 0.0
                results.append(
                    {
                        "id": r["id"],
                        "category": r["category"],
                        "monthly_limit": limit_val,
                        "currency": r["currency"],
                        "spent": spent,
                        "percent": pct,
                    }
                )
        return results

    def check_budget_alert(
        self, category: str, currency: str = "BDT", user_id: int | None = None
    ) -> str | None:
        """Return a warning message if the user's monthly category budget is >= 80% used."""
        norm_cat = (category or "general").strip().lower()
        norm_curr = (currency or "BDT").strip().upper()
        for b in self.list_budgets(user_id=user_id):
            if b["category"] == norm_cat and b["currency"] == norm_curr:
                if b["percent"] >= 100.0:
                    return (
                        f"⚠️ Budget exceeded for [{norm_cat}]: "
                        f"{b['spent']:.2f} / {b['monthly_limit']:.2f} {norm_curr} ({b['percent']:.0f}%)."
                    )
                if b["percent"] >= 80.0:
                    return (
                        f"⚠️ Budget alert for [{norm_cat}]: "
                        f"{b['spent']:.2f} / {b['monthly_limit']:.2f} {norm_curr} ({b['percent']:.0f}%)."
                    )
        return None

    def add_knowledge_note(
        self,
        title: str,
        content: str,
        source_url: str = "",
        tags: str = "",
        embedding: list[float] | None = None,
        user_id: int | None = None,
    ) -> int:
        uid = self._resolve_user_id(user_id)
        emb_json = json.dumps(embedding) if embedding else ""
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO knowledge_notes(user_id, title, content, source_url, tags, embedding, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    uid,
                    title.strip()[:300] or "Untitled Note",
                    content.strip(),
                    source_url.strip(),
                    tags.strip().lower(),
                    emb_json,
                    self.now().isoformat(),
                ),
            )
            note_id = int(cur.lastrowid)
        self.audit("knowledge_saved", f"#{note_id} {title.strip()[:80]}", user_id=uid)
        return note_id

    def list_knowledge_notes(
        self, limit: int = 30, tag: str = "", user_id: int | None = None
    ) -> list[dict[str, Any]]:
        uid = self._resolve_user_id(user_id)
        with self._connect() as conn:
            if tag.strip():
                rows = conn.execute(
                    """SELECT id, user_id, title, content, source_url, tags, embedding, created_at
                       FROM knowledge_notes
                       WHERE user_id = ? AND tags LIKE ?
                       ORDER BY id DESC LIMIT ?""",
                    (uid, f"%{tag.strip().lower()}%", int(limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, user_id, title, content, source_url, tags, embedding, created_at
                       FROM knowledge_notes
                       WHERE user_id = ?
                       ORDER BY id DESC LIMIT ?""",
                    (uid, int(limit)),
                ).fetchall()
        return [dict(r) for r in rows]

    def delete_knowledge_note(self, note_id: int, user_id: int | None = None) -> bool:
        uid = self._resolve_user_id(user_id)
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM knowledge_notes WHERE id = ? AND user_id = ?",
                (int(note_id), uid),
            )
        if cur.rowcount:
            self.audit("knowledge_deleted", f"#{note_id}", user_id=uid)
            return True
        return False

    def add_price_watch(
        self,
        chat_id: int,
        url: str,
        product_name: str,
        target_price: float,
        currency: str = "USD",
        last_price: float | None = None,
        user_id: int | None = None,
    ) -> int:
        uid = self._resolve_user_id(user_id or chat_id)
        now_iso = self.now().isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO price_watches(
                       user_id, chat_id, url, product_name, target_price, currency,
                       last_price, active, created_at, last_checked_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (
                    uid,
                    chat_id,
                    url.strip(),
                    product_name.strip()[:200] or "Tracked Item",
                    float(target_price),
                    (currency or "USD").strip().upper(),
                    float(last_price) if last_price is not None else None,
                    now_iso,
                    now_iso if last_price is not None else None,
                ),
            )
            watch_id = int(cur.lastrowid)
        self.audit("price_watch_added", f"#{watch_id} {product_name}", user_id=uid)
        return watch_id

    def list_price_watches(
        self, active_only: bool = True, user_id: int | None = None
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if user_id is not None:
                uid = self._resolve_user_id(user_id)
                sql = "SELECT * FROM price_watches WHERE user_id = ?"
                params: tuple[Any, ...] = (uid,)
                if active_only:
                    sql += " AND active = 1"
                sql += " ORDER BY id DESC"
                rows = conn.execute(sql, params).fetchall()
            else:
                sql = "SELECT * FROM price_watches"
                if active_only:
                    sql += " WHERE active = 1"
                sql += " ORDER BY id ASC"
                rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def update_price_watch(
        self, watch_id: int, last_price: float | None, active: bool = True
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE price_watches
                   SET last_price = ?, active = ?, last_checked_at = ?
                   WHERE id = ?""",
                (
                    float(last_price) if last_price is not None else None,
                    1 if active else 0,
                    self.now().isoformat(),
                    int(watch_id),
                ),
            )

    def cancel_price_watch(self, watch_id: int, user_id: int | None = None) -> bool:
        uid = self._resolve_user_id(user_id)
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE price_watches SET active = 0 WHERE id = ? AND user_id = ? AND active = 1",
                (int(watch_id), uid),
            )
        return bool(cur.rowcount)

    # --- Autonomous Student Curriculum (study_topics) ---

    @staticmethod
    def _row_to_study_topic(row: sqlite3.Row) -> dict[str, Any]:
        return StudyTopic(
            id=int(row["id"]),
            user_id=int(row["user_id"]),
            subject=str(row["subject"]),
            level=int(row["level"]),
            lessons_completed=int(row["lessons_completed"]),
            next_subtopic=str(row["next_subtopic"] or ""),
            status=str(row["status"] or "active"),
            created_at=str(row["created_at"]),
            last_studied_at=row["last_studied_at"],
        ).to_dict()

    def enroll_study_topic(
        self,
        subject: str,
        next_subtopic: str = "",
        user_id: int | None = None,
    ) -> dict[str, Any]:
        uid = self._resolve_user_id(user_id)
        clean_subj = subject.strip()
        if not clean_subj:
            raise ValueError("Study subject cannot be empty.")
        now_iso = self.now().isoformat()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM study_topics WHERE user_id = ? AND LOWER(subject) = LOWER(?)",
                (uid, clean_subj),
            ).fetchone()
            if existing:
                sub = next_subtopic.strip() or existing["next_subtopic"]
                conn.execute(
                    """UPDATE study_topics
                       SET status = 'active', next_subtopic = ?
                       WHERE id = ?""",
                    (sub, existing["id"]),
                )
                row = conn.execute(
                    "SELECT * FROM study_topics WHERE id = ?", (existing["id"],)
                ).fetchone()
                return self._row_to_study_topic(row)

            cur = conn.execute(
                """INSERT INTO study_topics(
                       user_id, subject, level, lessons_completed, next_subtopic, status, created_at
                   ) VALUES (?, ?, 1, 0, ?, 'active', ?)""",
                (uid, clean_subj, next_subtopic.strip(), now_iso),
            )
            topic_id = int(cur.lastrowid)
            row = conn.execute(
                "SELECT * FROM study_topics WHERE id = ?", (topic_id,)
            ).fetchone()
        self.audit("study_topic_enrolled", f"#{topic_id} {clean_subj}", user_id=uid)
        return self._row_to_study_topic(row)

    def list_study_topics(
        self, status: str = "active", user_id: int | None = None
    ) -> list[dict[str, Any]]:
        uid = self._resolve_user_id(user_id)
        with self._connect() as conn:
            if status and status != "all":
                rows = conn.execute(
                    """SELECT * FROM study_topics
                       WHERE user_id = ? AND status = ?
                       ORDER BY id ASC""",
                    (uid, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM study_topics WHERE user_id = ? ORDER BY id ASC",
                    (uid,),
                ).fetchall()
        return [self._row_to_study_topic(r) for r in rows]

    def get_study_topic(
        self, topic_id: int, user_id: int | None = None
    ) -> dict[str, Any] | None:
        uid = self._resolve_user_id(user_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM study_topics WHERE id = ? AND user_id = ?",
                (int(topic_id), uid),
            ).fetchone()
        return self._row_to_study_topic(row) if row else None

    def pick_next_study_topic(
        self, user_id: int | None = None
    ) -> dict[str, Any] | None:
        """Pick the active study topic that was studied least recently (or never)."""
        uid = self._resolve_user_id(user_id)
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM study_topics
                   WHERE user_id = ? AND status = 'active'
                   ORDER BY last_studied_at IS NOT NULL, last_studied_at ASC, id ASC
                   LIMIT 1""",
                (uid,),
            ).fetchone()
        return self._row_to_study_topic(row) if row else None

    def record_study_lesson(
        self,
        topic_id: int,
        next_subtopic: str = "",
        advance_level: bool = True,
        user_id: int | None = None,
    ) -> dict[str, Any] | None:
        uid = self._resolve_user_id(user_id)
        now_iso = self.now().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM study_topics WHERE id = ? AND user_id = ?",
                (int(topic_id), uid),
            ).fetchone()
            if not row:
                return None
            lessons = int(row["lessons_completed"]) + 1
            curr_level = int(row["level"])
            new_level = min(5, curr_level + 1) if advance_level else curr_level
            conn.execute(
                """UPDATE study_topics
                   SET lessons_completed = ?, level = ?, next_subtopic = ?, last_studied_at = ?
                   WHERE id = ?""",
                (lessons, new_level, next_subtopic.strip(), now_iso, int(topic_id)),
            )
            updated = conn.execute(
                "SELECT * FROM study_topics WHERE id = ?", (int(topic_id),)
            ).fetchone()
        self.audit(
            "study_lesson_completed",
            f"#{topic_id} lesson={lessons} level={new_level}",
            user_id=uid,
        )
        return self._row_to_study_topic(updated) if updated else None

    def set_study_topic_status(
        self, topic_id: int, status: str, user_id: int | None = None
    ) -> bool:
        uid = self._resolve_user_id(user_id)
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE study_topics SET status = ? WHERE id = ? AND user_id = ?",
                (status.strip().lower(), int(topic_id), uid),
            )
        return bool(cur.rowcount)

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
