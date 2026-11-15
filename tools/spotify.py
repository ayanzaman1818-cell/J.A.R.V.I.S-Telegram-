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


_SPOTIFY_AUTOPLAY_PS1 = r"""
$ErrorActionPreference = 'SilentlyContinue'
$SearchUri = $env:SPOTIFY_SEARCH_URI
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32Spotify {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr FindWindowEx(IntPtr parentHandle, IntPtr childAfter, string lclassName, string windowTitle);
    [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);
}
"@

function Get-SpotifyProc {
    Get-Process Spotify -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
}

$beforeProc = Get-SpotifyProc
$beforeTitle = if ($beforeProc) { [string]$beforeProc.MainWindowTitle } else { "" }

Start-Process $SearchUri
$proc = $null
for ($i = 0; $i -lt 25; $i++) {
    Start-Sleep -Milliseconds 200
    $proc = Get-SpotifyProc
    if ($proc) { break }
}
if (-not $proc) {
    Write-Output "CLICKED=False|TITLE="
    exit 0
}

$hwnd = $proc.MainWindowHandle
[Win32Spotify]::ShowWindow($hwnd, 9) | Out-Null
[Win32Spotify]::SetForegroundWindow($hwnd) | Out-Null
Start-Sleep -Milliseconds 1800

# Wake up Chromium's Web Accessibility Tree (WM_GETOBJECT + OBJID_CLIENT = -4)
$renderHwnd = [Win32Spotify]::FindWindowEx($hwnd, [IntPtr]::Zero, "Chrome_RenderWidgetHostHWND", $null)
if ($renderHwnd -ne [IntPtr]::Zero) {
    [Win32Spotify]::SendMessage($renderHwnd, 0x003D, [IntPtr]::Zero, [IntPtr](-4)) | Out-Null
}

$root = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
$searchRoot = if ($renderHwnd -ne [IntPtr]::Zero) {
    [System.Windows.Automation.AutomationElement]::FromHandle($renderHwnd)
} else {
    $root
}
if ($searchRoot) {
    $null = $searchRoot.FindFirst([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
    Start-Sleep -Milliseconds 500
}

$clicked = $false
$btnCond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Button
)

for ($attempt = 0; $attempt -lt 6; $attempt++) {
    if (-not $root) { break }
    $winRect = $root.Current.BoundingRectangle
    if ($winRect.Width -le 0) {
        Start-Sleep -Milliseconds 350
        continue
    }

    $minX = $winRect.Left + [Math]::Max(240, [int]($winRect.Width * 0.20))
    $minY = $winRect.Top + 120
    $maxX = $winRect.Right - 25
    $maxY = $winRect.Bottom - 105

    # Hover over Top Result / first Song row so hover-only Play buttons become active
    $hoverX = [int]($minX + 140)
    $hoverY = [int]($minY + 110)
    [Win32Spotify]::SetCursorPos($hoverX, $hoverY) | Out-Null
    Start-Sleep -Milliseconds 220

    $buttons = $searchRoot.FindAll([System.Windows.Automation.TreeScope]::Descendants, $btnCond)
    $candidates = @()
    foreach ($b in $buttons) {
        $bName = [string]$b.Current.Name
        if ($bName -match '^Play(\s|$)') {
            $r = $b.Current.BoundingRectangle
            if ($r.Width -gt 4 -and $r.Height -gt 4 -and $r.Left -ge $minX -and $r.Right -le $maxX -and $r.Top -ge $minY -and $r.Bottom -le $maxY) {
                $priority = if ($bName.Length -gt 4) { 0 } else { 1 }
                $candidates += [PSCustomObject]@{ Btn = $b; Rect = $r; Priority = $priority; Top = $r.Top; Left = $r.Left; Name = $bName }
            }
        }
    }

    if ($candidates.Count -gt 0) {
        $best = $candidates | Sort-Object Priority, Top, Left | Select-Object -First 1
        $targetBtn = $best.Btn
        $r = $best.Rect
        $inv = $null
        if ($targetBtn.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$inv)) {
            $inv.Invoke()
        }
        Start-Sleep -Milliseconds 650
        $nowProc = Get-SpotifyProc
        $nowTitle = if ($nowProc) { [string]$nowProc.MainWindowTitle } else { "" }
        if (-not ($nowTitle -match ' - ') -or $nowTitle -eq $beforeTitle) {
            $cx = [int]($r.Left + ($r.Width / 2))
            $cy = [int]($r.Top + ($r.Height / 2))
            [Win32Spotify]::SetCursorPos($cx, $cy) | Out-Null
            Start-Sleep -Milliseconds 150
            [Win32Spotify]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
            [Win32Spotify]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
        }
        $clicked = $true
        break
    }
    Start-Sleep -Milliseconds 350
}

# Fallback: if no Play button was exposed in UIAutomation, double-click the first song row / top result play zone
if (-not $clicked -and $root) {
    $winRect = $root.Current.BoundingRectangle
    if ($winRect.Width -gt 0) {
        $minX = $winRect.Left + [Math]::Max(240, [int]($winRect.Width * 0.22))
        $minY = $winRect.Top + 130
        $tx = [int]($minX + 320)
        $ty = [int]($minY + 185)
        [Win32Spotify]::SetCursorPos($tx, $ty) | Out-Null
        Start-Sleep -Milliseconds 250
        [Win32Spotify]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
        [Win32Spotify]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
        Start-Sleep -Milliseconds 90
        [Win32Spotify]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
        [Win32Spotify]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
        $clicked = $true
    }
}

Start-Sleep -Milliseconds 800
$finalProc = Get-SpotifyProc
$finalTitle = if ($finalProc) { [string]$finalProc.MainWindowTitle } else { "" }
Write-Output "CLICKED=$clicked|TITLE=$finalTitle"
"""


def _run_spotify_search_and_play(uri: str) -> tuple[bool, str]:
    """Launch a Spotify search URI and click/invoke the top result's Play button."""
    if os.name != "nt":
        return False, ""
    try:
        env = os.environ.copy()
        env["SPOTIFY_SEARCH_URI"] = uri
        res = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _SPOTIFY_AUTOPLAY_PS1,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=16,
        )
        out = (res.stdout or "").strip().splitlines()
        last = out[-1] if out else ""
        if "TITLE=" in last:
            parts = dict(item.split("=", 1) for item in last.split("|") if "=" in item)
            return parts.get("CLICKED") == "True", parts.get("TITLE", "").strip()
    except Exception:
        pass
    return False, ""


@tool(
    "spotify_play",
    "Search and immediately PLAY a song, artist, or playlist in the Spotify desktop app, or control playback (play_pause, next_track, prev_track).",
    {
        "query": {
            "type": "string",
            "description": "Song, artist, or playlist to search and immediately play in Spotify (leave empty if only controlling playback).",
        },
        "action": {
            "type": "string",
            "enum": ["play", "search", "play_pause", "next_track", "prev_track"],
            "description": "Playback action (default 'play' when query is given, else 'play_pause').",
        },
    },
)
async def spotify_play(
    ctx: ToolContext, query: str = "", action: str = "play"
) -> str:
    err = _require_owner(ctx)
    if err:
        return err

    cleaned = query.strip()
    if cleaned and action in ("play", "search"):
        uri = f"spotify:search:{quote(cleaned)}"
        clicked, now_playing = await asyncio.to_thread(_run_spotify_search_and_play, uri)
        if now_playing and " - " in now_playing:
            return f"Now playing on Spotify: {now_playing}"
        if clicked:
            return f"Searched and triggered playback for '{cleaned}' on Spotify."
        return f"Opened Spotify search for '{cleaned}', but could not auto-click Play (ensure the Spotify desktop window is visible)."

    act = action if action in ("play_pause", "next_track", "prev_track") else "play_pause"
    await asyncio.to_thread(_send_vk, _VK_MAP[act], 1)
    await asyncio.sleep(0.5)
    now_playing = await asyncio.to_thread(_get_spotify_window_title)
    status_suffix = f" Now showing: {now_playing}" if now_playing else ""
    return f"Spotify action '{act}' sent.{status_suffix}"
