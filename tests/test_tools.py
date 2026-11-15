"""Tests for tool argument validation, SSRF protection, and Windows-safe formatting."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tools
from database.repository import DatabaseRepository
from tools.base import ToolContext
from tools.web import check_url_safe


class ToolValidationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test_tools.db"
        self.repo = DatabaseRepository(self.db_path)
        self.repo.init()
        self.p1 = patch("tools.productivity.db", self.repo)
        self.p2 = patch("tools.memory_tools.db", self.repo)
        self.p3 = patch("tools.integrations.db", self.repo)
        self.p4 = patch("tools.tracker.db", self.repo)
        self.p1.start()
        self.p2.start()
        self.p3.start()
        self.p4.start()
        self.addCleanup(self.p1.stop)
        self.addCleanup(self.p2.stop)
        self.addCleanup(self.p3.stop)
        self.addCleanup(self.p4.stop)
        self.addCleanup(self.tmp.cleanup)
        self.ctx = ToolContext(bot=None, chat_id=777, user_id=777)

    async def test_unknown_tool_returns_message(self):
        res = await tools.run_tool(self.ctx, "does_not_exist", {})
        self.assertIn("Unknown tool", res)

    async def test_missing_required_arg_rejected(self):
        res = await tools.run_tool(self.ctx, "web_search", {})
        self.assertIn("Missing required parameter", res)

    async def test_unknown_arg_rejected(self):
        res = await tools.run_tool(self.ctx, "web_search", {"query": "test", "bogus": 1})
        self.assertIn("Unknown parameter", res)

    async def test_invalid_integer_rejected(self):
        res = await tools.run_tool(self.ctx, "complete_task", {"task_id": "not-an-int"})
        self.assertIn("must be an integer", res)

    async def test_invalid_enum_rejected(self):
        res = await tools.run_tool(
            self.ctx, "add_task", {"title": "Test", "priority": "urgent"}
        )
        self.assertIn("must be one of", res)

    async def test_windows_safe_reminder_formatting(self):
        res = await tools.run_tool(
            self.ctx, "set_reminder", {"minutes": 10, "message": "Stretch"}
        )
        self.assertIn("Reminder #1 set for", res)

    async def test_ssrf_blocks_localhost_and_private_ips(self):
        with self.assertRaises(ValueError):
            await check_url_safe("http://127.0.0.1:8080/admin")
        with self.assertRaises(ValueError):
            await check_url_safe("file:///etc/passwd")
        with self.assertRaises(ValueError):
            await check_url_safe("http://169.254.169.254/latest/meta-data/")

    async def test_confirmation_flow(self):
        prep = await tools.run_tool(
            self.ctx,
            "gmail_prepare_email",
            {"to": "a@example.com", "subject": "Hi", "body": "Test"},
        )
        self.assertIn("Confirmation required", prep)
        bad = await tools.run_tool(self.ctx, "confirm_action", {"token": "wrong"})
        self.assertIn("No matching or active pending action", bad)

    async def test_expense_and_habit_tools(self):
        exp = await tools.run_tool(
            self.ctx,
            "log_expense",
            {"amount": 320, "currency": "BDT", "category": "food", "note": "Lunch"},
        )
        self.assertIn("Logged expense #1", exp)
        rep = await tools.run_tool(self.ctx, "expense_report", {"days": 30})
        self.assertIn("320.0 BDT", rep)

        hab = await tools.run_tool(
            self.ctx,
            "log_habit",
            {"habit": "reading", "value": 25, "unit": "pages"},
        )
        self.assertIn("Logged habit #1", hab)
        h_rep = await tools.run_tool(self.ctx, "habit_report", {"days": 7})
        self.assertIn("reading: 25.0 pages", h_rep)

    async def test_youtube_id_extraction_and_pc_status(self):
        from tools.youtube import extract_video_id

        self.assertEqual(
            extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
            "dQw4w9WgXcQ",
        )
        self.assertEqual(
            extract_video_id("https://youtu.be/dQw4w9WgXcQ?t=10"),
            "dQw4w9WgXcQ",
        )
        with patch("config.OWNER_ID", 777):
            status = await tools.run_tool(self.ctx, "pc_status", {})
            self.assertIn("Host OS:", status)


if __name__ == "__main__":
    unittest.main()
