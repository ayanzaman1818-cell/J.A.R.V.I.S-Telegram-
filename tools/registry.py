"""Unified tool registry module re-exporting all registered JARVIS tools (including desktop computer-use)."""

from __future__ import annotations

import tools  # Ensure all tool modules (including tools.desktop) are imported & registered
from tools.base import (
    MAX_TOOL_CHARS,
    REGISTRY,
    TOOL_TIMEOUT_SECONDS,
    Ctx,
    ToolContext,
    run_tool,
    schemas,
    tool,
    validate_args,
)

__all__ = [
    "Ctx",
    "MAX_TOOL_CHARS",
    "REGISTRY",
    "TOOL_TIMEOUT_SECONDS",
    "ToolContext",
    "run_tool",
    "schemas",
    "tool",
    "tools",
    "validate_args",
]
