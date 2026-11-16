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
        self.p5 = patch("tools.charts.db", self.repo)
        self.p6 = patch("tools.knowledge.db", self.repo)
        self.p7 = patch("tools.watchdog.db", self.repo)
        self.p8 = patch("services.voice.db", self.repo)
        for p in (self.p1, self.p2, self.p3, self.p4, self.p5, self.p6, self.p7, self.p8):
            p.start()
            self.addCleanup(p.stop)
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

    async def test_visual_png_charts_and_budgets(self):
        await tools.run_tool(
            self.ctx,
            "set_budget",
            {"category": "food", "monthly_limit": 500, "currency": "BDT"},
        )
        exp_res = await tools.run_tool(
            self.ctx,
            "log_expense",
            {"amount": 450, "currency": "BDT", "category": "food", "note": "Biryani"},
        )
        self.assertIn("Budget alert", exp_res)

        b_rep = await tools.run_tool(self.ctx, "budget_report", {})
        self.assertIn("450.00 / 500.00 BDT", b_rep)

        e_chart = await tools.run_tool(self.ctx, "expense_chart", {"days": 30})
        self.assertIn("Sent 30-day expense chart", e_chart)

        await tools.run_tool(
            self.ctx, "log_habit", {"habit": "coding", "value": 2, "unit": "hours"}
        )
        h_chart = await tools.run_tool(self.ctx, "habit_chart", {"days": 14})
        self.assertIn("Sent 14-day habit streak chart", h_chart)

    async def test_second_brain_knowledge_vault(self):
        saved = await tools.run_tool(
            self.ctx,
            "save_note",
            {
                "title": "Mark XLII Suit Calibration",
                "content": "Micro-repeater propulsion thrusters require 4.2 GW repulsor output.",
                "tags": "marvel,ironman",
            },
        )
        self.assertIn("Saved to Second Brain #1", saved)

        found = await tools.run_tool(
            self.ctx, "search_knowledge", {"query": "repulsor thrusters"}
        )
        self.assertIn("Mark XLII Suit Calibration", found)

        listed = await tools.run_tool(self.ctx, "list_notes", {"tag": "marvel"})
        self.assertIn("Mark XLII Suit Calibration", listed)

    async def test_price_watchdog_and_voice_mode(self):
        from tools.watchdog import extract_price_from_html

        html = '<meta property="product:price:amount" content="149.99" />'
        self.assertEqual(extract_price_from_html(html), 149.99)
        self.assertEqual(extract_price_from_html("Sale price: $89.50 today!"), 89.50)

        v_res = await tools.run_tool(
            self.ctx,
            "set_voice_chat_mode",
            {"enabled": True, "voice_preset": "marvel"},
        )
        self.assertIn("enabled", v_res)
        self.assertIn("en-GB-RyanNeural", v_res)

        from services import voice

        matched = voice.match_openpeon_clip("Welcome home, sir.")
        self.assertIsNotNone(matched)
        clip_res = await tools.run_tool(
            self.ctx,
            "jarvis_voice_clip",
            {"category": "session.start", "query": "welcome home"},
        )
        self.assertIn("Welcome home, sir.", clip_res)


if __name__ == "__main__":
    unittest.main()
