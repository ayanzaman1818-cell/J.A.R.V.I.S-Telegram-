"""Google Gemini client with bounded retries, timeouts, circuit breaker, and fallback."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import Any

from google import genai
from google.genai import types

import config
from brain.history import history_to_gemini

log = logging.getLogger("jarvis.brain.gemini")

PRIMARY_COOLDOWN_SECONDS = 180
PRIMARY_MAX_COOLDOWN_SECONDS = 3600
_GEMINI_SLOTS: asyncio.Semaphore | None = None
_client: genai.Client | None = None
_primary_down_until: float = 0.0
# Consecutive primary failures. While > 0 the breaker is open: user requests go
# straight to the fallback model and a background probe decides when to close it,
# so users never pay the primary model's timeout while it is degraded.
_primary_failures: int = 0
_probe_task: asyncio.Task | None = None
_STATS: dict[str, dict[str, Any]] = {}


def _record(model: str, ok: bool, elapsed: float) -> None:
    """Keep rolling per-model success/failure counts and recent latencies for /health."""
    from collections import deque

    entry = _STATS.setdefault(
        model, {"ok": 0, "fail": 0, "latencies": deque(maxlen=50), "last_error": ""}
    )
    if ok:
        entry["ok"] += 1
        entry["latencies"].append(elapsed)
    else:
        entry["fail"] += 1


def model_stats() -> dict[str, dict[str, Any]]:
    """Return a JSON-friendly snapshot of per-model reliability stats."""
    out: dict[str, dict[str, Any]] = {}
    for model, entry in _STATS.items():
        lat = sorted(entry["latencies"])
        out[model] = {
            "ok": entry["ok"],
            "fail": entry["fail"],
            "median_s": round(lat[len(lat) // 2], 2) if lat else None,
            "last_error": entry["last_error"],
        }
    return out


def _slots() -> asyncio.Semaphore:
    global _GEMINI_SLOTS
    if _GEMINI_SLOTS is None:
        _GEMINI_SLOTS = asyncio.Semaphore(2)
    return _GEMINI_SLOTS


def get_client() -> genai.Client | None:
    global _client
    if _client is not None:
        return _client
    if not config.GEMINI_API_KEY:
        return None
    timeout_ms = max(5, int(config.LLM_TIMEOUT_SECONDS)) * 1000
    _client = genai.Client(
        api_key=config.GEMINI_API_KEY,
        http_options=types.HttpOptions(
            timeout=timeout_ms,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )
    return _client


class GeminiFailure(Exception):
    """Primary model failed, and the fallback model failed too when one was tried."""

    def __init__(
        self,
        primary: Exception,
        fallback: Exception | None = None,
        fallback_model: str = "",
    ):
        self.primary = primary
        self.fallback = fallback
        self.fallback_model = fallback_model
        super().__init__(type(primary).__name__)


def _request_timeout_seconds(fast_fail: bool = False) -> float:
    """Per-request timeout. fast_fail caps the primary model when a fallback is ready."""
    full = float(max(5, int(config.LLM_TIMEOUT_SECONDS)))
    if fast_fail:
        return min(full, float(max(3, int(config.LLM_PRIMARY_TIMEOUT_SECONDS))))
    return full


def _backoff_seconds(attempt: int) -> float:
    base = min(1.0 * (2**attempt), 4.0)
    return base * (0.5 + random.random())


def _error_status(exc: Exception) -> int | None:
    value = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_detail(exc: Exception) -> str:
    text = str(exc).replace("\n", " ")
    text = re.sub(r"(key=)[^&\s]+", r"\1[redacted]", text, flags=re.I)
    text = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot[redacted]", text)
    return text[:240]


def _is_deadline(exc: Exception) -> bool:
    return _error_status(exc) == 504 or "deadline" in str(exc).lower()


def _is_timeout(exc: Exception) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    name = type(exc).__name__.lower()
    return "timeout" in name or "timed out" in str(exc).lower()


def _is_rate_limit(exc: Exception) -> bool:
    status = _error_status(exc)
    message = str(exc).lower()
    return status == 429 or any(
        marker in message
        for marker in ("resource_exhausted", "rate limit", "quota exceeded", "too many requests")
    )


def _is_auth_error(exc: Exception) -> bool:
    status = _error_status(exc)
    message = str(exc).lower()
    return status in (401, 403) or any(
        marker in message
        for marker in ("api_key", "api key", "unauthenticated", "permission denied", "unauthorized")
    )


def _is_client_error(exc: Exception) -> bool:
    status = _error_status(exc)
    message = str(exc).lower()
    return (
        status in (400, 404, 413, 422)
        or "request too large" in message
        or "invalid argument" in message
    )


def failure_kind(exc: Exception) -> str:
    if _is_rate_limit(exc):
        return "rate_limit"
    if _is_auth_error(exc):
        return "auth"
    if _is_timeout(exc):
        return "timeout"
    if _is_client_error(exc):
        return "client"
    status = _error_status(exc)
    message = str(exc).lower()
    if status in (500, 502, 503, 504) or any(
        marker in message
        for marker in (
            "internal error",
            "internal server",
            "temporarily unavailable",
            "bad gateway",
            "gateway timeout",
            "service unavailable",
        )
    ):
        return "transient"
    return "unknown"


def _is_transient_server_error(exc: Exception) -> bool:
    return failure_kind(exc) in ("timeout", "transient")


def _primary_paused() -> bool:
    return time.time() < _primary_down_until


def _cooldown_for(failures: int) -> int:
    """Exponential cooldown: 3m, 6m, 12m, 24m, then capped at 60m."""
    return int(min(PRIMARY_COOLDOWN_SECONDS * (2 ** max(0, failures - 1)), PRIMARY_MAX_COOLDOWN_SECONDS))


def _note_primary_failure(model: str) -> None:
    global _primary_down_until, _primary_failures
    if not model or model == config.LLM_FALLBACK_MODEL:
        return
    _primary_failures += 1
    cooldown = _cooldown_for(_primary_failures)
    _primary_down_until = time.time() + cooldown
    log.warning(
        "Primary Gemini model paused for %ss after a deadline or outage: model=%s consecutive_failures=%s",
        cooldown,
        model,
        _primary_failures,
    )


def _note_primary_success() -> None:
    global _primary_down_until, _primary_failures
    if _primary_failures:
        log.info("Primary Gemini model recovered after %s failure(s)", _primary_failures)
    _primary_down_until = 0.0
    _primary_failures = 0


def _breaker_open() -> bool:
    """True while the primary is paused or awaiting a successful background probe."""
    return _primary_paused() or _primary_failures > 0


_DEEP_THINKING_RE = re.compile(
    r"\b(architect|debug|refactor|prove|derive|step[- ]by[- ]step|trade[- ]?offs|"
    r"root cause|algorithm|optimize|why does|compare in depth)\b",
    re.I,
)


def _thinking_config_for(
    model: str,
    user_text: str = "",
    *,
    force_low: bool = False,
) -> types.ThinkingConfig | None:
    """Select the lowest supported thinking mode for routine turns, scaling up only for complex queries.
    Returns None for models (such as Gemma 4) that do not accept thinking_config.
    """
    m_low = (model or "").lower()
    if "gemma" in m_low:
        return None
    text = (user_text or "").strip()
    is_deep = (not force_low) and (len(text) > 450 or bool(_DEEP_THINKING_RE.search(text)))

    if "2.5" in m_low:
        return types.ThinkingConfig(thinking_budget=1024 if is_deep else 0)

    if is_deep:
        level = getattr(config, "LLM_DEEP_THINKING_LEVEL", "MEDIUM") or "MEDIUM"
        if level == "MINIMAL" and "lite" not in m_low:
            level = "LOW"
        return types.ThinkingConfig(thinking_level=level)

    # Routine turn or post-tool summarization: use MINIMAL on flash-lite, LOW on standard flash
    if "lite" in m_low:
        return types.ThinkingConfig(thinking_level="MINIMAL")
    base_lvl = getattr(config, "LLM_THINKING_LEVEL", "LOW") or "LOW"
    if base_lvl == "MINIMAL":
        base_lvl = "LOW"
    return types.ThinkingConfig(thinking_level=base_lvl)


async def _probe_primary(model: str) -> None:
    """Send a tiny request to the primary model off the user's critical path (with LOW thinking)."""
    try:
        probe_cfg: dict[str, Any] = {
            "max_output_tokens": 8,
            "temperature": 0.0,
        }
        tc = _thinking_config_for(model, force_low=True)
        if tc is not None:
            probe_cfg["thinking_config"] = tc
        await _generate(
            model,
            [{"role": "user", "parts": [{"text": "Reply with OK."}]}],
            types.GenerateContentConfig(**probe_cfg),
            attempts=1,
            timeout=_request_timeout_seconds(fast_fail=True),
        )
        _note_primary_success()
    except Exception as exc:
        if _is_transient_server_error(exc):
            _note_primary_failure(model)
        else:
            log.info("Primary probe inconclusive: kind=%s", failure_kind(exc))


def _maybe_start_probe(model: str) -> None:
    """Kick off one background probe once the cooldown has elapsed."""
    global _probe_task
    if _primary_paused() or (_probe_task and not _probe_task.done()):
        return
    try:
        _probe_task = asyncio.get_running_loop().create_task(_probe_primary(model))
    except RuntimeError:
        _probe_task = None


def model_health() -> dict[str, Any]:
    remaining = max(0, int(_primary_down_until - time.time()))
    return {
        "primary": config.LLM_MODEL,
        "fast": config.LLM_FAST_MODEL,
        "deep": config.LLM_DEEP_MODEL,
        "fallback": config.LLM_FALLBACK_MODEL,
        "local_base_url": getattr(config, "LOCAL_LLM_BASE_URL", "http://127.0.0.1:1234/v1"),
        "primary_paused_seconds": remaining,
        "primary_failures": _primary_failures,
        "probing": bool(_probe_task and not _probe_task.done()),
        "stats": model_stats(),
    }


MODEL_PRESETS: dict[str, str] = {
    "jarvis7b": "lmstudio:jarvis-qwen-7b",
    "jarvis": "lmstudio:jarvis-qwen-7b",
    "qwen38b": "lmstudio:qwen3-8b",
    "qwen8b": "lmstudio:qwen3-8b",
    "gemma26b": "gemma-4-26b-a4b-it",
    "gemma31b": "gemma-4-31b-it",
    "flashlite": "gemini-3.5-flash-lite",
    "flash38": "gemini-3.8-flash",
    "bonsai27b": "lmstudio:bonsai-27b",
    "bonsaiternary": "lmstudio:ternary-bonsai-27b",
    "lmstudio": "lmstudio:auto",
    "qwen7b": "lmstudio:qwen2.5-7b",
    "qwen3b": "lmstudio:qwen2.5-3b",
}


def is_local_model(model: str | None) -> bool:
    """Return True if the model string targets a local LM Studio / llama.cpp / OpenAI-compatible server."""
    m = (model or "").strip().lower()
    return m.startswith(("lmstudio:", "local:", "ollama:", "llamacpp:", "bonsai:"))


def _persist_env_model(resolved: str) -> None:
    """Persist LLM_MODEL, LLM_FAST_MODEL, and LLM_DEEP_MODEL in .env so model choice survives restarts."""
    try:
        env_path = config.BASE_DIR / ".env"
        if not env_path.exists():
            return
        text = env_path.read_text(encoding="utf-8")
        lines = text.splitlines()
        updated_keys: set[str] = set()
        new_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                new_lines.append(line)
                continue
            k, _ = stripped.split("=", 1)
            k_clean = k.strip()
            if k_clean in ("LLM_MODEL", "LLM_FAST_MODEL", "LLM_DEEP_MODEL"):
                new_lines.append(f"{k_clean}={resolved}")
                updated_keys.add(k_clean)
            else:
                new_lines.append(line)
        for k_req in ("LLM_MODEL", "LLM_FAST_MODEL", "LLM_DEEP_MODEL"):
            if k_req not in updated_keys:
                new_lines.append(f"{k_req}={resolved}")
        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    except Exception as exc:
        log.debug("Could not persist active model to .env: %s", exc)


def set_active_model(model_or_preset: str) -> str:
    """Switch JARVIS's primary/fast/deep LLM model live at runtime (supports Cloud Gemini/Gemma & Local LM Studio)."""
    global _primary_down_until, _primary_failures
    key = (model_or_preset or "").strip()
    resolved = MODEL_PRESETS.get(key.lower(), key)
    if not resolved:
        return config.LLM_MODEL
    config.LLM_MODEL = resolved
    config.LLM_FAST_MODEL = resolved
    config.LLM_DEEP_MODEL = resolved
    _primary_down_until = 0.0
    _primary_failures = 0
    _persist_env_model(resolved)
    log.info("Switched active JARVIS LLM model to: %s", resolved)
    return resolved


class _LocalFunctionCall:
    def __init__(self, name: str, args: dict[str, Any], call_id: str) -> None:
        self.name = name
        self.args = args
        self.id = call_id


class _LocalChatResponse:
    """Adapter that makes an OpenAI/LM Studio ChatCompletion look identical to a google-genai response."""

    def __init__(self, text: str, function_calls: list[_LocalFunctionCall]) -> None:
        self.text = text
        self.function_calls = function_calls
        self.candidates: list[Any] = []


def _local_system_prompt(query: str = "", user_id: int | None = None) -> str:
    """Build a compact but live-grounded system prompt for local LM Studio models, including Second Brain & casual reading recall."""
    from datetime import datetime
    now = datetime.now(config.get_zoneinfo()).strftime("%A %d %B %Y, %H:%M %Z")
    base = (
        f"You are J.A.R.V.I.S., an ultra-capable personal AI assistant with understated, dry British wit "
        f"(composed, eloquent, subtly deadpan — think Paul Bettany's J.A.R.V.I.S.). "
        f"Address the user as 'Sir' in nearly every reply (as a poised British butler-AI would), "
        f"never use generic AI filler like 'Sure! I'd be happy to help!' "
        f"or excessive exclamation marks, and keep replies crisp with occasional dry humor. "
        f"Current date and time: {now}. User's home location: {config.LOCATION}. "
        "IMPORTANT: When the user asks you to perform an action (play/pause/skip music, change volume, "
        "start a timer, set a reminder, check weather, open an app, etc.), ALWAYS call the provided tool "
        "instead of claiming you did it in plain text. "
        "Always prioritize live tool results, web facts, and your Second Brain notes over outdated memory."
    )
    extra_blocks: list[str] = []
    try:
        from tools.knowledge import quick_knowledge_context
        uid = user_id or config.OWNER_ID
        if query:
            matched_notes = quick_knowledge_context(query, user_id=uid, limit=2)
            if matched_notes:
                extra_blocks.append(f"Relevant notes from your Second Brain:\n{matched_notes}")
        # Also include the 2 most recent Casual Reading world-knowledge cards so Qwen stays naturally well-read
        recent_reads = db.list_knowledge_notes(limit=2, tag="casual-reading", user_id=uid)
        if recent_reads:
            read_lines = [
                f"- {n['title']}: {str(n.get('content', '')).replace(chr(10), ' ')[:280]}"
                for n in recent_reads
            ]
            extra_blocks.append("Recent books/articles you casually read to stay current:\n" + "\n".join(read_lines))
    except Exception:
        pass
    if extra_blocks:
        return base + "\n\n" + "\n\n".join(extra_blocks)
    return base


def _extract_auto_grounding_topic(user_query: str) -> str | None:
    """Detect if a prompt asks about or writes about a real-world entity/topic that needs live web grounding."""
    import re
    q = (user_query or "").strip()
    if len(q) < 10:
        return None
    q_low = q.lower()
    # Skip purely personal / local PC / meta greetings
    if any(
        q_low.startswith(p)
        for p in ("hi ", "hello", "hey ", "confirm", "yes", "no", "cancel", "lock my", "mute", "unmute", "set volume")
    ):
        return None
    # Pattern 1: "write/send/draft an email ... about <topic>"
    m = re.search(
        r"\b(?:email|mail|write|draft|tell|explain|summary|summarize|review|thoughts|update)\b.*?\b(?:about|on|regarding)\s+([A-Za-z0-9][A-Za-z0-9\s:.\-']{2,60})",
        q,
        flags=re.IGNORECASE,
    )
    if m:
        topic = m.group(1).strip(" .?!,;")
        # Strip trailing instructions like "to john@..."
        topic = re.sub(r"\s+\bto\s+\S+@\S+.*$", "", topic, flags=re.IGNORECASE).strip()
        if len(topic) >= 3:
            return topic

    # Pattern 2: Factual / current-events questions ("when did X come out", "is X out yet", "latest on X", "who won X", "what happened with X")
    if re.search(
        r"\b(latest|current|news|release|released|launched|came out|come out|out yet|worth it|who won|score|update on|what is|who is|tell me about)\b",
        q_low,
    ):
        return q[:90]
    return None


def _select_relevant_tools_for_local(user_query: str, all_schemas: list[dict[str, Any]], max_tools: int = 8) -> list[dict[str, Any]]:
    """Select only the most relevant tools for a local LLM query so we never blow up LM Studio's 4096-token context window."""
    q = (user_query or "").lower().strip()
    # Simple greetings or pure conversational chat don't need 99 tools stuffed into context
    if len(q) <= 18 and any(
        w == q or q.startswith(w + " ")
        for w in ("hi", "hello", "hey", "yo", "jarvis", "hey jarvis", "thanks", "thank you", "good morning", "good evening", "how are you", "who are you", "what model are you")
    ):
        return []

    keywords_map: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
        (("weather", "temperature", "rain", "forecast"), ("get_weather",)),
        (("time", "timer", "pomodoro", "countdown", "focus", "remind", "reminder", "alarm"), ("start_focus_timer", "set_reminder", "set_reminder_at", "list_reminders")),
        (("task", "todo", "to-do"), ("add_task", "list_tasks", "complete_task")),
        (("calendar", "schedule", "meeting", "event", "holiday"), ("calendar_today", "calendar_search", "calendar_prepare_event", "confirm_action")),
        (("email", "gmail", "inbox", "mail", "send"), ("gmail_search", "gmail_read", "gmail_prepare_email", "confirm_action", "web_search")),
        (("drive", "doc", "file"), ("drive_search", "drive_create_text_prepare", "confirm_action")),
        (("search", "look up", "google", "news", "web", "price", "cost", "worth", "about", "latest", "release", "game", "movie"), ("web_search", "tech_news", "price_lookup", "read_page")),
        (("screen", "monitor", "look", "seeing", "window", "click", "type", "desktop", "open", "launch", "pc", "volume", "mute", "lock", "battery", "screenshot", "clipboard"), ("see_screen", "pc_status", "pc_screenshot", "pc_launch_app", "pc_media_control", "pc_lock", "pc_clipboard_get", "pc_clipboard_set", "desktop_click", "desktop_type")),
        (("spotify", "music", "song", "play", "pause", "skip", "next", "previous", "track", "resume"), ("spotify_play", "spotify_status", "pc_media_control")),
        (("connect", "composio", "github", "notion", "slack", "discord", "trello"), ("composio_list_connections", "composio_connect_app", "composio_execute_action")),
        (("remember", "memory", "forget"), ("remember", "list_memories", "forget_memory")),
        (("note", "knowledge", "archive", "save"), ("save_note", "search_knowledge", "list_notes")),
        (("call", "phone", "speak", "voice"), ("speak", "call_me", "jarvis_voice_clip")),
        (("yes", "confirm", "approve", "send it", "do it"), ("confirm_action",)),
    ]
    wanted_names: list[str] = ["web_search"]
    for triggers, tool_names in keywords_map:
        if any(t in q for t in triggers):
            for tn in tool_names:
                if tn not in wanted_names:
                    wanted_names.append(tn)

    by_name = {s.get("function", {}).get("name"): s for s in all_schemas}
    return [by_name[n] for n in wanted_names[:max_tools] if n in by_name]


async def _generate_local_openai(
    model: str,
    messages: list[dict[str, Any]],
    *,
    use_tools: bool = True,
    timeout: float = 90.0,
) -> _LocalChatResponse:
    """Execute a chat completion against LM Studio (http://127.0.0.1:1234/v1) or another local OpenAI-compatible server."""
    import json
    from openai import AsyncOpenAI
    from tools.base import schemas

    prefix, raw_model = (model.split(":", 1) if ":" in model else ("lmstudio", model))
    p_low = prefix.lower()
    if p_low == "llamacpp":
        base_url = "http://127.0.0.1:8080/v1"
    elif p_low == "ollama":
        base_url = "http://127.0.0.1:11434/v1"
    else:
        base_url = getattr(config, "LOCAL_LLM_BASE_URL", "http://127.0.0.1:1234/v1")
    api_key = getattr(config, "LOCAL_LLM_API_KEY", "lm-studio") or "lm-studio"

    local_timeout = max(float(timeout), 90.0)
    client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=local_timeout, max_retries=0)

    # Auto-resolve the exact loaded model ID in LM Studio if needed (e.g. 'bonsai-27b' or 'auto')
    try:
        models_page = await asyncio.wait_for(client.models.list(), timeout=3.0)
        loaded_ids = [
            getattr(m, "id", "")
            for m in (getattr(models_page, "data", None) or [])
            if getattr(m, "id", "") and "embed" not in getattr(m, "id", "").lower()
        ]
        if loaded_ids:
            hint = raw_model.lower()
            if hint in ("auto", "default", "local-model", ""):
                raw_model = loaded_ids[0]
            elif raw_model not in loaded_ids:
                tokens = [t for t in hint.replace("_", "-").split("-") if len(t) >= 3]
                matched = next(
                    (mid for mid in loaded_ids if hint in mid.lower() or any(t in mid.lower() for t in tokens)),
                    None,
                )
                raw_model = matched or loaded_ids[0]
    except Exception:
        pass

    # Keep only recent conversation messages and use a compact system prompt so we fit inside LM Studio's 4096-token n_ctx
    recent_non_sys = [m for m in messages if m.get("role") != "system"][-8:]
    latest_user_q = next(
        (str(m.get("content", "")) for m in reversed(recent_non_sys) if m.get("role") == "user"),
        "",
    )
    sys_content = _local_system_prompt(query=latest_user_q, user_id=config.OWNER_ID)
    if "qwen3" in raw_model.lower() or "qwen-3" in raw_model.lower():
        # Append /no_think for Qwen3 so it answers directly at 25-35 tok/s without a slow <think> reasoning block
        sys_content += " /no_think"
    oai_messages: list[dict[str, Any]] = [
        {"role": "system", "content": sys_content}
    ]
    user_query = ""
    has_tool_turn = any(m.get("role") == "tool" for m in recent_non_sys)
    for m in recent_non_sys:
        role = m.get("role", "user")
        if role == "user":
            content_str = str(m.get("content", ""))
            user_query = content_str
            oai_messages.append({"role": "user", "content": content_str})
        elif role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": str(m.get("content", "") or "")}
            tcs = m.get("tool_calls") or []
            if tcs:
                entry["tool_calls"] = tcs
            oai_messages.append(entry)
        elif role == "tool":
            oai_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(m.get("tool_call_id", "call_1")),
                    "content": str(m.get("content", ""))[:1500],
                }
            )

    # Automatic Live Web Grounding: if the user asks about or writes an email/summary about a real-world topic
    # and we haven't already run a tool turn, fetch live DuckDuckGo web snippets in <1s and inject them into context!
    if not has_tool_turn and user_query:
        grounding_topic = _extract_auto_grounding_topic(user_query)
        if grounding_topic:
            try:
                from tools.web import web_search
                from tools.base import ToolContext
                snippets = await asyncio.wait_for(
                    web_search(ToolContext(), query=grounding_topic, max_results=3),
                    timeout=5.0,
                )
                if snippets and snippets != "No results.":
                    log.info("Auto-grounded local LLM on topic=%r (%d chars)", grounding_topic, len(snippets))
                    oai_messages[-1]["content"] = (
                        f"{user_query}\n\n"
                        f"[Live Web Search Facts for '{grounding_topic}' — use these up-to-date facts instead of outdated training memory]:\n"
                        f"{snippets[:1200]}"
                    )
                    # Quietly remember what we just read in Second Brain so Qwen permanently retains it over time!
                    try:
                        existing_topics = [
                            n["title"].lower()
                            for n in db.list_knowledge_notes(limit=25, tag="casual-reading", user_id=config.OWNER_ID)
                        ]
                        if not any(grounding_topic.lower() in t for t in existing_topics):
                            db.add_knowledge_note(
                                title=f"Casual Reading: {grounding_topic[:60]}",
                                content=snippets[:1200],
                                tags="casual-reading,auto-learned",
                                user_id=config.OWNER_ID,
                            )
                    except Exception:
                        pass
            except Exception as exc:
                log.debug("Auto-grounding search skipped: %s", exc)

    kwargs: dict[str, Any] = {
        "model": raw_model,
        "messages": oai_messages,
        "temperature": config.LLM_TEMPERATURE,
    }
    selected_tools = _select_relevant_tools_for_local(user_query, schemas()) if use_tools else []
    if selected_tools:
        kwargs["tools"] = selected_tools

    started = time.perf_counter()
    log.info(
        "LM Studio request started: model=%s tools=%d base_url=%s",
        raw_model,
        len(selected_tools),
        base_url,
    )
    try:
        try:
            resp = await asyncio.wait_for(
                client.chat.completions.create(**kwargs),
                timeout=local_timeout,
            )
        except Exception as first_err:
            # Only retry without tools if LM Studio immediately returned a 400 Bad Request on the tool schema
            # (never retry on a TimeoutError, which would queue a second overlapping request on the CPU!)
            if not isinstance(first_err, (asyncio.TimeoutError, TimeoutError)) and (
                "tools" in kwargs or any(m.get("role") == "tool" for m in oai_messages)
            ):
                log.info(
                    "LM Studio rejected tool payload (%s); retrying pure chat on %s",
                    first_err,
                    raw_model,
                )
                kwargs.pop("tools", None)
                clean_msgs = [
                    {"role": m["role"], "content": str(m.get("content", ""))}
                    for m in oai_messages
                    if m.get("role") in ("system", "user", "assistant") and str(m.get("content", "")).strip()
                ]
                kwargs["messages"] = clean_msgs
                resp = await asyncio.wait_for(
                    client.chat.completions.create(**kwargs),
                    timeout=local_timeout,
                )
            else:
                raise
        elapsed = time.perf_counter() - started
        log.info(
            "LM Studio request completed: model=%s elapsed=%.2fs",
            raw_model,
            elapsed,
        )
        _record(model, True, elapsed)
    except Exception as exc:
        _record(model, False, time.perf_counter() - started)
        _STATS[model]["last_error"] = type(exc).__name__
        raise

    choice = resp.choices[0].message
    text = (choice.content or "").strip()
    # Strip any `<think>...</think>` tags if a local reasoning model emits them in content
    if "<think>" in text and "</think>" in text:
        import re as _re
        text = _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL).strip()
    fn_calls: list[_LocalFunctionCall] = []
    for tc in getattr(choice, "tool_calls", None) or []:
        fn = getattr(tc, "function", None)
        if not fn:
            continue
        try:
            parsed_args = json.loads(fn.arguments or "{}")
        except Exception:
            parsed_args = {}
        fn_calls.append(
            _LocalFunctionCall(
                name=fn.name,
                args=parsed_args,
                call_id=getattr(tc, "id", None) or "call_local_1",
            )
        )
    return _LocalChatResponse(text=text, function_calls=fn_calls)


def _public_reason(exc: Exception) -> str:
    kind = failure_kind(exc)
    status = _error_status(exc)
    if kind == "timeout":
        return f"no response within {int(_request_timeout_seconds())}s"
    if kind == "rate_limit":
        return "rate limit"
    if status:
        return f"HTTP {status}"
    return kind


def user_error(exc: Exception) -> str:
    if isinstance(exc, GeminiFailure):
        tried = f" I also tried {exc.fallback_model}." if exc.fallback_model else ""
        return (
            "Gemini is unavailable right now "
            f"({_public_reason(exc.primary)}).{tried} Please try again in a moment."
        )
    kind = failure_kind(exc)
    if kind == "rate_limit":
        return "Google is rate-limiting Gemini requests right now. Please wait a minute and try again."
    if kind == "auth":
        return "Google rejected the API key. Check GEMINI_API_KEY in your .env file."
    if kind == "timeout":
        return f"Gemini did not respond within {int(_request_timeout_seconds())} seconds. Please try again."
    if _error_status(exc) == 413 or "request too large" in str(exc).lower():
        return "That request became too large for the current model. I've trimmed the context; please ask it again."
    if kind == "transient":
        status = _error_status(exc)
        detail = f" (HTTP {status})" if status else ""
        return f"Gemini is temporarily unavailable{detail}. Please try again in a moment."
    return f"Something went wrong on my end ({type(exc).__name__}). Please try again."


def _function_declarations() -> list[dict[str, Any]]:
    from tools.base import schemas

    declarations = []
    for schema in schemas():
        function = schema.get("function", {})
        declarations.append(
            {
                "name": function["name"],
                "description": function.get("description", ""),
                "parameters": function.get(
                    "parameters", {"type": "object", "properties": {}}
                ),
            }
        )
    return declarations


async def _generate(
    model: str,
    contents: list[dict[str, Any]],
    generation_config: types.GenerateContentConfig,
    *,
    attempts: int = 2,
    timeout: float | None = None,
):
    client = get_client()
    if client is None:
        raise RuntimeError(
            "GEMINI_API_KEY is missing. Add your Google AI Studio API key to .env."
        )

    last_error: Exception | None = None
    timeout = timeout if timeout is not None else _request_timeout_seconds()
    for attempt in range(attempts):
        started = time.perf_counter()
        try:
            log.info(
                "Gemini request started: model=%s attempt=%s/%s timeout=%.0fs",
                model,
                attempt + 1,
                attempts,
                timeout,
            )
            async with _slots():
                response = await asyncio.wait_for(
                    client.aio.models.generate_content(
                        model=model,
                        contents=contents,
                        config=generation_config,
                    ),
                    timeout=timeout + min(2.0, timeout),
                )
            log.info(
                "Gemini request completed: model=%s attempt=%s/%s elapsed=%.2fs",
                model,
                attempt + 1,
                attempts,
                time.perf_counter() - started,
            )
            _record(model, True, time.perf_counter() - started)
            return response
        except Exception as exc:
            last_error = exc
            kind = failure_kind(exc)
            _record(model, False, time.perf_counter() - started)
            _STATS[model]["last_error"] = f"{kind} {_error_status(exc) or ''}".strip()
            log.warning(
                "Gemini request failed: model=%s attempt=%s/%s kind=%s status=%s elapsed=%.2fs error=%s detail=%s",
                model,
                attempt + 1,
                attempts,
                kind,
                _error_status(exc),
                time.perf_counter() - started,
                type(exc).__name__,
                _safe_detail(exc),
            )
            if (
                kind not in ("timeout", "transient")
                or _is_timeout(exc)
                or _is_deadline(exc)
                or attempt + 1 >= attempts
            ):
                break
            delay = _backoff_seconds(attempt)
            log.info(
                "Gemini retry scheduled: model=%s next_attempt=%s/%s delay=%.2fs",
                model,
                attempt + 2,
                attempts,
                delay,
            )
            await asyncio.sleep(delay)
    assert last_error is not None
    raise last_error


async def chat(
    messages: list[dict[str, Any]],
    *,
    use_tools: bool = True,
    force_model: str | None = None,
    media_bytes: bytes | None = None,
    mime_type: str | None = None,
    thinking_budget: int | None = None,
):
    global _primary_down_until
    user_text = next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
        "",
    )
    model = force_model or (
        config.LLM_DEEP_MODEL if len(user_text) > 600 else config.LLM_FAST_MODEL
    )

    has_visual_media = bool(
        media_bytes
        and mime_type
        and (mime_type.startswith("image/") or mime_type.startswith("video/"))
    )

    if is_local_model(model):
        # Automatic Vision Bridge: text-only local models (e.g. JARVIS-Qwen-7B) cannot read screenshots/images.
        # Seamlessly route visual turns (see_screen, Auto-See Screen, dropped images) to Gemini Vision,
        # then stay on your local model for all regular turns!
        if has_visual_media and get_client() is not None:
            vision_model = (
                config.LLM_FALLBACK_MODEL
                if (config.LLM_FALLBACK_MODEL and not is_local_model(config.LLM_FALLBACK_MODEL))
                else "gemini-3.5-flash-lite"
            )
            log.info(
                "Auto Vision Bridge: routing visual turn (%s) from local %s -> %s",
                mime_type,
                model,
                vision_model,
            )
            model = vision_model
        else:
            try:
                return await _generate_local_openai(
                    model,
                    messages,
                    use_tools=use_tools,
                    timeout=float(config.LLM_TIMEOUT_SECONDS),
                )
            except Exception as local_err:
                log.warning(
                    "Local LLM (%s) failed (%s); falling back to %s",
                    model,
                    local_err,
                    config.LLM_FALLBACK_MODEL,
                )
                if not config.LLM_FALLBACK_MODEL or is_local_model(config.LLM_FALLBACK_MODEL):
                    raise
                model = config.LLM_FALLBACK_MODEL

    if get_client() is None:
        raise RuntimeError(
            "GEMINI_API_KEY is missing. Add your Google AI Studio API key to .env."
        )

    system_instruction, contents = history_to_gemini(messages)

    if media_bytes and mime_type:
        media_part = types.Part.from_bytes(data=media_bytes, mime_type=mime_type)
        attached = False
        for item in reversed(contents):
            if item.get("role") == "user" and any(
                isinstance(p, dict) and "text" in p for p in item.get("parts", [])
            ):
                item["parts"].append(media_part)
                attached = True
                break
        if not attached:
            contents.append({"role": "user", "parts": [media_part]})

    def _build_gen_config(target_model: str) -> types.GenerateContentConfig:
        cfg: dict[str, Any] = {
            "system_instruction": system_instruction,
            "temperature": config.LLM_TEMPERATURE,
            "max_output_tokens": config.LLM_MAX_OUTPUT_TOKENS,
        }
        tc = _thinking_config_for(
            target_model,
            user_text,
            force_low=(thinking_budget == 0),
        )
        if tc is not None:
            cfg["thinking_config"] = tc
        if use_tools:
            cfg["tools"] = [{"function_declarations": _function_declarations()}]
        return types.GenerateContentConfig(**cfg)

    fallback_model = config.LLM_FALLBACK_MODEL
    has_fallback = bool(fallback_model and model != fallback_model)
    if has_fallback and _breaker_open():
        log.warning(
            "Primary Gemini model still paused; using fallback immediately: fallback=%s",
            fallback_model,
        )
        _maybe_start_probe(model)
        return await _generate(
            fallback_model,
            contents,
            _build_gen_config(fallback_model),
            attempts=config.GEMINI_FALLBACK_RETRIES,
        )

    try:
        response = await _generate(
            model,
            contents,
            _build_gen_config(model),
            attempts=config.GEMINI_RETRY_ATTEMPTS,
            timeout=_request_timeout_seconds(fast_fail=has_fallback),
        )
        _note_primary_success()
        return response
    except Exception as primary_error:
        kind = failure_kind(primary_error)
        if _is_transient_server_error(primary_error):
            _note_primary_failure(model)
        elif kind == "rate_limit" and "gemma" in (model or "").lower():
            # Gemma models have a strict 15k TPM free-tier quota; pause primary for 60s and use Gemini fallback
            global _primary_down_until
            _primary_down_until = max(_primary_down_until, time.time() + 60.0)
        can_fallback = (
            (_is_transient_server_error(primary_error) or (kind == "rate_limit" and "gemma" in (model or "").lower()))
            and fallback_model
            and fallback_model != model
        )
        if not can_fallback:
            log.info(
                "Gemini fallback skipped: model=%s kind=%s fallback=%s",
                model,
                kind,
                fallback_model or "(none)",
            )
            raise primary_error
        log.warning(
            "Primary Gemini model unavailable; trying fallback: primary=%s kind=%s fallback=%s attempts=%s",
            model,
            kind,
            fallback_model,
            config.GEMINI_FALLBACK_RETRIES,
        )
        try:
            return await _generate(
                fallback_model,
                contents,
                _build_gen_config(fallback_model),
                attempts=config.GEMINI_FALLBACK_RETRIES,
            )
        except Exception as fallback_error:
            log.error(
                "Gemini fallback failed: model=%s kind=%s status=%s error=%s detail=%s",
                fallback_model,
                failure_kind(fallback_error),
                _error_status(fallback_error),
                type(fallback_error).__name__,
                _safe_detail(fallback_error),
            )
            raise GeminiFailure(
                primary_error, fallback_error, fallback_model
            ) from fallback_error
