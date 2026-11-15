"""Modular tool registry with strict JSON-schema argument validation."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Callable, Coroutine

log = logging.getLogger("jarvis.tools")

MAX_TOOL_CHARS = 2500
REGISTRY: dict[str, dict[str, Any]] = {}


@dataclass(slots=True)
class ToolContext:
    """Execution context passed to every tool."""

    bot: Any = None
    chat_id: int = 0
    job_queue: Any = None
    user_id: int | None = None


# Alias for backward compatibility
Ctx = ToolContext


def tool(
    name: str,
    description: str,
    props: dict[str, dict[str, Any]] | None = None,
    required: tuple[str, ...] | list[str] = (),
):
    """Register an async tool function with its parameter schema."""

    def deco(fn: Callable[..., Coroutine[Any, Any, Any]]):
        REGISTRY[name] = {
            "fn": fn,
            "schema": {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": props or {},
                        "required": list(required),
                    },
                },
            },
        }
        return fn

    return deco


def schemas() -> list[dict[str, Any]]:
    return [entry["schema"] for entry in REGISTRY.values()]


def _coerce_value(key: str, val: Any, prop: dict[str, Any]) -> tuple[Any, str | None]:
    expected = prop.get("type")
    if expected == "string":
        if not isinstance(val, str):
            return None, f"'{key}' must be a string, got {type(val).__name__}"
    elif expected == "integer":
        if isinstance(val, bool):
            return None, f"'{key}' must be an integer, got bool"
        if isinstance(val, int):
            pass
        elif isinstance(val, float) and val.is_integer():
            val = int(val)
        elif isinstance(val, str):
            try:
                val = int(val.strip())
            except ValueError:
                return None, f"'{key}' must be an integer, got '{val}'"
        else:
            return None, f"'{key}' must be an integer, got {type(val).__name__}"
    elif expected == "number":
        if isinstance(val, bool):
            return None, f"'{key}' must be a number, got bool"
        if isinstance(val, (int, float)):
            val = float(val)
        elif isinstance(val, str):
            try:
                val = float(val.strip())
            except ValueError:
                return None, f"'{key}' must be a number, got '{val}'"
        else:
            return None, f"'{key}' must be a number, got {type(val).__name__}"
        if not math.isfinite(val):
            return None, f"'{key}' must be a finite number"
    elif expected == "boolean":
        if isinstance(val, bool):
            pass
        elif isinstance(val, str) and val.strip().lower() in ("true", "false"):
            val = val.strip().lower() == "true"
        else:
            return None, f"'{key}' must be a boolean, got {type(val).__name__}"
    elif expected == "array":
        if not isinstance(val, list):
            return None, f"'{key}' must be an array, got {type(val).__name__}"
        item_schema = prop.get("items") or {}
        if item_schema:
            coerced_list = []
            for idx, item in enumerate(val):
                coerced_item, err = _coerce_value(f"{key}[{idx}]", item, item_schema)
                if err:
                    return None, err
                coerced_list.append(coerced_item)
            val = coerced_list

    enum_values = prop.get("enum")
    if enum_values is not None and val not in enum_values:
        return None, f"'{key}' must be one of {enum_values}, got {val!r}"

    return val, None


def validate_args(schema: dict[str, Any], raw_args: Any) -> tuple[dict[str, Any], str | None]:
    """Validate and coerce tool arguments against the tool's JSON schema."""
    if raw_args is None:
        args: dict[str, Any] = {}
    elif isinstance(raw_args, dict):
        args = raw_args
    else:
        return {}, f"Arguments must be an object, got {type(raw_args).__name__}"

    params = schema.get("function", {}).get("parameters", {})
    props: dict[str, dict[str, Any]] = params.get("properties", {})
    required: list[str] = params.get("required", [])

    unknown = [k for k in args if k not in props]
    if unknown:
        return {}, f"Unknown parameter(s): {', '.join(unknown)}"

    for req in required:
        if req not in args or args[req] is None:
            return {}, f"Missing required parameter: '{req}'"

    validated: dict[str, Any] = {}
    for key, val in args.items():
        if val is None:
            continue
        coerced, err = _coerce_value(key, val, props[key])
        if err:
            return {}, err
        validated[key] = coerced

    return validated, None


async def run_tool(ctx: ToolContext, name: str, args: Any) -> str:
    entry = REGISTRY.get(name)
    if not entry:
        return f"Unknown tool: {name}"

    validated, err = validate_args(entry["schema"], args)
    if err:
        log.warning("Tool argument validation failed: tool=%s error=%s", name, err)
        return f"Invalid arguments for {name}: {err}"

    try:
        out = await entry["fn"](ctx, **validated)
    except Exception as exc:
        log.warning("Tool execution failed: tool=%s error=%s", name, type(exc).__name__, exc_info=True)
        out = f"Tool error ({type(exc).__name__}): {exc}"
    return str(out)[:MAX_TOOL_CHARS]
