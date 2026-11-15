"""JARVIS cognitive brain: Gemini client, conversation history, and autonomous agent."""

from __future__ import annotations

from brain.agent import reset, respond, simple, system_prompt
from brain.gemini import GeminiFailure, chat, failure_kind, get_client, model_health, user_error
from brain.history import history_to_gemini, response_text, response_tool_calls, trim_history

__all__ = [
    "GeminiFailure",
    "chat",
    "failure_kind",
    "get_client",
    "history_to_gemini",
    "model_health",
    "reset",
    "respond",
    "response_text",
    "response_tool_calls",
    "simple",
    "system_prompt",
    "trim_history",
    "user_error",
]
