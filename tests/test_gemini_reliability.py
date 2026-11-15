"""Simulated Gemini timeout, 503/504, fallback, loop guard, and concurrency tests."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import config
from brain import agent, gemini, history
from database.repository import DatabaseRepository
from tools.base import ToolContext


class _Response:
    def __init__(self, text: str = "", calls=None):
        self.text = text
        self.function_calls = calls or []
        self.candidates = []


class _Call:
    def __init__(self, name: str, args: dict, call_id: str = "call-1"):
        self.name = name
        self.args = args
        self.id = call_id


class _ProviderError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class GeminiReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test_jarvis.db"
        self.repo = DatabaseRepository(self.db_path)
        self.repo.init()

        self.db_patch1 = patch("brain.agent.db", self.repo)
        self.db_patch2 = patch("memory.db", self.repo)
        self.db_patch1.start()
        self.db_patch2.start()
        self.addCleanup(self.db_patch1.stop)
        self.addCleanup(self.db_patch2.stop)
        self.addCleanup(self.tmp.cleanup)

        self.ctx = ToolContext(bot=None, chat_id=4242, user_id=4242)
        agent.HISTORY.pop(self.ctx.chat_id, None)
        gemini._primary_down_until = 0.0
        self.calls: list[str] = []

    def _install(self, generate):
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = generate
        self.client_patch = patch.object(gemini, "get_client", return_value=mock_client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.backoff_patch = patch.object(gemini, "_backoff_seconds", return_value=0)
        self.backoff_patch.start()
        self.addCleanup(self.backoff_patch.stop)
        self.timeout_patch = patch.object(
            gemini, "_request_timeout_seconds", return_value=0.05
        )
        self.timeout_patch.start()
        self.addCleanup(self.timeout_patch.stop)

    async def _ask(self, generate, text: str = "Why are apples sometimes green?") -> str:
        self._install(generate)
        return await agent.respond(self.ctx, text)

    async def test_plain_answer_persists_to_sqlite(self):
        async def generate(*, model, contents, config):
            self.calls.append(model)
            return _Response("Chlorophyll.")

        reply = await self._ask(generate)
        self.assertEqual(reply, "Chlorophyll.")
        self.assertEqual(self.calls, [config.LLM_FAST_MODEL])

        # Simulate bot restart: clear in-memory cache and verify SQLite reloads it
        agent.HISTORY.pop(self.ctx.chat_id, None)
        reloaded = agent._get_history(self.ctx.chat_id)
        self.assertEqual(len(reloaded), 2)
        self.assertEqual(reloaded[0]["role"], "user")
        self.assertEqual(reloaded[1]["content"], "Chlorophyll.")

    async def test_primary_timeout_skips_retry_and_uses_fallback(self):
        async def generate(*, model, contents, config):
            self.calls.append(model)
            if model != globals()["config"].LLM_FALLBACK_MODEL:
                await asyncio.sleep(5)
            return _Response("Answer from backup.")

        reply = await self._ask(generate)
        self.assertEqual(reply, "Answer from backup.")
        self.assertEqual(
            self.calls,
            [config.LLM_FAST_MODEL, config.LLM_FALLBACK_MODEL],
        )

    async def test_primary_503_retries_then_uses_fallback(self):
        async def generate(*, model, contents, config):
            self.calls.append(model)
            if model != globals()["config"].LLM_FALLBACK_MODEL:
                raise _ProviderError(503, "Service Unavailable")
            return _Response("Recovered after 503.")

        reply = await self._ask(generate)
        self.assertEqual(reply, "Recovered after 503.")
        self.assertEqual(
            self.calls,
            [config.LLM_FAST_MODEL] * config.GEMINI_RETRY_ATTEMPTS
            + [config.LLM_FALLBACK_MODEL],
        )

    async def test_both_models_fail_rolls_back_turn(self):
        async def generate(*, model, contents, config):
            self.calls.append(model)
            raise _ProviderError(503, "Service Unavailable")

        reply = await self._ask(generate)
        self.assertIn("Gemini is unavailable", reply)
        self.assertIn(config.LLM_FALLBACK_MODEL, reply)
        self.assertEqual(
            self.calls.count(config.LLM_FAST_MODEL), config.GEMINI_RETRY_ATTEMPTS
        )
        self.assertEqual(
            self.calls.count(config.LLM_FALLBACK_MODEL),
            config.GEMINI_FALLBACK_RETRIES,
        )
        self.assertEqual(agent.HISTORY[self.ctx.chat_id], [])

    async def test_auth_error_is_not_retried_or_fallen_back(self):
        async def generate(*, model, contents, config):
            self.calls.append(model)
            raise _ProviderError(401, "API key not valid")

        reply = await self._ask(generate)
        self.assertIn("API key", reply)
        self.assertEqual(self.calls, [config.LLM_FAST_MODEL])

    async def test_deadline_skips_second_primary_attempt_and_pauses(self):
        async def generate(*, model, contents, config):
            self.calls.append(model)
            if model != globals()["config"].LLM_FALLBACK_MODEL:
                raise _ProviderError(504, "DEADLINE_EXCEEDED")
            return _Response("Answer after deadline.")

        reply = await self._ask(generate)
        self.assertEqual(reply, "Answer after deadline.")
        self.assertEqual(
            self.calls, [config.LLM_FAST_MODEL, config.LLM_FALLBACK_MODEL]
        )
        self.assertGreater(gemini._primary_down_until, 0)

        self.calls.clear()
        reply = await self._ask(generate)
        self.assertEqual(reply, "Answer after deadline.")
        self.assertEqual(self.calls, [config.LLM_FALLBACK_MODEL])

    async def test_rate_limit_is_not_treated_as_outage(self):
        async def generate(*, model, contents, config):
            self.calls.append(model)
            raise _ProviderError(429, "Resource exhausted")

        reply = await self._ask(generate)
        self.assertIn("rate-limiting", reply)
        self.assertEqual(self.calls, [config.LLM_FAST_MODEL])

    async def test_multi_tool_history_survives(self):
        steps = {"n": 0}

        async def generate(*, model, contents, config):
            self.calls.append(model)
            steps["n"] += 1
            if steps["n"] == 1:
                return _Response("", [_Call("web_search", {"query": "green apples"})])
            if steps["n"] == 2:
                return _Response(
                    "", [_Call("read_page", {"url": "https://example.com"})]
                )
            return _Response("Apples are green before they ripen.")

        with patch.object(
            agent.tools,
            "run_tool",
            new=AsyncMock(side_effect=["search hit", "page text"]),
        ):
            reply = await self._ask(generate)

        self.assertEqual(reply, "Apples are green before they ripen.")
        hist = agent.HISTORY[self.ctx.chat_id]
        roles = [m["role"] for m in hist]
        self.assertEqual(
            roles, ["user", "assistant", "tool", "assistant", "tool", "assistant"]
        )
        system, contents = history.history_to_gemini(
            [{"role": "system", "content": "sys"}] + hist
        )
        self.assertEqual(system, "sys")
        tool_turns = [
            item
            for item in contents
            if item["role"] == "user"
            and any("functionResponse" in p for p in item.get("parts", []))
        ]
        self.assertEqual(len(tool_turns), 2)
        self.assertIn(
            "search hit",
            tool_turns[0]["parts"][0]["functionResponse"]["response"]["result"],
        )

    async def test_infinite_tool_loop_is_stopped(self):
        async def generate(*, model, contents, config):
            return _Response("", [_Call("web_search", {"query": "same query"})])

        with patch.object(
            agent.tools, "run_tool", new=AsyncMock(return_value="some result")
        ) as mock_run:
            reply = await self._ask(generate)

        self.assertIn("circles", reply)
        self.assertEqual(mock_run.await_count, 1)

    async def test_slow_request_yields_the_event_loop(self):
        seen: list[str] = []

        async def generate(*, model, contents, config):
            seen.append("model-start")
            await asyncio.sleep(0.03)
            seen.append("model-end")
            return _Response("done")

        async def other_work():
            await asyncio.sleep(0.01)
            seen.append("telegram-other")

        self._install(generate)
        await asyncio.gather(
            agent.respond(self.ctx, "hello"), other_work()
        )
        self.assertLess(seen.index("telegram-other"), seen.index("model-end"))


if __name__ == "__main__":
    unittest.main()
