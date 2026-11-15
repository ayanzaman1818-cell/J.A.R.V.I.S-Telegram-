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
