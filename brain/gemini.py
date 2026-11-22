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
        "primary_paused_seconds": remaining,
        "primary_failures": _primary_failures,
        "probing": bool(_probe_task and not _probe_task.done()),
        "stats": model_stats(),
    }


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
    if get_client() is None:
        raise RuntimeError(
            "GEMINI_API_KEY is missing. Add your Google AI Studio API key to .env."
        )

    user_text = next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
        "",
    )
    model = force_model or (
        config.LLM_DEEP_MODEL if len(user_text) > 600 else config.LLM_FAST_MODEL
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
