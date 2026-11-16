"""Tests for SQLite storage, WAL pragmas, reminders, stale recovery, tasks, and conversation persistence."""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from database.repository import DatabaseRepository


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.repo = DatabaseRepository(self.db_path)
        self.repo.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_memories_crud(self):
        self.assertTrue(self.repo.remember("Likes black coffee", user_id=101))
        self.assertTrue(self.repo.remember("Lives in Dhaka", user_id=101))
        items = self.repo.memories(user_id=101)
        self.assertEqual(len(items), 2)
        self.assertEqual(self.repo.memories("coffee", user_id=101), ["Likes black coffee"])
        deleted = self.repo.forget("coffee", user_id=101)
        self.assertEqual(deleted, 1)
        self.assertEqual(self.repo.memories(user_id=101), ["Lives in Dhaka"])

    def test_reminders_claim_complete_cancel_and_stale_recovery(self):
        past = self.repo.now() - timedelta(minutes=1)
        rid = self.repo.create_reminder(123, past, "Take vitamins", user_id=123)
        due = self.repo.due_reminders()
        self.assertEqual(len(due), 1)
        self.assertTrue(self.repo.claim_reminder(rid))
        self.assertFalse(self.repo.claim_reminder(rid))

        # Simulate stale 'sending' state older than 5 minutes
        stale_time = (self.repo.now() - timedelta(minutes=10)).isoformat()
        with self.repo._connect() as conn:
            conn.execute(
                "UPDATE reminders SET claimed_at = ? WHERE id = ?", (stale_time, rid)
            )
        recovered = self.repo.due_reminders()
        self.assertEqual(len(recovered), 1)
        self.assertTrue(self.repo.claim_reminder(rid))

        self.repo.complete_reminder(recovered[0])
        self.assertEqual(self.repo.upcoming_reminders(user_id=123), [])

        future = self.repo.now() + timedelta(hours=2)
        rid2 = self.repo.create_reminder(123, future, "Call mom", user_id=123)
        self.assertTrue(self.repo.cancel_reminder(rid2, user_id=123))
        self.assertFalse(self.repo.cancel_reminder(rid2, user_id=123))

    def test_tasks_lifecycle(self):
        tid = self.repo.add_task("Review PR", priority="high", user_id=123)
        tasks = self.repo.list_tasks(user_id=123)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["title"], "Review PR")
        self.assertTrue(self.repo.complete_task(tid, user_id=123))
        self.assertEqual(self.repo.list_tasks(user_id=123), [])

    def test_conversation_history_append_and_clear(self):
        msgs = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello, sir."},
        ]
        self.repo.append_history_messages(999, msgs, user_id=999)
        loaded = self.repo.load_history_messages(999)
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[1]["content"], "Hello, sir.")
        self.repo.clear_history_messages(999)
        self.assertEqual(self.repo.load_history_messages(999), [])

    def test_expenses_and_habits_and_snooze(self):
        eid = self.repo.add_expense(450.0, currency="BDT", category="groceries", note="Eggs & milk", user_id=501)
        self.assertGreater(eid, 0)
        summary = self.repo.expense_summary(days=30, user_id=501)
        self.assertEqual(summary["totals"][0]["total"], 450.0)
        self.assertTrue(self.repo.delete_expense(eid, user_id=501))

        hid = self.repo.log_habit("workout", value=30, unit="mins", user_id=501)
        self.assertGreater(hid, 0)
        habits = self.repo.habit_summary(days=7, user_id=501)
        self.assertEqual(len(habits), 1)
        self.assertEqual(habits[0]["total_value"], 30.0)

        rid = self.repo.create_reminder(501, self.repo.now(), "Drink water", user_id=501)
        snoozed = self.repo.snooze_reminder(rid, minutes=15, user_id=501)
        self.assertIsNotNone(snoozed)

    def test_budgets_knowledge_and_price_watches(self):
        self.repo.set_budget("food", 1000.0, currency="BDT", user_id=601)
        self.repo.add_expense(850.0, currency="BDT", category="food", note="Dinner", user_id=601)
        alert = self.repo.check_budget_alert("food", currency="BDT", user_id=601)
        self.assertIsNotNone(alert)
        self.assertIn("85%", alert)

        daily = self.repo.daily_expense_series(days=7, user_id=601)
        self.assertEqual(len(daily), 7)
        self.assertEqual(daily[-1]["total"], 850.0)

        self.repo.log_habit("meditation", value=15, unit="mins", user_id=601)
        matrix = self.repo.daily_habit_matrix(days=7, user_id=601)
        self.assertEqual(len(matrix["habits"]), 1)
        self.assertGreaterEqual(matrix["habits"][0]["streak"], 1)

        nid = self.repo.add_knowledge_note(
            title="Arc Reactor Notes",
            content="Palladium core replacement specs",
            tags="stark,engineering",
            user_id=601,
        )
        self.assertGreater(nid, 0)
        notes = self.repo.list_knowledge_notes(tag="stark", user_id=601)
        self.assertEqual(len(notes), 1)
        self.assertTrue(self.repo.delete_knowledge_note(nid, user_id=601))

        wid = self.repo.add_price_watch(
            chat_id=601,
            url="https://example.com/item",
            product_name="RTX GPU",
            target_price=500.0,
            currency="USD",
            last_price=599.0,
            user_id=601,
        )
        watches = self.repo.list_price_watches(user_id=601)
        self.assertEqual(len(watches), 1)
        self.assertTrue(self.repo.cancel_price_watch(wid, user_id=601))
        self.assertEqual(self.repo.list_price_watches(user_id=601), [])


if __name__ == "__main__":
    unittest.main()
