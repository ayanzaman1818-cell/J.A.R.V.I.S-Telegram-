"""Spotify and desktop music playback integration."""

from __future__ import annotations

import asyncio
import ctypes
import os
import subprocess
from urllib.parse import quote

import config
from tools.base import ToolContext, tool
from tools.pc_control import _VK_MAP, _require_owner, _send_vk


def _get_spotify_window_title() -> str | None:
    """Inspect visible Windows top-level windows to detect the currently playing Spotify track."""
    if os.name != "nt":
        return None

    titles: list[str] = []
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def foreach_window(hwnd, lParam):
        if ctypes.windll.user32.IsWindowVisible(hwnd):
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buff = ctypes.create_unicode_buffer(length + 1)
                ctypes.windll.user32.GetWindowTextW(hwnd, buff, length + 1)
                text = buff.value.strip()
                if " - " in text or text.startswith("Spotify"):
                    pid = ctypes.c_ulong()
                    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    titles.append(text)
        return True

    ctypes.windll.user32.EnumWindows(EnumWindowsProc(foreach_window), 0)

    # Check via PowerShell Get-Process Spotify for exact MainWindowTitle
    try:
        res = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-Process Spotify -ErrorAction SilentlyContinue | Where-Object {$_.MainWindowTitle} | Select-Object -First 1).MainWindowTitle",
            ],
            capture_output=True,
            text=True,
            timeout=4,
        )
        win_title = (res.stdout or "").strip()
        if win_title:
            return win_title
    except Exception:
        pass
    return None


@tool(
    "spotify_status",
    "Check what song or podcast is currently playing on Spotify on the owner's PC.",
)
async def spotify_status(ctx: ToolContext) -> str:
    title = await asyncio.to_thread(_get_spotify_window_title)
    if not title:
        return "Spotify does not appear to be running on the PC right now."
    if title.lower() in ("spotify", "spotify free", "spotify premium"):
        return "Spotify is open on the PC, but playback is currently paused."
    return f"Currently playing on Spotify: {title}"


@tool(
    "spotify_play",
    "Search and open a song, artist, or playlist in the Spotify desktop app, or control playback (play/pause, next, previous).",
    {
        "query": {
            "type": "string",
            "description": "Song, artist, or playlist to search/open in Spotify (leave empty if only controlling playback).",
        },
        "action": {
            "type": "string",
            "enum": ["search", "play_pause", "next_track", "prev_track"],
            "description": "Playback action (default 'search' when query is given, else 'play_pause').",
        },
    },
)
async def spotify_play(
    ctx: ToolContext, query: str = "", action: str = "search"
) -> str:
    err = _require_owner(ctx)
    if err:
        return err

    cleaned = query.strip()
    if cleaned and action == "search":
        uri = f"spotify:search:{quote(cleaned)}"
        if os.name == "nt":
            subprocess.Popen(["cmd", "/c", "start", "", uri], shell=False)
        return f"Opened Spotify search for '{cleaned}'."

    act = action if action in ("play_pause", "next_track", "prev_track") else "play_pause"
    await asyncio.to_thread(_send_vk, _VK_MAP[act], 1)
    await asyncio.sleep(0.5)
    now_playing = await asyncio.to_thread(_get_spotify_window_title)
    status_suffix = f" Now showing: {now_playing}" if now_playing else ""
    return f"Spotify action '{act}' sent.{status_suffix}"
