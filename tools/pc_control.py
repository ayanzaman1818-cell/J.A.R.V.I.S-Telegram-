"""PC / Desktop remote control tools (system status, screenshot, volume/media, lock, app launcher)."""

from __future__ import annotations

import asyncio
import ctypes
import os
import platform
import shutil
import subprocess
import tempfile
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

import config
from tools.base import ToolContext, tool


def _require_owner(ctx: ToolContext) -> str | None:
    if not config.is_owner_user(ctx.user_id or ctx.chat_id):
        return "Permission denied: only the bot owner can run PC control tools."
    return None


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_byte),
        ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_byte),
        ("SystemStatusFlag", ctypes.c_byte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


def _collect_pc_status() -> str:
    lines = [f"Host OS: {platform.system()} {platform.release()} ({platform.machine()})"]

    if os.name == "nt":
        mem = _MEMORYSTATUSEX()
        mem.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
            total_gb = mem.ullTotalPhys / (1024**3)
            used_gb = (mem.ullTotalPhys - mem.ullAvailPhys) / (1024**3)
            lines.append(
                f"RAM: {used_gb:.1f} GB / {total_gb:.1f} GB ({mem.dwMemoryLoad}% used)"
            )

        pwr = _SYSTEM_POWER_STATUS()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(pwr)):
            if pwr.BatteryFlag != 128 and pwr.BatteryLifePercent <= 100:
                ac = "Plugged in" if pwr.ACLineStatus == 1 else "On battery"
                lines.append(f"Battery: {pwr.BatteryLifePercent}% ({ac})")
            else:
                lines.append("Power: Desktop AC (no battery)")

        uptime_ms = ctypes.windll.kernel32.GetTickCount64()
        uptime_hrs = uptime_ms / (1000 * 3600)
        lines.append(f"Uptime: {uptime_hrs:.1f} hours")

    try:
        usage = shutil.disk_usage(config.BASE_DIR)
        used_d = usage.used / (1024**3)
        total_d = usage.total / (1024**3)
        pct_d = (usage.used / usage.total) * 100 if usage.total else 0
        lines.append(
            f"Disk ({config.BASE_DIR.drive or config.BASE_DIR}): {used_d:.1f} GB / {total_d:.1f} GB ({pct_d:.0f}% used)"
        )
    except Exception:
        pass

    return "\n".join(lines)


def get_pc_metrics() -> dict[str, Any]:
    """Collect structured PC health metrics for proactive monitoring and status checks."""
    metrics: dict[str, Any] = {
        "ram_pct": 0.0,
        "disk_pct": 0.0,
        "battery_pct": None,
        "on_battery": False,
        "uptime_hrs": 0.0,
    }
    if os.name == "nt":
        mem = _MEMORYSTATUSEX()
        mem.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
            metrics["ram_pct"] = float(mem.dwMemoryLoad)

        pwr = _SYSTEM_POWER_STATUS()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(pwr)):
            if pwr.BatteryFlag != 128 and pwr.BatteryLifePercent <= 100:
                metrics["battery_pct"] = int(pwr.BatteryLifePercent)
                metrics["on_battery"] = pwr.ACLineStatus != 1

        uptime_ms = ctypes.windll.kernel32.GetTickCount64()
        metrics["uptime_hrs"] = round(uptime_ms / (1000 * 3600), 1)

    try:
        usage = shutil.disk_usage(config.BASE_DIR)
        if usage.total:
            metrics["disk_pct"] = round((usage.used / usage.total) * 100, 1)
    except Exception:
        pass
    return metrics


@tool(
    "pc_status",
    "Check the host PC's RAM usage, disk space, battery level, and system uptime.",
)
async def pc_status(ctx: ToolContext) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    return await asyncio.to_thread(_collect_pc_status)


def _capture_desktop_png(path: str) -> None:
    try:
        from PIL import ImageGrab

        img = ImageGrab.grab(all_screens=True)
        img.save(path, "PNG")
        return
    except ImportError:
        pass

    if os.name != "nt":
        raise RuntimeError("Desktop screenshot requires Windows or Pillow installed.")

    ps_script = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
        "$b = [System.Windows.Forms.SystemInformation]::VirtualScreen; "
        "$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height; "
        "$g = [System.Drawing.Graphics]::FromImage($bmp); "
        "$g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size); "
        f"$bmp.Save('{path}', [System.Drawing.Imaging.ImageFormat]::Png); "
        "$g.Dispose(); $bmp.Dispose()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_script],
        check=True,
        timeout=15,
        capture_output=True,
    )


@tool(
    "pc_screenshot",
    "Take a live screenshot of the owner's PC desktop screen and send it to Telegram.",
)
async def pc_screenshot(ctx: ToolContext) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        await asyncio.to_thread(_capture_desktop_png, path)
        with open(path, "rb") as f:
            await ctx.bot.send_photo(ctx.chat_id, f, caption="Desktop screenshot")
    finally:
        if os.path.exists(path):
            os.unlink(path)
    return "Desktop screenshot sent to Telegram."


_VK_MAP = {
    "mute": 0xAD,
    "volume_down": 0xAE,
    "volume_up": 0xAF,
    "next_track": 0xB0,
    "prev_track": 0xB1,
    "stop": 0xB2,
    "play_pause": 0xB3,
}


def _send_vk(vk_code: int, steps: int = 1) -> None:
    if os.name != "nt":
        raise RuntimeError("Media/volume key control is only supported on Windows.")
    for _ in range(max(1, min(steps, 25))):
        ctypes.windll.user32.keybd_event(vk_code, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk_code, 0, 2, 0)


@tool(
    "pc_media_control",
    "Control PC system volume and media playback (play/pause, next, previous, mute, volume up/down).",
    {
        "action": {
            "type": "string",
            "enum": [
                "play_pause",
                "next_track",
                "prev_track",
                "stop",
                "mute",
                "volume_up",
                "volume_down",
            ],
        },
        "steps": {
            "type": "integer",
            "description": "Number of key presses for volume_up / volume_down (default 5).",
        },
    },
    ["action"],
)
async def pc_media_control(
    ctx: ToolContext, action: str, steps: int = 5
) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    vk = _VK_MAP.get(action)
    if not vk:
        return f"Unsupported action: {action}"
    presses = steps if action in ("volume_up", "volume_down") else 1
    await asyncio.to_thread(_send_vk, vk, presses)
    return f"Executed PC media control: {action}."


@tool(
    "pc_lock",
    "Lock the owner's Windows PC workstation screen immediately.",
)
async def pc_lock(ctx: ToolContext) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    if os.name != "nt":
        return "Lock workstation is only supported on Windows."
    ctypes.windll.user32.LockWorkStation()
    return "PC workstation locked."


_SAFE_APPS = {
    "notepad": ["notepad.exe"],
    "calculator": ["calc.exe"],
    "calc": ["calc.exe"],
    "explorer": ["explorer.exe"],
    "spotify": ["cmd", "/c", "start", "spotify:"],
    "vscode": ["cmd", "/c", "code"],
    "code": ["cmd", "/c", "code"],
    "chrome": ["cmd", "/c", "start", "chrome"],
    "edge": ["cmd", "/c", "start", "msedge"],
}


@tool(
    "pc_launch_app",
    "Open a desktop application (spotify, vscode, notepad, calc, chrome, edge, explorer) or open a URL in the PC's default browser.",
    {
        "target": {
            "type": "string",
            "description": "App name (e.g. 'spotify', 'vscode', 'calc') or an https:// URL.",
        }
    },
    ["target"],
)
async def pc_launch_app(ctx: ToolContext, target: str) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    cleaned = target.strip()
    if cleaned.startswith(("http://", "https://")):
        await asyncio.to_thread(webbrowser.open, cleaned)
        return f"Opened {cleaned} on PC default browser."

    key = cleaned.lower()
    cmd = _SAFE_APPS.get(key)
    if not cmd:
        allowed = ", ".join(sorted(_SAFE_APPS.keys()))
        return f"App '{cleaned}' is not in the safe launch list ({allowed})."
    subprocess.Popen(cmd, shell=False)
    return f"Launched '{key}' on PC."


_CLIPBOARD_FALLBACK: str = ""


def _read_clipboard_sync() -> str:
    global _CLIPBOARD_FALLBACK
    if os.name == "nt":
        try:
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode == 0:
                return (res.stdout or "").rstrip("\r\n")
        except Exception:
            pass
    return _CLIPBOARD_FALLBACK


def _write_clipboard_sync(text: str) -> None:
    global _CLIPBOARD_FALLBACK
    _CLIPBOARD_FALLBACK = text
    if os.name == "nt":
        try:
            subprocess.run(
                ["clip.exe"],
                input=text.encode("utf-16le"),
                check=False,
                timeout=5,
                capture_output=True,
            )
        except Exception:
            pass


@tool(
    "pc_clipboard_get",
    "Read the current text contents of the owner's PC clipboard.",
)
async def pc_clipboard_get(ctx: ToolContext) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    content = await asyncio.to_thread(_read_clipboard_sync)
    if not content:
        return "PC clipboard is empty (or contains non-text data)."
    return f"PC Clipboard:\n{content[:3500]}"


@tool(
    "pc_clipboard_set",
    "Copy text or a URL directly into the owner's PC clipboard.",
    {"text": {"type": "string", "description": "Text to copy to the PC clipboard."}},
    ["text"],
)
async def pc_clipboard_set(ctx: ToolContext, text: str) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    if not text:
        return "Cannot copy empty text to clipboard."
    await asyncio.to_thread(_write_clipboard_sync, text)
    preview = text[:80] + ("..." if len(text) > 80 else "")
    return f"Copied to PC clipboard: {preview}"


def _allowed_pc_folders() -> dict[str, Path]:
    home = Path.home()
    return {
        "downloads": home / "Downloads",
        "desktop": home / "Desktop",
        "documents": home / "Documents",
        "inbox": config.INBOX_DIR,
        "notes": config.KNOWLEDGE_LOCAL_DIR,
    }


def _resolve_pc_folder(folder: str) -> Path | None:
    folders = _allowed_pc_folders()
    return folders.get((folder or "downloads").strip().lower())


@tool(
    "pc_list_files",
    "List recent files in the owner's PC Downloads, Desktop, Documents, Inbox, or Notes folder.",
    {
        "folder": {
            "type": "string",
            "enum": ["downloads", "desktop", "documents", "inbox", "notes"],
            "description": "Folder to inspect (default 'downloads').",
        },
        "limit": {
            "type": "integer",
            "description": "Max files to list (default 15).",
        },
    },
)
async def pc_list_files(
    ctx: ToolContext, folder: str = "downloads", limit: int = 15
) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    target_dir = _resolve_pc_folder(folder)
    if target_dir is None:
        return "Unsupported folder. Choose downloads, desktop, documents, inbox, or notes."
    if not target_dir.exists():
        return f"Folder '{folder}' ({target_dir}) does not exist yet."

    def _scan() -> list[str]:
        files = [p for p in target_dir.iterdir() if p.is_file()]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        lines = [f"Recent files in {folder} ({target_dir}):"]
        for f in files[: max(1, min(int(limit), 40))]:
            st = f.stat()
            kb = st.st_size / 1024.0
            size_str = f"{kb / 1024:.1f} MB" if kb >= 1024 else f"{kb:.1f} KB"
            mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
            lines.append(f"- {f.name} ({size_str}, {mtime})")
        return lines if len(lines) > 1 else [f"No files found in {folder}."]

    return "\n".join(await asyncio.to_thread(_scan))


@tool(
    "pc_send_file",
    "Send a file from the owner's PC (Downloads, Desktop, Documents, Inbox, or Notes) to Telegram. "
    "Pass filename='latest' to send the most recently modified file in that folder.",
    {
        "filename": {
            "type": "string",
            "description": "Exact or partial file name, or 'latest' for the newest file.",
        },
        "folder": {
            "type": "string",
            "enum": ["downloads", "desktop", "documents", "inbox", "notes"],
            "description": "Source folder (default 'downloads').",
        },
    },
    ["filename"],
)
async def pc_send_file(
    ctx: ToolContext, filename: str, folder: str = "downloads"
) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    target_dir = _resolve_pc_folder(folder)
    if target_dir is None or not target_dir.exists():
        return f"Folder '{folder}' is unavailable."

    query = filename.strip()
    if not query:
        return "Please specify a filename or 'latest'."

    def _find_file() -> Path | None:
        root_resolved = target_dir.resolve()
        files = [
            p
            for p in root_resolved.iterdir()
            if p.is_file() and p.resolve().is_relative_to(root_resolved)
        ]
        if not files:
            return None
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if query.lower() == "latest":
            return files[0]
        for p in files:
            if p.name.lower() == query.lower():
                return p
        for p in files:
            if query.lower() in p.name.lower():
                return p
        return None

    chosen = await asyncio.to_thread(_find_file)
    if chosen is None:
        return f"No matching file '{query}' found in {folder}."

    size = chosen.stat().st_size
    if size > 45 * 1024 * 1024:
        return f"File '{chosen.name}' is too large for Telegram ({size / (1024**2):.1f} MB > 45 MB)."

    if ctx.bot:
        with open(chosen, "rb") as f:
            await ctx.bot.send_document(
                ctx.chat_id,
                document=f,
                filename=chosen.name,
                caption=f"📁 {chosen.name} (from {folder})",
            )
    return f"Sent '{chosen.name}' ({size / 1024:.1f} KB) from {folder} to Telegram."


@tool(
    "pc_focus_mode",
    "Activate a desktop environment preset: 'work' (launch VS Code + Spotify), 'chill' (Spotify + comfortable volume), "
    "'meeting' (mute media immediately), or 'night' (mute media + lock workstation).",
    {
        "mode": {
            "type": "string",
            "enum": ["work", "chill", "meeting", "night"],
            "description": "Preset mode to activate.",
        }
    },
    ["mode"],
)
async def pc_focus_mode(ctx: ToolContext, mode: str) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    m = mode.strip().lower()
    actions_taken: list[str] = []
    if m == "work":
        try:
            subprocess.Popen(_SAFE_APPS["vscode"], shell=False)
            actions_taken.append("launched VS Code")
        except Exception:
            pass
        try:
            subprocess.Popen(_SAFE_APPS["spotify"], shell=False)
            actions_taken.append("opened Spotify")
        except Exception:
            pass
        return f"Work Mode activated ({', '.join(actions_taken) or 'workspace ready'})."

    if m == "chill":
        try:
            subprocess.Popen(_SAFE_APPS["spotify"], shell=False)
            actions_taken.append("opened Spotify")
        except Exception:
            pass
        if os.name == "nt":
            await asyncio.to_thread(_send_vk, _VK_MAP["play_pause"], 1)
            actions_taken.append("toggled playback")
        return f"Chill Mode activated ({', '.join(actions_taken) or 'ready'})."

    if m == "meeting":
        if os.name == "nt":
            await asyncio.to_thread(_send_vk, _VK_MAP["stop"], 1)
            await asyncio.to_thread(_send_vk, _VK_MAP["mute"], 1)
        return "Meeting Mode activated (media stopped and system audio muted)."

    if m == "night":
        if os.name == "nt":
            await asyncio.to_thread(_send_vk, _VK_MAP["stop"], 1)
            await asyncio.to_thread(_send_vk, _VK_MAP["mute"], 1)
            ctypes.windll.user32.LockWorkStation()
        return "Night Mode activated (audio muted, playback stopped, and PC locked)."

    return f"Unknown mode '{mode}'."
