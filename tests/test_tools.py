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

    async def test_focus_timer_data_export_and_tool_timeout(self):
        # 1. Pomodoro / focus timer
        res = await tools.run_tool(
            self.ctx,
            "start_focus_timer",
            {"minutes": 25, "label": "Ship stability upgrade"},
        )
        self.assertIn("25 minutes", res)
        self.assertIn("Ship stability upgrade", res)

        # 2. Data export (expenses, tasks, habits, notes)
        await tools.run_tool(
            self.ctx,
            "log_expense",
            {"amount": 120.5, "currency": "BDT", "category": "coffee", "note": "espresso"},
        )
        await tools.run_tool(
            self.ctx,
            "add_task",
            {"title": "Review backups", "priority": "high"},
        )
        exp_csv = await tools.run_tool(
            self.ctx,
            "export_data",
            {"dataset": "expenses", "days": 30},
        )
        self.assertIn("jarvis_expenses_30d.csv", exp_csv)

        tasks_csv = await tools.run_tool(
            self.ctx,
            "export_data",
            {"dataset": "tasks"},
        )
        self.assertIn("jarvis_tasks.csv", tasks_csv)

        notes_md = await tools.run_tool(
            self.ctx,
            "export_data",
            {"dataset": "notes"},
        )
        self.assertIn("jarvis_notes.md", notes_md)

        # 3. Fish Audio circuit breaker status
        from services import voice

        self.assertEqual(voice._fish_cooldown_for(402), 1800)
        self.assertEqual(voice._fish_cooldown_for(429), 300)

    async def test_agentic_research_goal_planner_and_scheduled_jobs(self):
        from unittest.mock import AsyncMock, patch

        # 1. Autonomous scheduled agent jobs (standing orders)
        sched_res = await tools.run_tool(
            self.ctx,
            "schedule_agent_job",
            {
                "when": "in 20 minutes",
                "instruction": "Check top Hacker News AI stories and my high-priority tasks",
                "recurrence": "weekdays",
            },
        )
        self.assertIn("Autonomous Agent Job", sched_res)
        self.assertIn("repeating weekdays", sched_res)

        jobs_list = await tools.run_tool(self.ctx, "list_agent_jobs", {})
        self.assertIn("Check top Hacker News AI stories", jobs_list)

        # 2. Autonomous goal decomposition planner (plan_goal)
        with patch("tools.agentic.db", self.repo), patch(
            "brain.gemini.chat", new_callable=AsyncMock
        ) as mock_chat:
            mock_chat.return_value = type(
                "R",
                (),
                {
                    "text": '{"summary": "Ship MVP in 3 phases", "tasks": ['
                    '{"title": "Design schema", "priority": "high", "due_at": "today"},'
                    '{"title": "Deploy service", "priority": "normal", "due_at": "Friday"}'
                    "]}",
                    "candidates": [],
                },
            )()
            plan_res = await tools.run_tool(
                self.ctx,
                "plan_goal",
                {"goal": "Launch SaaS MVP", "deadline": "Friday"},
            )
            self.assertIn("Goal Roadmap Created: Launch SaaS MVP", plan_res)
            self.assertIn("Design schema", plan_res)
            self.assertIn("Deploy service", plan_res)

        # 3. Autonomous multi-source deep research (deep_research)
        with patch("tools.agentic.db", self.repo), patch(
            "tools.agentic.web_search",
            new=AsyncMock(return_value="Title\nhttps://example.com/article\nSnippet"),
        ), patch(
            "tools.agentic.read_page",
            new=AsyncMock(return_value="https://example.com/article\n\nDetailed benchmark findings."),
        ), patch(
            "brain.gemini.chat", new_callable=AsyncMock
        ) as mock_chat2:
            mock_chat2.return_value = type(
                "R",
                (),
                {
                    "text": "1. Executive Summary: Rust excels at latency.",
                    "candidates": [],
                },
            )()
            research_res = await tools.run_tool(
                self.ctx,
                "deep_research",
                {"topic": "Rust vs Go latency", "save_to_vault": True},
            )
            self.assertIn("Executive Summary", research_res)
            self.assertIn("Archived to Second Brain", research_res)

        # 4. Autonomous Student / Self-Learning Engine (auto-discovery + syllabus + flashcards)
        with patch("tools.student.db", self.repo), patch(
            "tools.student.web_search",
            new=AsyncMock(return_value="Paper\nhttps://example.com/raft\nConsensus protocol"),
        ), patch(
            "tools.student.read_page",
            new=AsyncMock(return_value="https://example.com/raft\n\nRaft leader election mechanics."),
        ), patch(
            "brain.gemini.chat", new_callable=AsyncMock
        ) as mock_study_chat:
            mock_study_chat.return_value = type(
                "R",
                (),
                {
                    "text": (
                        "1. Core Mental Model: Raft separates leader election and log replication.\n"
                        "2. Flashcard Q: What triggers an election? A: Election timeout.\n"
                        "NEXT_SUBTOPIC: Log compaction and snapshotting in Raft"
                    ),
                    "candidates": [],
                },
            )()
            # Auto-discover when syllabus is empty
            auto_res = await tools.run_tool(self.ctx, "study_next_lesson", {})
            self.assertIn("Autonomous Study Session Complete", auto_res)
            self.assertIn("Saved to Second Brain as Note", auto_res)
            self.assertIn("Log compaction and snapshotting in Raft", auto_res)

            # Enroll a specific topic and check curriculum + flashcards
            enroll_res = await tools.run_tool(
                self.ctx,
                "enroll_study_topic",
                {"subject": "Distributed Consensus", "study_now": True},
            )
            self.assertIn("Distributed Consensus", enroll_res)

            syllabus = await tools.run_tool(self.ctx, "list_study_curriculum", {})
            self.assertIn("Distributed Consensus", syllabus)
            self.assertIn("Level 2", syllabus)

            quiz_res = await tools.run_tool(
                self.ctx,
                "quiz_from_study_notes",
                {"subject": "Distributed Consensus"},
            )
            self.assertIn("Flashcard Review", quiz_res)

    async def test_pc_workstation_automation_suite(self):
        import os
        from unittest.mock import patch
        import config

        with patch.object(config, "OWNER_ID", self.ctx.user_id):
            # 1. Keep-Awake (Caffeine) mode toggle
            caff_on = await tools.run_tool(self.ctx, "pc_stay_awake", {"enabled": True})
            self.assertIn("ENABLED", caff_on)
            caff_off = await tools.run_tool(self.ctx, "pc_stay_awake", {"enabled": False})
            self.assertIn("DISABLED", caff_off)

            # 2. Protected process guard in pc_kill_process
            kill_sys = await tools.run_tool(self.ctx, "pc_kill_process", {"target": "explorer.exe"})
            self.assertIn("Refusing", kill_sys)
            kill_self = await tools.run_tool(
                self.ctx, "pc_kill_process", {"target": str(os.getpid())}
            )
            self.assertIn("Refusing", kill_self)

            # 3. Auto-organize Downloads/Inbox folder
            inbox_dir = Path(self.tmp.name) / "inbox_test"
            inbox_dir.mkdir(parents=True, exist_ok=True)
            (inbox_dir / "diagram.png").write_bytes(b"png")
            (inbox_dir / "report.pdf").write_bytes(b"pdf")
            (inbox_dir / "bundle.zip").write_bytes(b"zip")
            with patch("tools.pc_control._resolve_pc_folder", return_value=inbox_dir):
                org_res = await tools.run_tool(
                    self.ctx, "pc_organize_downloads", {"folder": "inbox"}
                )
                self.assertIn("Organized 3 file(s)", org_res)
                self.assertTrue((inbox_dir / "Images" / "diagram.png").exists())
                self.assertTrue((inbox_dir / "Documents" / "report.pdf").exists())
                self.assertTrue((inbox_dir / "Archives" / "bundle.zip").exists())

            # 4. Network info & allowlisted diagnostics
            net_res = await tools.run_tool(self.ctx, "pc_network_info", {})
            self.assertIn("Hostname:", net_res)
            diag_res = await tools.run_tool(
                self.ctx, "pc_run_diagnostic", {"command": "python_env"}
            )
            self.assertIn("Diagnostic (python_env)", diag_res)

    async def test_desktop_companion_vision_and_computer_use(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        import config
        from brain.history import history_to_gemini
        from services.desktop_overlay import DesktopOverlayService
        from tools import desktop, registry

        # Verify tools/registry.py exposes all computer-use tools
        for tool_name in (
            "see_screen",
            "desktop_list_windows",
            "desktop_focus_window",
            "desktop_click",
            "desktop_type",
            "desktop_hotkey",
            "desktop_scroll",
        ):
            self.assertIn(tool_name, registry.REGISTRY)

        # Verify pyautogui safety flags
        if desktop.pyautogui is not None:
            self.assertTrue(desktop.pyautogui.FAILSAFE)
            self.assertAlmostEqual(desktop.pyautogui.PAUSE, 0.15)

        with patch.object(config, "OWNER_ID", self.ctx.user_id), patch(
            "tools.desktop.db", self.repo
        ):
            # 1. see_screen captures JPEG bytes (<=1600px) and attaches them to ToolContext for multimodal Gemini
            see_out = await tools.run_tool(self.ctx, "see_screen", {"target": "monitor"})
            self.assertIn("Screen captured", see_out)
            media = self.ctx.consume_media()
            self.assertEqual(len(media), 1)
            jpeg_bytes, mime = media[0]
            self.assertEqual(mime, "image/jpeg")
            self.assertTrue(jpeg_bytes.startswith(b"\xff\xd8"))

            # Verify history_to_gemini translates mid-loop tool _media into Gemini multimodal Part
            _, gem_contents = history_to_gemini(
                [
                    {"role": "user", "content": "Inspect my screen"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-see-1",
                                "function": {"name": "see_screen", "arguments": "{}"},
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call-see-1",
                        "name": "see_screen",
                        "content": see_out,
                        "_media": [(jpeg_bytes, "image/jpeg")],
                    },
                ]
            )
            self.assertEqual(len(gem_contents[-1]["parts"]), 2)

            # 2. FAILSAFE (0, 0) guard on desktop_click
            fs_click = await tools.run_tool(self.ctx, "desktop_click", {"x": 0, "y": 0})
            self.assertIn("FAILSAFE", fs_click)

            # 3. Password & card credential guard on desktop_type
            pw_block = await tools.run_tool(
                self.ctx, "desktop_type", {"text": "password: MySecretPassword123"}
            )
            self.assertIn("Security Block", pw_block)

            # 4. Confirmation gate for form submission (press_enter=True) and destructive hotkeys (win+r)
            with patch("tools.desktop._active_window_title", return_value="Browser"), patch(
                "tools.desktop._type_sync", return_value="Typed 'hello world' and pressed Enter."
            ):
                type_confirm = await tools.run_tool(
                    self.ctx,
                    "desktop_type",
                    {"text": "hello world", "press_enter": True},
                )
                self.assertIn("Confirmation required", type_confirm)
                pending = self.repo.get_latest_pending_action(self.ctx.chat_id)
                self.assertIsNotNone(pending)
                exec_res = await tools.run_tool(
                    self.ctx, "confirm_action", {"token": pending.token}
                )
                self.assertIn("Typed 'hello world'", exec_res)

            hk_confirm = await tools.run_tool(
                self.ctx, "desktop_hotkey", {"keys": ["win", "r"]}
            )
            self.assertIn("Confirmation required", hk_confirm)

            # 5. DesktopOverlayService shares brain.agent.respond() and OWNER_ID
            overlay = DesktopOverlayService(owner_id=self.ctx.user_id)
            initial_sugg = overlay.fetch_live_suggestions()
            self.assertEqual(len(initial_sugg), 4)
            # Mark one suggestion done -> removed from suggestions list
            overlay.mark_suggestion_done(initial_sugg[0]["id"])
            self.assertEqual(len(overlay.fetch_live_suggestions()), 3)
            # Mark all remaining suggestions done -> empty list -> triggers Composio Connections Hub tab
            for s in list(initial_sugg):
                overlay.mark_suggestion_done(s["id"])
            self.assertEqual(len(overlay.fetch_live_suggestions()), 0)
            hub_cards = overlay.fetch_composio_hub_cards()
            self.assertGreaterEqual(len(hub_cards), 4)

            with patch(
                "brain.agent.respond", new=AsyncMock(return_value="Overlay reply OK")
            ) as mock_resp:
                reply = await overlay.submit_query_async(
                    "What is on my screen?", auto_see=True, speak_reply=False
                )
                self.assertEqual(reply, "Overlay reply OK")
                self.assertEqual(mock_resp.await_count, 1)
                _, kwargs = mock_resp.call_args
                self.assertEqual(kwargs["mime_type"], "image/jpeg")
                self.assertIsNotNone(kwargs["media_bytes"])

    async def test_twilio_two_way_voice_call_webhook(self):
        from unittest.mock import AsyncMock, patch
        import config
        from services import twilio_webhook, voice

        with patch.object(config, "OWNER_ID", self.ctx.user_id), patch.object(
            config, "MY_PHONE", "+8801712345678"
        ), patch.object(
            config, "TWILIO_WEBHOOK_URL", "https://jarvis.example.ngrok-free.app/twilio/voice"
        ), patch.object(
            config, "TWILIO_USE_CUSTOM_TTS", False
        ):
            # 1. Initial inbound pickup (no SpeechResult yet) -> greets owner and opens <Gather input="speech">
            twiml_greet = await twilio_webhook.handle_twilio_voice_turn(
                {"CallSid": "CA111", "From": "+8801712345678", "To": "+15550001111"}
            )
            self.assertIn('<Gather input="speech"', twiml_greet)
            self.assertIn("https://jarvis.example.ngrok-free.app/twilio/voice", twiml_greet)
            self.assertIn("How may I assist you?", twiml_greet)

            # 2. User speaks something -> routes to brain.agent.respond() -> replies and loops <Gather>
            with patch(
                "brain.agent.respond",
                new=AsyncMock(return_value="It is currently 28 degrees and clear in Dhaka, sir."),
            ) as mock_brain:
                twiml_turn = await twilio_webhook.handle_twilio_voice_turn(
                    {
                        "CallSid": "CA111",
                        "From": "+8801712345678",
                        "To": "+15550001111",
                        "SpeechResult": "What is the weather like right now?",
                    }
                )
                self.assertEqual(mock_brain.await_count, 1)
                self.assertIn("28 degrees and clear in Dhaka", twiml_turn)
                self.assertIn('<Gather input="speech"', twiml_turn)

            # 3. User says goodbye -> speaks farewell and emits <Hangup/>
            twiml_bye = await twilio_webhook.handle_twilio_voice_turn(
                {
                    "CallSid": "CA111",
                    "From": "+8801712345678",
                    "To": "+15550001111",
                    "SpeechResult": "That is all, goodbye",
                }
            )
            self.assertIn("<Hangup/>", twiml_bye)

            # 4. Unauthorized caller number -> rejected with <Hangup/>
            twiml_unauth = await twilio_webhook.handle_twilio_voice_turn(
                {
                    "CallSid": "CA999",
                    "From": "+19998887777",
                    "To": "+15550001111",
                    "SpeechResult": "Unlock the PC",
                }
            )
            self.assertIn("Unauthorized", twiml_unauth)
            self.assertIn("<Hangup/>", twiml_unauth)

            # 5. Custom TTS audio cache round-trip for <Play>
            tok = voice.cache_twilio_audio(b"ID3-fake-mp3-bytes")
            self.assertEqual(voice.get_twilio_audio(tok), b"ID3-fake-mp3-bytes")

            # 6. 100% Free Web Voice Call turn (Option 2 — browser / Telegram phone call UI)
            with patch(
                "brain.agent.respond",
                new=AsyncMock(return_value="All systems nominal, sir."),
            ), patch(
                "services.twilio_webhook._synthesize_call_audio_path",
                new=AsyncMock(return_value="/twilio/audio/mock_tok.mp3"),
            ):
                web_greet = await twilio_webhook.handle_web_call_turn(is_greeting=True)
                self.assertIn("on the line", web_greet["reply"])
                self.assertEqual(web_greet["audio_url"], "/twilio/audio/mock_tok.mp3")

                web_turn = await twilio_webhook.handle_web_call_turn(
                    user_text="Check system status"
                )
                self.assertEqual(web_turn["heard"], "Check system status")
                self.assertEqual(web_turn["reply"], "All systems nominal, sir.")
                self.assertFalse(web_turn["hangup"])

                web_bye = await twilio_webhook.handle_web_call_turn(
                    user_text="Goodbye Jarvis"
                )
                self.assertTrue(web_bye["hangup"])

            # 7. Native 1-on-1 Telegram VoIP Call PCM & WAV pipeline
            from services import telegram_call

            silent_pcm = b"\x00\x00" * 1920
            loud_pcm = b"\x00\x20" * 1920
            self.assertEqual(telegram_call._pcm_rms(silent_pcm), 0.0)
            self.assertGreater(telegram_call._pcm_rms(loud_pcm), 1000.0)
            wav_hdr = telegram_call._pcm_to_wav_bytes(loud_pcm)
            self.assertTrue(wav_hdr.startswith(b"RIFF"))


if __name__ == "__main__":
    unittest.main()
