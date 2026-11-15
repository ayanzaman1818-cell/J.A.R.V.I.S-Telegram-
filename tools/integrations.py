"""Google Workspace tools (Gmail, Drive, Calendar) via Composio with SQLite-backed confirmation flow."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any

import config
from database.repository import db
from tools.base import ToolContext, tool

log = logging.getLogger("jarvis.composio")


class ComposioUnavailable(RuntimeError):
    pass


def composio_enabled() -> bool:
    return bool(config.COMPOSIO_API_KEY) and not config.COMPOSIO_API_KEY.startswith("ck_")


@lru_cache(maxsize=1)
def _client():
    if not config.COMPOSIO_API_KEY:
        raise ComposioUnavailable(
            "Composio is not configured. Add COMPOSIO_API_KEY to .env."
        )
    if config.COMPOSIO_API_KEY.startswith("ck_"):
        raise ComposioUnavailable(
            "COMPOSIO_API_KEY is a Connect consumer key (ck_*). This bot needs an ak_* "
            "project API key from Composio: Settings > Project Settings > API Keys."
        )
    try:
        from composio import Composio
    except ImportError as exc:
        raise ComposioUnavailable(
            "Composio SDK is not installed. Run: pip install -r requirements.txt"
        ) from exc
    return Composio(
        api_key=config.COMPOSIO_API_KEY,
        toolkit_versions={
            "gmail": "latest",
            "googledrive": "latest",
            "googlecalendar": "latest",
        },
    )


def _account(toolkit: str) -> str | None:
    return {
        "gmail": config.COMPOSIO_GMAIL_ACCOUNT,
        "googledrive": config.COMPOSIO_GOOGLEDRIVE_ACCOUNT,
        "googlecalendar": config.COMPOSIO_GOOGLECALENDAR_ACCOUNT,
    }.get(toolkit) or None


def _execute_sync(toolkit: str, slug: str, arguments: dict[str, Any]):
    kwargs: dict[str, Any] = {
        "slug": slug,
        "arguments": arguments,
        "dangerously_skip_version_check": True,
    }
    if config.COMPOSIO_USER_ID:
        kwargs["user_id"] = config.COMPOSIO_USER_ID
    account = _account(toolkit)
    if account:
        kwargs["connected_account_id"] = account
    result = _client().tools.execute(**kwargs)
    if hasattr(result, "model_dump"):
        return result.model_dump(exclude_none=True)
    if hasattr(result, "dict"):
        return result.dict(exclude_none=True)
    return result


async def execute(toolkit: str, slug: str, arguments: dict[str, Any]) -> str:
    """Execute a Composio tool with a hard timeout and return bounded JSON."""
    timeout = float(config.COMPOSIO_TIMEOUT_SECONDS)
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_execute_sync, toolkit, slug, arguments),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        log.warning(
            "Composio tool timed out: toolkit=%s slug=%s timeout=%.0fs",
            toolkit,
            slug,
            timeout,
        )
        return f"Google {toolkit} request timed out after {int(timeout)}s. Please try again."
    except ComposioUnavailable as exc:
        return str(exc)
    except Exception as exc:
        log.warning(
            "Composio tool failed: toolkit=%s slug=%s error=%s",
            toolkit,
            slug,
            type(exc).__name__,
        )
        return f"Google {toolkit} error ({type(exc).__name__}): {exc}"
    return json.dumps(result, ensure_ascii=False, default=str)[:6000]


def clear_pending(chat_id: int) -> None:
    db.clear_pending_actions(chat_id)


def _queue_action(
    ctx: ToolContext, kind: str, payload: dict[str, Any], summary: str
) -> str:
    token = secrets.token_urlsafe(8)
    user_id = ctx.user_id or ctx.chat_id or config.OWNER_ID or 0
    db.clear_pending_actions(ctx.chat_id)
    db.create_pending_action(
        token=token,
        user_id=user_id,
        chat_id=ctx.chat_id,
        kind=kind,
        payload=payload,
        summary=summary,
        ttl_seconds=900,
    )
    return f"Confirmation required. {summary} Reply yes to proceed (confirmation token: {token})."


# ---------------------------------------------------------------- Gmail & Drive
@tool(
    "gmail_search",
    "Search the owner's Gmail. Read-only. Use Gmail search syntax such as is:unread or from:person@example.com.",
    {
        "query": {"type": "string"},
        "max_results": {"type": "integer", "description": "1 to 20; default 10."},
    },
    ["query"],
)
async def gmail_search(ctx: ToolContext, query: str, max_results: int = 10) -> str:
    max_results = max(1, min(int(max_results), 20))
    return await execute(
        "gmail",
        "GMAIL_FETCH_EMAILS",
        {
            "query": query,
            "max_results": max_results,
            "verbose": False,
            "include_payload": False,
        },
    )


@tool(
    "gmail_read",
    "Read one Gmail message by an ID returned from gmail_search. Read-only.",
    {"message_id": {"type": "string"}},
    ["message_id"],
)
async def gmail_read(ctx: ToolContext, message_id: str) -> str:
    return await execute(
        "gmail",
        "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
        {"message_id": message_id, "format": "full"},
    )


@tool(
    "gmail_prepare_email",
    "Prepare an email for confirmation. This does not send it.",
    {
        "to": {"type": "string"},
        "subject": {"type": "string"},
        "body": {"type": "string"},
    },
    ["to", "subject", "body"],
)
async def gmail_prepare_email(
    ctx: ToolContext, to: str, subject: str, body: str
) -> str:
    return _queue_action(
        ctx,
        "gmail_send",
        {"to": to, "subject": subject, "body": body},
        f"Send email to {to} with subject '{subject}'.",
    )


@tool(
    "drive_search",
    "Search the owner's Google Drive. Read-only. Use a name or a Google Drive query.",
    {
        "query": {"type": "string"},
        "max_results": {"type": "integer", "description": "1 to 50; default 20."},
    },
    ["query"],
)
async def drive_search(ctx: ToolContext, query: str, max_results: int = 20) -> str:
    max_results = max(1, min(int(max_results), 50))
    return await execute(
        "googledrive",
        "GOOGLEDRIVE_FIND_FILE",
        {"q": query, "pageSize": max_results},
    )


@tool(
    "drive_create_text_prepare",
    "Prepare a plain-text Google Drive file for confirmation. This does not create it.",
    {
        "file_name": {"type": "string"},
        "text_content": {"type": "string"},
        "parent_id": {"type": "string", "description": "Optional Drive folder ID."},
    },
    ["file_name", "text_content"],
)
async def drive_create_text_prepare(
    ctx: ToolContext,
    file_name: str,
    text_content: str,
    parent_id: str | None = None,
) -> str:
    payload: dict[str, Any] = {"file_name": file_name, "text_content": text_content}
    if parent_id:
        payload["parent_id"] = parent_id
    return _queue_action(
        ctx,
        "drive_create_text",
        payload,
        f"Create Google Drive file '{file_name}'.",
    )


# ---------------------------------------------------------------- Calendar
def _calendar_window(days: int = 1) -> tuple[str, str]:
    start = datetime.now(config.get_zoneinfo()).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return start.isoformat(), (start + timedelta(days=days)).isoformat()


def _calendar_search_window(query: str, days: int) -> tuple[str, str]:
    tz = config.get_zoneinfo()
    now = datetime.now(tz)
    match = re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})\b",
        query,
        re.IGNORECASE,
    )
    if match:
        months = {
            name.lower(): index
            for index, name in enumerate(
                (
                    "January",
                    "February",
                    "March",
                    "April",
                    "May",
                    "June",
                    "July",
                    "August",
                    "September",
                    "October",
                    "November",
                    "December",
                ),
                1,
            )
        }
        try:
            start = datetime(
                now.year,
                months[match.group(1).lower()],
                int(match.group(2)),
                tzinfo=tz,
            )
            return start.isoformat(), (start + timedelta(days=1)).isoformat()
        except ValueError:
            pass
    return now.isoformat(), (now + timedelta(days=days)).isoformat()


@tool("calendar_today", "List today's Google Calendar events. Read-only.")
async def calendar_today(ctx: ToolContext) -> str:
    start, end = _calendar_window()
    return await execute(
        "googlecalendar",
        "GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS",
        {
            "time_min": start,
            "time_max": end,
            "single_events": True,
            "response_detail": "minimal",
            "max_results_per_calendar": 50,
        },
    )


@tool(
    "calendar_search",
    "Search Google Calendar for appointments, holidays, availability, or event names. Read-only. "
    "Use an empty query to list all events in the time range.",
    {
        "query": {"type": "string"},
        "days": {"type": "integer", "description": "Days ahead to search, 1 to 365; default 30."},
    },
)
async def calendar_search(ctx: ToolContext, query: str = "", days: int = 30) -> str:
    days = max(1, min(int(days), 365))
    start, end = _calendar_search_window(query, days)
    arguments: dict[str, Any] = {
        "time_min": start,
        "time_max": end,
        "single_events": True,
        "response_detail": "minimal",
        "max_results_per_calendar": 250,
    }
    if query and not re.search(
        r"\b(holiday|holidays|january|february|march|april|may|june|july|august|september|october|november|december|\d{1,2})\b",
        query,
        re.I,
    ):
        arguments["q"] = query
    return await execute(
        "googlecalendar",
        "GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS",
        arguments,
    )


@tool(
    "calendar_prepare_event",
    "Prepare a Google Calendar event for confirmation. start and end must be RFC3339 timestamps with timezone offsets.",
    {
        "title": {"type": "string"},
        "start": {"type": "string"},
        "end": {"type": "string"},
        "description": {"type": "string"},
        "attendees": {"type": "array", "items": {"type": "string"}},
    },
    ["title", "start", "end"],
)
async def calendar_prepare_event(
    ctx: ToolContext,
    title: str,
    start: str,
    end: str,
    description: str = "",
    attendees: list[str] | None = None,
) -> str:
    payload = {
        "summary": title,
        "start_datetime": start,
        "end_datetime": end,
        "timezone": config.TIMEZONE,
        "description": description,
        "attendees": attendees or [],
        "send_updates": "none",
    }
    return _queue_action(
        ctx,
        "calendar_create",
        payload,
        f"Create calendar event '{title}' from {start} to {end}.",
    )


@tool(
    "confirm_action",
    "Execute the single pending email send, Drive file creation, or Calendar event creation "
    "only after the user explicitly says yes. Pass the exact confirmation token.",
    {"token": {"type": "string"}},
    ["token"],
)
async def confirm_action(ctx: ToolContext, token: str) -> str:
    pending = db.pop_pending_action(ctx.chat_id, token)
    if not pending:
        return "No matching or active pending action. Ask the user to prepare the action again."
    kind = pending.kind
    if kind == "gmail_send":
        return await execute("gmail", "GMAIL_SEND_EMAIL", pending.payload)
    if kind == "drive_create_text":
        return await execute(
            "googledrive", "GOOGLEDRIVE_CREATE_FILE_FROM_TEXT", pending.payload
        )
    if kind == "calendar_create":
        return await execute(
            "googlecalendar", "GOOGLECALENDAR_CREATE_EVENT", pending.payload
        )
    return "Unknown pending action."
