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


# Connectable Composio apps surfaced in the Desktop Overlay Connections tab & Telegram
POPULAR_COMPOSIO_APPS: list[dict[str, str]] = [
    {
        "toolkit": "notion",
        "name": "Notion",
        "icon": "prism",
        "desc": "Connect Notion workspaces, pages, and databases.",
    },
    {
        "toolkit": "slack",
        "name": "Slack",
        "icon": "cloud",
        "desc": "Read channels, search messages, and send Slack updates.",
    },
    {
        "toolkit": "discord",
        "name": "Discord",
        "icon": "cloud",
        "desc": "Connect Discord servers, channels, and messages.",
    },
    {
        "toolkit": "github",
        "name": "GitHub",
        "icon": "prism",
        "desc": "Repositories, issues, pull requests, and commits.",
    },
    {
        "toolkit": "spotify",
        "name": "Spotify",
        "icon": "cloud",
        "desc": "Control playback, playlists, and saved tracks.",
    },
    {
        "toolkit": "linear",
        "name": "Linear",
        "icon": "prism",
        "desc": "Track engineering issues, cycles, and project roadmaps.",
    },
    {
        "toolkit": "trello",
        "name": "Trello",
        "icon": "calendar",
        "desc": "Manage Trello boards, lists, and project cards.",
    },
    {
        "toolkit": "outlook",
        "name": "Microsoft Outlook",
        "icon": "calendar",
        "desc": "Read and send Outlook emails and calendar events.",
    },
    {
        "toolkit": "youtube",
        "name": "YouTube",
        "icon": "prism",
        "desc": "Manage channels, playlists, and video subscriptions.",
    },
    {
        "toolkit": "figma",
        "name": "Figma",
        "icon": "prism",
        "desc": "Inspect Figma design files, components, and comments.",
    },
    {
        "toolkit": "jira",
        "name": "Jira",
        "icon": "calendar",
        "desc": "Track Jira issues, sprints, and engineering projects.",
    },
    {
        "toolkit": "todoist",
        "name": "Todoist",
        "icon": "calendar",
        "desc": "Sync personal tasks, projects, and productivity labels.",
    },
    {
        "toolkit": "dropbox",
        "name": "Dropbox",
        "icon": "cloud",
        "desc": "Access, search, and manage cloud files in Dropbox.",
    },
    {
        "toolkit": "gmail",
        "name": "Gmail",
        "icon": "cloud",
        "desc": "Read, search, draft, and send emails.",
    },
    {
        "toolkit": "googlecalendar",
        "name": "Google Calendar",
        "icon": "calendar",
        "desc": "Sync meetings, daily schedule, and availability.",
    },
    {
        "toolkit": "googledrive",
        "name": "Google Drive",
        "icon": "prism",
        "desc": "Search files, docs, and save notes to Google Drive.",
    },
]

# Dynamic in-memory cache of active Composio connected account IDs (slug -> ca_xxx)
_CONNECTED_ACCOUNTS_CACHE: dict[str, str] = {}
_CACHE_TS: float = 0.0


def composio_enabled() -> bool:
    return bool(config.COMPOSIO_API_KEY) and not config.COMPOSIO_API_KEY.startswith("ck_")


def _effective_user_id() -> str:
    if config.COMPOSIO_USER_ID:
        return config.COMPOSIO_USER_ID
    return f"jarvis-owner-{config.OWNER_ID or 1}"


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
    return Composio(api_key=config.COMPOSIO_API_KEY)


def get_connected_toolkits_sync(force_refresh: bool = False) -> dict[str, str]:
    """Return a mapping of {toolkit_slug: connected_account_id} for all ACTIVE Composio connections.
    Automatically discovers new connections from Composio cloud and SQLite preferences without requiring .env edits!
    """
    import time

    global _CONNECTED_ACCOUNTS_CACHE, _CACHE_TS
    now = time.time()
    if not force_refresh and _CONNECTED_ACCOUNTS_CACHE and (now - _CACHE_TS < 45.0):
        return dict(_CONNECTED_ACCOUNTS_CACHE)

    mapping: dict[str, str] = {}
    # 1. Seed with any legacy .env overrides if present
    if config.COMPOSIO_GMAIL_ACCOUNT:
        mapping["gmail"] = config.COMPOSIO_GMAIL_ACCOUNT
    if config.COMPOSIO_GOOGLEDRIVE_ACCOUNT:
        mapping["googledrive"] = config.COMPOSIO_GOOGLEDRIVE_ACCOUNT
    if config.COMPOSIO_GOOGLECALENDAR_ACCOUNT:
        mapping["googlecalendar"] = config.COMPOSIO_GOOGLECALENDAR_ACCOUNT

    # 2. Load any saved connected account IDs from SQLite preferences
    try:
        prefs = db.list_preferences(user_id=config.OWNER_ID or None)
        for k, v in prefs.items():
            if k.startswith("composio_ca_") and v:
                slug = k[len("composio_ca_") :].lower()
                mapping[slug] = v
    except Exception:
        pass

    # 3. Query Composio cloud live for all ACTIVE connected accounts on this project/user
    if composio_enabled():
        try:
            uid = _effective_user_id()
            resp = _client().connected_accounts.list(
                user_ids=[uid], statuses=["ACTIVE"]
            )
            for item in getattr(resp, "items", []) or []:
                ca_id = getattr(item, "id", "") or ""
                tk_obj = getattr(item, "toolkit", None)
                slug = (getattr(tk_obj, "slug", "") or "").lower()
                if slug and ca_id:
                    mapping[slug] = ca_id
                    try:
                        db.set_preference(
                            f"composio_ca_{slug}", ca_id, user_id=config.OWNER_ID or None
                        )
                    except Exception:
                        pass
        except Exception:
            log.debug("Composio live connected_accounts.list lookup skipped", exc_info=True)

    _CONNECTED_ACCOUNTS_CACHE = mapping
    _CACHE_TS = now
    return dict(mapping)


def authorize_toolkit_sync(toolkit: str) -> dict[str, str]:
    """Initiate a 1-click Composio v3 OAuth flow (POST /api/v3/connected_accounts/link) for ANY toolkit
    (gmail, github, notion, slack, discord, linear, etc.) and return {'toolkit': slug, 'redirect_url': url, 'connected_account_id': id}.
    No .env edits required!
    """
    slug = re.sub(r"[^a-z0-9_-]", "", (toolkit or "").strip().lower())
    if not slug:
        raise ValueError("Toolkit slug is required (e.g. 'gmail', 'github', 'notion', 'slack').")
    client = _client()
    uid = _effective_user_id()
    auth_config_id = client.toolkits._get_auth_config_id(toolkit=slug)
    try:
        req = client.connected_accounts.link(
            user_id=uid,
            auth_config_id=auth_config_id,
            allow_multiple=True,
        )
    except Exception:
        req = client.toolkits.authorize(user_id=uid, toolkit=slug)
    ca_id = getattr(req, "id", "") or ""
    redirect_url = getattr(req, "redirect_url", "") or ""
    return {
        "toolkit": slug,
        "connected_account_id": ca_id,
        "redirect_url": redirect_url,
    }


def _account(toolkit: str) -> str | None:
    slug = (toolkit or "").strip().lower()
    active_map = get_connected_toolkits_sync(force_refresh=False)
    return active_map.get(slug)


def _execute_sync(toolkit: str, slug: str, arguments: dict[str, Any]):
    kwargs: dict[str, Any] = {
        "slug": slug,
        "arguments": arguments,
        "dangerously_skip_version_check": True,
        "user_id": _effective_user_id(),
    }
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
    "Execute the single pending email send, Drive file creation, Calendar event creation, "
    "or confirmed desktop computer-use action only after the user explicitly says yes. Pass the exact confirmation token.",
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
    if kind.startswith("desktop_"):
        from tools.desktop import execute_confirmed_desktop_action

        return await execute_confirmed_desktop_action(kind, pending.payload)
    if kind == "composio_exec":
        return await execute(
            str(pending.payload.get("toolkit", "")),
            str(pending.payload.get("tool_slug", "")),
            dict(pending.payload.get("arguments") or {}),
        )
    return "Unknown pending action."


# ---------------------------------------------------------------- Universal Composio Bridge
@tool(
    "composio_list_connections",
    "List all connected and available external app integrations bridged via Composio (Gmail, Google Calendar, Google Drive, GitHub, Notion, Slack, Discord, Linear, etc.) without needing any extra API keys in .env.",
)
async def composio_list_connections(ctx: ToolContext) -> str:
    if not composio_enabled():
        return "Composio is not configured. Set COMPOSIO_API_KEY in .env once to unlock 250+ app integrations."
    active = await asyncio.to_thread(get_connected_toolkits_sync, True)
    lines = ["🔌 **Composio Universal Integration Bridge**"]
    seen: set[str] = set()
    for app in POPULAR_COMPOSIO_APPS:
        tk = app["toolkit"]
        seen.add(tk)
        ca_id = active.get(tk)
        status = f"✅ Connected ({ca_id})" if ca_id else "⚪ Not connected (1-click OAuth ready)"
        lines.append(f"- **{app['name']}** (`{tk}`): {status} — {app['desc']}")
    for tk, ca_id in sorted(active.items()):
        if tk not in seen:
            lines.append(f"- **{tk.title()}** (`{tk}`): ✅ Connected ({ca_id})")
    lines.append(
        "\nTo connect any new service (e.g. GitHub, Notion, Slack, Discord, Trello, Jira, Spotify, Outlook), "
        "call `composio_connect_app(toolkit='<name>')` for an instant browser OAuth link — no .env changes needed!"
    )
    return "\n".join(lines)


@tool(
    "composio_connect_app",
    "Connect any external service or app (e.g. 'github', 'notion', 'slack', 'discord', 'linear', 'gmail', 'googlecalendar', 'googledrive', 'outlook', 'spotify', 'trello') via Composio 1-click OAuth. No API keys or .env edits needed.",
    {
        "toolkit": {
            "type": "string",
            "description": "Toolkit slug to connect (e.g. 'github', 'notion', 'slack', 'discord', 'linear', 'gmail').",
        },
        "open_browser": {
            "type": "boolean",
            "description": "If True (default), also opens the OAuth login URL directly on the user's PC browser.",
        },
    },
    ["toolkit"],
)
async def composio_connect_app(
    ctx: ToolContext, toolkit: str, open_browser: bool = True
) -> str:
    if not composio_enabled():
        return "Composio is not configured. Add your single COMPOSIO_API_KEY to .env."
    try:
        info = await asyncio.to_thread(authorize_toolkit_sync, toolkit)
    except Exception as exc:
        return f"Could not initiate Composio connection for '{toolkit}': {exc}"

    url = info.get("redirect_url") or ""
    ca_id = info.get("connected_account_id") or ""
    slug = info.get("toolkit") or toolkit
    if open_browser and url:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:
            pass
    return (
        f"🔗 **Connect {slug.title()} via Composio**\n"
        f"- Account slot: `{ca_id}`\n"
        f"- OAuth Link: {url}\n\n"
        "Complete the login in your browser and JARVIS will automatically discover and use the connection immediately (no .env edit or restart required)."
    )


@tool(
    "composio_execute_action",
    "Execute any Composio tool action across 250+ connected apps (GitHub, Notion, Slack, Discord, Linear, Gmail, Calendar, Drive, etc.) using Composio as the universal bridge.",
    {
        "toolkit": {
            "type": "string",
            "description": "Toolkit slug, e.g. 'github', 'notion', 'slack', 'gmail'.",
        },
        "tool_slug": {
            "type": "string",
            "description": "Exact Composio action slug, e.g. 'GITHUB_STAR_A_REPOSITORY_FOR_THE_AUTHENTICATED_USER', 'NOTION_SEARCH_NOTION_PAGE', 'SLACK_SENDS_A_MESSAGE_TO_A_SLACK_CHANNEL'.",
        },
        "arguments": {
            "type": "object",
            "description": "JSON object of arguments for the Composio tool action.",
        },
        "require_confirmation": {
            "type": "boolean",
            "description": "Set True for write/send/delete actions that modify external state; False for read/search actions.",
        },
    },
    ["toolkit", "tool_slug"],
)
async def composio_execute_action(
    ctx: ToolContext,
    toolkit: str,
    tool_slug: str,
    arguments: dict[str, Any] | None = None,
    require_confirmation: bool = False,
) -> str:
    args = dict(arguments or {})
    if require_confirmation:
        return _queue_action(
            ctx,
            "composio_exec",
            {"toolkit": toolkit, "tool_slug": tool_slug, "arguments": args},
            f"Run Composio action {tool_slug} on {toolkit} with {json.dumps(args)[:200]}.",
        )
    return await execute(toolkit, tool_slug.upper(), args)
