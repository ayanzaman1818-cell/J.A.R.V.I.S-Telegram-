"""Conversation history trimming and Gemini Content translation."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

log = logging.getLogger("jarvis.brain.history")

MAX_HISTORY = 30
MAX_HISTORY_CHARS = 10_000


def trim_history(
    hist: list[dict[str, Any]],
    max_messages: int = MAX_HISTORY,
    max_chars: int = MAX_HISTORY_CHARS,
) -> None:
    """Trim history in-place while keeping turn boundaries aligned to user messages."""
    while len(hist) > max_messages:
        hist.pop(0)
    while hist and hist[0].get("role") != "user":
        hist.pop(0)

    def _size() -> int:
        return sum(len(json.dumps(m, default=str)) for m in hist)

    while len(hist) > 1 and _size() > max_chars:
        next_user = next(
            (i for i, m in enumerate(hist[1:], 1) if m.get("role") == "user"),
            None,
        )
        if next_user is None:
            break
        del hist[:next_user]
        while hist and hist[0].get("role") != "user":
            hist.pop(0)


def history_to_gemini(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Translate internal message history into Gemini system_instruction and contents."""
    system = ""
    contents: list[dict[str, Any]] = []
    call_names: dict[str, str] = {}

    for message in messages:
        for call in message.get("tool_calls", []) or []:
            call_names[call.get("id", "")] = call.get("function", {}).get("name", "")

    for message in messages:
        role = message.get("role")
        if role == "system":
            system = str(message.get("content", ""))
            continue
        if role == "user":
            contents.append(
                {"role": "user", "parts": [{"text": str(message.get("content", ""))}]}
            )
            continue
        if role == "assistant":
            saved = message.get("_gemini_content")
            if saved:
                contents.append(saved)
                continue
            calls = message.get("tool_calls") or []
            if calls:
                parts = []
                for call in calls:
                    fn = call.get("function", {})
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except (TypeError, json.JSONDecodeError):
                        args = {}
                    function_call: dict[str, Any] = {
                        "name": fn.get("name", ""),
                        "args": args,
                    }
                    if call.get("id"):
                        function_call["id"] = call["id"]
                    parts.append({"functionCall": function_call})
                contents.append({"role": "model", "parts": parts})
            elif message.get("content"):
                contents.append(
                    {"role": "model", "parts": [{"text": str(message["content"])}]}
                )
            continue
        if role == "tool":
            call_id = message.get("tool_call_id", "")
            name = message.get("name") or call_names.get(call_id, "")
            if not name:
                log.warning("Skipping tool result without a matching function name")
                continue
            function_response: dict[str, Any] = {
                "name": name,
                "response": {"result": str(message.get("content", ""))},
            }
            if call_id:
                function_response["id"] = call_id
            part = {"functionResponse": function_response}
            if contents and contents[-1].get("role") == "user" and all(
                "functionResponse" in p for p in contents[-1].get("parts", [])
            ):
                contents[-1]["parts"].append(part)
            else:
                contents.append({"role": "user", "parts": [part]})

    return system, contents


def candidate_content_dict(response: Any, tool_calls: list[dict[str, Any]]) -> dict | None:
    """Preserve Google's model turn, including function-call IDs and thought signatures."""
    try:
        content = response.candidates[0].content
        payload = content.model_dump(mode="json", exclude_none=True, by_alias=True)
        pending = list(tool_calls)
        for part in payload.get("parts", []):
            function_call = part.get("functionCall") or part.get("function_call")
            if not isinstance(function_call, dict):
                continue
            match = next(
                (c for c in pending if c["function"]["name"] == function_call.get("name")),
                None,
            )
            if match:
                pending.remove(match)
                function_call.setdefault("id", match["id"])
        return payload
    except (AttributeError, IndexError, TypeError):
        return None


def response_text(response: Any) -> str:
    """Extract text parts without triggering SDK warnings on function_call responses."""
    candidates = getattr(response, "candidates", None)
    if candidates:
        try:
            parts = candidates[0].content.parts or []
            return "".join(p.text for p in parts if getattr(p, "text", None)).strip()
        except (AttributeError, IndexError, TypeError):
            pass
    return (getattr(response, "text", None) or "").strip()


def response_tool_calls(response: Any) -> list[dict[str, Any]]:
    calls = []
    for function_call in getattr(response, "function_calls", None) or []:
        args = dict(function_call.args or {})
        call_id = getattr(function_call, "id", None) or str(uuid.uuid4())
        calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": function_call.name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            }
        )
    return calls
