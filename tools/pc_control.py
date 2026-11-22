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


def _enum_visible_windows() -> list[dict[str, Any]]:
    """Return visible top-level windows with hwnd, pid, and title via Win32 EnumWindows."""
    if os.name != "nt":
        return []
    user32 = ctypes.windll.user32
    results: list[dict[str, Any]] = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _cb(hwnd: int, _lparam: int) -> bool:
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value.strip()
                if title and title not in ("Program Manager", "Settings", "Microsoft Text Input Application"):
                    pid = ctypes.c_ulong(0)
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    results.append({"hwnd": int(hwnd), "pid": int(pid.value), "title": title})
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return results


def _find_window_by_title(query: str) -> dict[str, Any] | None:
    q = (query or "").strip().lower()
    if not q:
        return None
    for w in _enum_visible_windows():
        if q in w["title"].lower():
            return w
    return None


def _capture_desktop_png(
    path: str,
    *,
    window_title: str = "",
    hide_telegram: bool = True,
) -> str:
    """Capture desktop or a specific target window; optionally hides foreground Telegram window first."""
    import time

    minimized_hwnd: int | None = None
    note = "Full desktop"
    if os.name == "nt":
        user32 = ctypes.windll.user32
        if window_title.strip():
            match = _find_window_by_title(window_title)
            if match:
                # Capture target window in the background via Win32 PrintWindow without stealing focus!
                from tools.desktop import _capture_window_background_pil, _find_hwnd_by_title

                info = _find_hwnd_by_title(window_title)
                if info:
                    hwnd, full_title, (_, _, ww, wh) = info
                    bg_img = _capture_window_background_pil(hwnd, ww, wh)
                    if bg_img is not None:
                        bg_img.save(path, "PNG")
                        return f"Window (background): {full_title}"
                hwnd = match["hwnd"]
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
                time.sleep(0.25)
                note = f"Window: {match['title']}"
        elif hide_telegram:
            fg = user32.GetForegroundWindow()
            if fg:
                length = user32.GetWindowTextLengthW(fg)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(fg, buf, length + 1)
                    fg_title = buf.value.strip()
                    if any(k in fg_title.lower() for k in ("telegram", "web.telegram.org")):
                        user32.ShowWindow(fg, 6)  # SW_MINIMIZE
                        minimized_hwnd = fg
                        time.sleep(0.30)
                        note = "Desktop (Telegram auto-minimized)"

    try:
        try:
            from PIL import ImageGrab

            img = ImageGrab.grab(all_screens=True)
            img.save(path, "PNG")
            return note
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
        return note
    finally:
        if minimized_hwnd and os.name == "nt":
            try:
                ctypes.windll.user32.ShowWindow(minimized_hwnd, 9)  # SW_RESTORE
            except Exception:
                pass


@tool(
    "pc_screenshot",
    "Take a live screenshot of the owner's PC desktop or a specific application window and send it to Telegram. "
    "Automatically minimizes Telegram if it is in the foreground so it captures the real underlying app.",
    {
        "window_title": {
            "type": "string",
            "description": "Optional app/window title to focus before capturing (e.g. 'Visual Studio Code', 'Chrome', 'PowerShell').",
        },
        "delay_seconds": {
            "type": "integer",
            "description": "Optional countdown delay (0..20s) so the user can switch windows.",
        },
        "hide_telegram": {
            "type": "boolean",
            "description": "Temporarily minimize Telegram if it is the active foreground window (default true).",
        },
    },
)
async def pc_screenshot(
    ctx: ToolContext,
    window_title: str = "",
    delay_seconds: int = 0,
    hide_telegram: bool = True,
) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    wait_s = max(0, min(int(delay_seconds or 0), 20))
    if wait_s > 0:
        await asyncio.sleep(wait_s)
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        cap_note = await asyncio.to_thread(
            _capture_desktop_png,
            path,
            window_title=window_title,
            hide_telegram=hide_telegram,
        )
        if ctx.bot:
            with open(path, "rb") as f:
                await ctx.bot.send_photo(ctx.chat_id, f, caption=f"📸 {cap_note}")
    finally:
        if os.path.exists(path):
            os.unlink(path)
    return f"Desktop screenshot ({cap_note}) sent to Telegram."


@tool(
    "pc_inspect_window",
    "Look at a specific application window (or the underlying PC screen, auto-hiding Telegram), "
    "capture a screenshot, analyze it with Gemini Vision, and answer the user's question or debug the error shown.",
    {
        "window_title": {
            "type": "string",
            "description": "Optional window title to bring to front first (e.g. 'Visual Studio Code', 'Terminal').",
        },
        "question": {
            "type": "string",
            "description": "What to analyze or solve on the screen.",
        },
        "delay_seconds": {
            "type": "integer",
            "description": "Optional delay in seconds before snapping (0..20).",
        },
    },
)
async def pc_inspect_window(
    ctx: ToolContext,
    window_title: str = "",
    question: str = "Examine this screen/window, explain what is shown, and help solve any visible error or task.",
    delay_seconds: int = 0,
) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    wait_s = max(0, min(int(delay_seconds or 0), 20))
    if wait_s > 0:
        await asyncio.sleep(wait_s)
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        cap_note = await asyncio.to_thread(
            _capture_desktop_png,
            path,
            window_title=window_title,
            hide_telegram=True,
        )
        img_bytes = Path(path).read_bytes()
        if ctx.bot:
            with open(path, "rb") as f:
                await ctx.bot.send_photo(ctx.chat_id, f, caption=f"👁 Inspecting {cap_note}")
    finally:
        if os.path.exists(path):
            os.unlink(path)

    try:
        from brain.gemini import chat
        from brain.history import response_text

        resp = await chat(
            [
                {
                    "role": "system",
                    "content": "You are JARVIS inspecting the owner's live PC screen. Be concise, technical, and directly helpful.",
                },
                {"role": "user", "content": question},
            ],
            use_tools=False,
            media_bytes=img_bytes,
            mime_type="image/png",
        )
        analysis = response_text(resp).strip()
        if analysis:
            return f"👁 Screen Analysis ({cap_note}):\n{analysis}"
    except Exception as exc:
        return f"Captured {cap_note}, but vision analysis failed ({type(exc).__name__})."
    return f"Captured {cap_note}."


_VK_MAP = {
    "mute": 0xAD,
    "volume_down": 0xAE,
    "volume_up": 0xAF,
    "next_track": 0xB0,
    "next": 0xB0,
    "prev_track": 0xB1,
    "prev": 0xB1,
    "stop": 0xB2,
    "play_pause": 0xB3,
}

_APPCOMMAND_MAP = {
    "next_track": 11,      # APPCOMMAND_MEDIA_NEXTTRACK
    "next": 11,
    "prev_track": 12,      # APPCOMMAND_MEDIA_PREVIOUSTRACK
    "prev": 12,
    "stop": 13,            # APPCOMMAND_MEDIA_STOP
    "play_pause": 14,      # APPCOMMAND_MEDIA_PLAY_PAUSE
}


def _send_vk(vk_code: int, steps: int = 1) -> None:
    if os.name != "nt":
        raise RuntimeError("Media/volume key control is only supported on Windows.")
    user32 = ctypes.windll.user32
    scan = user32.MapVirtualKeyW(vk_code, 0)
    # Media & volume hardware virtual keys (0xAD..0xB3) require KEYEVENTF_EXTENDEDKEY (0x0001)
    ext = 0x0001 if 0xAD <= vk_code <= 0xB3 else 0x0000
    for _ in range(max(1, min(steps, 25))):
        user32.keybd_event(vk_code, scan, ext, 0)
        user32.keybd_event(vk_code, scan, ext | 0x0002, 0)


def send_media_transport_command(action: str) -> None:
    """Trigger a media transport command (prev_track, play_pause, next_track) reliably across Windows and Spotify."""
    if os.name != "nt":
        return
    act = (action or "").strip().lower()
    vk = _VK_MAP.get(act)
    if vk:
        _send_vk(vk, 1)

    # Also post WM_APPCOMMAND (0x0319) directly to any open Spotify window so background track skipping always works
    app_cmd = _APPCOMMAND_MAP.get(act)
    if not app_cmd:
        return
    user32 = ctypes.windll.user32
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    cls_buf = ctypes.create_unicode_buffer(128)
    lparam = app_cmd << 16

    def _cb(hwnd: int, _lp: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        user32.GetClassNameW(hwnd, cls_buf, 128)
        if not cls_buf.value.startswith("Chrome_WidgetWin"):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        t_low = buf.value.strip().lower()
        if t_low.startswith("spotify") or (
            " - " in buf.value
            and not any(
                b in t_low
                for b in (
                    "google chrome",
                    "microsoft edge",
                    "visual studio code",
                    "antigravity",
                    "discord",
                    "lm studio",
                    "telegram",
                )
            )
        ):
            user32.PostMessageW(hwnd, 0x0319, hwnd, lparam)
        return True

    try:
        user32.EnumWindows(EnumWindowsProc(_cb), 0)
    except Exception:
        pass


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
    "Open a desktop application (spotify, vscode, notepad, calc, chrome, edge, explorer) or open a URL in the PC's default browser. "
    "Launches in the background by default (SW_SHOWNOACTIVATE) so it does not steal focus from the user's active window.",
    {
        "target": {
            "type": "string",
            "description": "App name (e.g. 'spotify', 'vscode', 'calc') or an https:// URL.",
        },
        "background": {
            "type": "boolean",
            "description": "Launch without stealing active window focus (default true).",
        },
    },
    ["target"],
)
async def pc_launch_app(
    ctx: ToolContext, target: str, background: bool = True
) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    cleaned = target.strip()
    if cleaned.startswith(("http://", "https://")):
        await asyncio.to_thread(webbrowser.open, cleaned, new=2, autoraise=not background)
        return f"Opened {cleaned} on PC default browser."

    key = cleaned.lower()
    cmd = _SAFE_APPS.get(key)
    if not cmd:
        allowed = ", ".join(sorted(_SAFE_APPS.keys()))
        return f"App '{cleaned}' is not in the safe launch list ({allowed})."
    startupinfo = None
    if os.name == "nt" and background:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 4  # SW_SHOWNOACTIVATE (open in background without stealing focus)
    subprocess.Popen(cmd, shell=False, startupinfo=startupinfo)
    mode_str = " in background" if background else ""
    return f"Launched '{key}'{mode_str} on PC."


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


# --- Advanced Workstation Automation Suite ---


@tool(
    "pc_list_windows",
    "List all visible open application windows on the owner's PC desktop with their window titles and PIDs.",
)
async def pc_list_windows(ctx: ToolContext) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    wins = await asyncio.to_thread(_enum_visible_windows)
    if not wins:
        return "No visible application windows found."
    lines = [f"🪟 Open Desktop Windows ({len(wins)}):"]
    for w in wins[:25]:
        lines.append(f"- [PID {w['pid']}] {w['title']}")
    return "\n".join(lines)


@tool(
    "pc_window_control",
    "Manage open desktop windows on the PC: focus (bring to front), minimize, maximize, restore, close, or minimize_all.",
    {
        "action": {
            "type": "string",
            "enum": ["focus", "minimize", "maximize", "restore", "close", "minimize_all"],
        },
        "window_title": {
            "type": "string",
            "description": "Window title or app keyword (not required for 'minimize_all').",
        },
    },
    ["action"],
)
async def pc_window_control(
    ctx: ToolContext, action: str, window_title: str = ""
) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    if os.name != "nt":
        return "Window control is only supported on Windows."

    user32 = ctypes.windll.user32
    act = action.strip().lower()

    if act == "minimize_all":
        for w in _enum_visible_windows():
            user32.ShowWindow(w["hwnd"], 6)  # SW_MINIMIZE
        return "Minimized all visible desktop windows."

    match = await asyncio.to_thread(_find_window_by_title, window_title)
    if not match:
        return f"No open window matching '{window_title}' found."

    hwnd = match["hwnd"]
    title = match["title"]
    if act == "focus":
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return f"Focused window: '{title}'."
    if act == "minimize":
        user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
        return f"Minimized window: '{title}'."
    if act == "maximize":
        user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
        return f"Maximized window: '{title}'."
    if act == "restore":
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        return f"Restored window: '{title}'."
    if act == "close":
        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        return f"Sent close signal to window: '{title}'."
    return f"Unsupported window action: {action}"


def get_top_processes_list(limit: int = 8) -> list[dict[str, Any]]:
    """Return top memory-consuming processes on Windows as dicts [{name, pid, mem_mb}]."""
    if os.name != "nt":
        return []
    try:
        ps = (
            f"Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First {max(1, min(int(limit), 20))} "
            "Id,ProcessName,@{Name='MB';Expression={[math]::Round($_.WorkingSet64 / 1MB, 1)}} | ConvertTo-Json -Compress"
        )
        res = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if res.returncode == 0 and res.stdout.strip():
            import json

            data = json.loads(res.stdout.strip())
            if isinstance(data, dict):
                data = [data]
            return [
                {
                    "pid": int(p.get("Id", 0)),
                    "name": str(p.get("ProcessName", "")),
                    "mem_mb": float(p.get("MB", 0.0)),
                }
                for p in data
            ]
    except Exception:
        pass
    return []


@tool(
    "pc_top_processes",
    "List the top memory-consuming processes currently running on the owner's PC.",
    {
        "limit": {
            "type": "integer",
            "description": "Number of top processes to return (default 8, max 20).",
        }
    },
)
async def pc_top_processes(ctx: ToolContext, limit: int = 8) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    procs = await asyncio.to_thread(get_top_processes_list, limit)
    if not procs:
        return "Could not retrieve process list."
    lines = ["⚙️ Top Processes by RAM Usage:"]
    for p in procs:
        lines.append(f"- {p['name']} (PID {p['pid']}): {p['mem_mb']:.1f} MB")
    return "\n".join(lines)


_PROTECTED_PROCESSES = {
    "system",
    "idle",
    "registry",
    "smss",
    "csrss",
    "wininit",
    "services",
    "lsass",
    "svchost",
    "winlogon",
    "explorer",
    "dwm",
    "fontdrvhost",
    "spoolsv",
}


@tool(
    "pc_kill_process",
    "Terminate a frozen or resource-heavy application process on the PC by PID or process name.",
    {
        "target": {
            "type": "string",
            "description": "Numeric PID (e.g. '12345') or process name (e.g. 'notepad', 'chrome').",
        }
    },
    ["target"],
)
async def pc_kill_process(ctx: ToolContext, target: str) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    if os.name != "nt":
        return "Process termination is only configured for Windows."

    cleaned = target.strip()
    if not cleaned:
        return "Please specify a PID or process name."

    my_pid = os.getpid()
    if cleaned.isdigit():
        pid = int(cleaned)
        if pid in (0, 4, my_pid):
            return f"Refusing to terminate protected PID {pid}."
        res = await asyncio.to_thread(
            subprocess.run,
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if res.returncode == 0:
            return f"🛑 Terminated process PID {pid}."
        return f"Could not terminate PID {pid}: {(res.stderr or res.stdout).strip()[:160]}"

    base_name = cleaned.lower().removesuffix(".exe")
    if base_name in _PROTECTED_PROCESSES or base_name in ("python", "py"):
        return f"Refusing to mass-kill protected/runtime process '{cleaned}'. Use a specific non-Jarvis PID if needed."

    exe_name = f"{base_name}.exe"
    res = await asyncio.to_thread(
        subprocess.run,
        ["taskkill", "/IM", exe_name, "/F"],
        capture_output=True,
        text=True,
        timeout=8,
    )
    if res.returncode == 0:
        return f"🛑 Terminated process '{exe_name}'."
    return f"Could not terminate '{exe_name}': {(res.stderr or res.stdout).strip()[:160]}"


@tool(
    "pc_power_control",
    "Control PC power and monitors: 'display_off' (turn off screen), 'sleep', 'hibernate', "
    "'shutdown' (scheduled with safety delay), 'restart', or 'cancel_shutdown'.",
    {
        "action": {
            "type": "string",
            "enum": [
                "display_off",
                "sleep",
                "hibernate",
                "shutdown",
                "restart",
                "cancel_shutdown",
            ],
        },
        "delay_seconds": {
            "type": "integer",
            "description": "Delay in seconds for shutdown/restart (default 60, so you can cancel if needed).",
        },
    },
    ["action"],
)
async def pc_power_control(
    ctx: ToolContext, action: str, delay_seconds: int = 60
) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    if os.name != "nt":
        return "Power control is only supported on Windows."

    act = action.strip().lower()
    if act == "display_off":
        # WM_SYSCOMMAND (0x0112), SC_MONITORPOWER (0xF170), 2 (power off)
        ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, 2)
        return "🖥 PC display turned off."

    if act == "sleep":
        ctypes.windll.powrprof.SetSuspendState(0, 1, 0)
        return "💤 Putting PC to sleep."

    if act == "hibernate":
        ctypes.windll.powrprof.SetSuspendState(1, 1, 0)
        return "❄️ Hibernating PC."

    if act == "cancel_shutdown":
        await asyncio.to_thread(
            subprocess.run, ["shutdown", "/a"], capture_output=True, timeout=5
        )
        return "✅ Any pending PC shutdown/restart has been cancelled."

    delay = max(10, min(int(delay_seconds or 60), 7200))
    flag = "/s" if act == "shutdown" else "/r"
    await asyncio.to_thread(
        subprocess.run,
        ["shutdown", flag, "/t", str(delay)],
        capture_output=True,
        timeout=5,
    )
    return (
        f"⚠️ PC {act} scheduled in {delay} seconds. "
        "Send `/pc` or tell me 'cancel shutdown' (`pc_power_control(action='cancel_shutdown')`) to abort."
    )


_CAFFEINE_ACTIVE = False


@tool(
    "pc_stay_awake",
    "Toggle Windows Caffeine / Keep-Awake mode so the PC and display do not go to sleep during long downloads, renders, or builds.",
    {
        "enabled": {
            "type": "boolean",
            "description": "True to keep PC awake, False to restore normal power sleep timer.",
        }
    },
    ["enabled"],
)
async def pc_stay_awake(ctx: ToolContext, enabled: bool = True) -> str:
    global _CAFFEINE_ACTIVE
    err = _require_owner(ctx)
    if err:
        return err
    if os.name == "nt":
        # ES_CONTINUOUS = 0x80000000, ES_SYSTEM_REQUIRED = 0x00000001, ES_DISPLAY_REQUIRED = 0x00000002
        flags = 0x80000000 | (0x00000003 if enabled else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags))
    _CAFFEINE_ACTIVE = bool(enabled)
    return (
        "☕ Keep-Awake (Caffeine) Mode ENABLED: PC will not sleep until disabled."
        if enabled
        else "💤 Keep-Awake Mode DISABLED: normal Windows sleep timers restored."
    )


def _collect_network_info() -> str:
    import socket

    hostname = socket.gethostname()
    lines = [f"🌐 Hostname: {hostname}"]
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            lines.append(f"LAN IPv4: {s.getsockname()[0]}")
        finally:
            s.close()
    except Exception:
        pass

    if os.name == "nt":
        try:
            res = subprocess.run(
                ["netsh", "wlan", "show", "interfaces"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    stripped = line.strip()
                    if stripped.startswith(("SSID", "Signal", "State", "Radio type")) and not stripped.startswith("BSSID"):
                        lines.append(f"Wi-Fi {stripped}")
        except Exception:
            pass
    return "\n".join(lines)


@tool(
    "pc_network_info",
    "Check the PC's local LAN IP address, hostname, and active Wi-Fi SSID / signal strength.",
)
async def pc_network_info(ctx: ToolContext) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    return await asyncio.to_thread(_collect_network_info)


_DIAG_COMMANDS: dict[str, list[str]] = {
    "git_status": ["git", "status", "-sb"],
    "git_log": ["git", "log", "--oneline", "-n", "8"],
    "docker_ps": ["docker", "ps", "--format", "table {{.Names}}\t{{.Status}}\t{{.Ports}}"],
    "ipconfig": ["ipconfig"],
    "netstat_listening": ["netstat", "-ano", "-p", "TCP"],
    "gpu_nvidia_smi": [
        "nvidia-smi",
        "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total",
        "--format=csv,noheader",
    ],
    "python_env": ["py", "--version"],
}


@tool(
    "pc_run_diagnostic",
    "Run a safe, read-only workstation diagnostic command on the PC (git_status, git_log, docker_ps, ipconfig, netstat_listening, gpu_nvidia_smi, python_env).",
    {
        "command": {
            "type": "string",
            "enum": sorted(_DIAG_COMMANDS.keys()),
            "description": "Allowlisted diagnostic command to run.",
        }
    },
    ["command"],
)
async def pc_run_diagnostic(ctx: ToolContext, command: str) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    cmd = _DIAG_COMMANDS.get(command)
    if not cmd:
        return f"Unknown diagnostic '{command}'."
    try:
        res = await asyncio.to_thread(
            subprocess.run,
            cmd,
            cwd=str(config.BASE_DIR),
            capture_output=True,
            text=True,
            timeout=10,
        )
        out = (res.stdout or "") + ("\n" + res.stderr if res.stderr else "")
        out = out.strip() or "(No output)"
        if command == "netstat_listening":
            listening = [ln for ln in out.splitlines() if "LISTENING" in ln][:20]
            out = "\n".join(listening) or out[:1500]
        return f"🖥 Diagnostic ({command}):\n{out[:2000]}"
    except FileNotFoundError:
        return f"Tool for '{command}' ({cmd[0]}) is not installed on this PC."
    except Exception as exc:
        return f"Diagnostic '{command}' failed: {exc}"


@tool(
    "pc_download_url",
    "Download a file from any public http(s) URL directly onto the owner's PC (Downloads or Inbox folder).",
    {
        "url": {"type": "string", "description": "Direct http(s) URL of the file to download."},
        "folder": {
            "type": "string",
            "enum": ["downloads", "inbox", "desktop", "documents"],
            "description": "Destination PC folder (default 'downloads').",
        },
        "filename": {
            "type": "string",
            "description": "Optional custom filename.",
        },
    },
    ["url"],
)
async def pc_download_url(
    ctx: ToolContext, url: str, folder: str = "downloads", filename: str = ""
) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    from urllib.parse import urlparse
    from tools.web import safe_get

    dest_dir = _resolve_pc_folder(folder) or config.INBOX_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)

    resp = await safe_get(url.strip(), timeout=35.0)
    resp.raise_for_status()

    raw_name = filename.strip()
    if not raw_name:
        parsed_path = urlparse(str(resp.url)).path
        raw_name = Path(parsed_path).name or "downloaded_file"
    safe_name = "".join(c for c in raw_name if c.isalnum() or c in "._- ")[:120].strip() or "download.bin"

    target_path = dest_dir / safe_name
    counter = 1
    while target_path.exists():
        target_path = dest_dir / f"{Path(safe_name).stem}_{counter}{Path(safe_name).suffix}"
        counter += 1

    await asyncio.to_thread(target_path.write_bytes, resp.content)
    size_kb = len(resp.content) / 1024.0
    return f"📥 Downloaded '{target_path.name}' ({size_kb:.1f} KB) to PC {folder} ({target_path})."


_EXT_CATEGORIES: dict[str, tuple[str, ...]] = {
    "Images": (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"),
    "Documents": (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".md", ".csv"),
    "Archives": (".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"),
    "Installers": (".exe", ".msi", ".msix", ".bat"),
    "Code": (".py", ".js", ".ts", ".json", ".yaml", ".yml", ".html", ".css", ".sql", ".rs", ".go"),
    "Media": (".mp3", ".wav", ".ogg", ".flac", ".mp4", ".mkv", ".mov", ".avi"),
}


@tool(
    "pc_organize_downloads",
    "Tidy and auto-organize loose files in the PC's Downloads or Inbox folder into categorized subfolders (Images, Documents, Archives, Installers, Code, Media).",
    {
        "folder": {
            "type": "string",
            "enum": ["downloads", "inbox"],
            "description": "Folder to organize (default 'downloads').",
        }
    },
)
async def pc_organize_downloads(ctx: ToolContext, folder: str = "downloads") -> str:
    err = _require_owner(ctx)
    if err:
        return err
    target_dir = _resolve_pc_folder(folder)
    if target_dir is None or not target_dir.exists():
        return f"Folder '{folder}' does not exist."

    def _organize() -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in list(target_dir.iterdir()):
            if not item.is_file():
                continue
            ext = item.suffix.lower()
            placed = None
            for cat, exts in _EXT_CATEGORIES.items():
                if ext in exts:
                    placed = cat
                    break
            if not placed:
                continue
            cat_dir = target_dir / placed
            cat_dir.mkdir(exist_ok=True)
            dest = cat_dir / item.name
            idx = 1
            while dest.exists():
                dest = cat_dir / f"{item.stem}_{idx}{item.suffix}"
                idx += 1
            shutil.move(str(item), str(dest))
            counts[placed] = counts.get(placed, 0) + 1
        return counts

    moved = await asyncio.to_thread(_organize)
    total = sum(moved.values())
    if total == 0:
        return f"✨ No loose files needed organizing in {folder}."
    breakdown = ", ".join(f"{k}: {v}" for k, v in sorted(moved.items()))
    return f"🗂 Organized {total} file(s) in {folder} ({breakdown})."


@tool(
    "pc_notify",
    "Display a popup notification message on the owner's PC desktop screen.",
    {
        "title": {"type": "string", "description": "Notification title."},
        "message": {"type": "string", "description": "Message body to display on the PC screen."},
    },
    ["title", "message"],
)
async def pc_notify(ctx: ToolContext, title: str, message: str) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    if os.name == "nt":
        # MB_ICONINFORMATION (0x40) | MB_SYSTEMMODAL (0x1000) in a non-blocking background thread
        asyncio.get_running_loop().run_in_executor(
            None,
            lambda: ctypes.windll.user32.MessageBoxW(0, message[:500], f"JARVIS — {title[:80]}", 0x00001040),
        )
        return f"🔔 Displayed desktop alert on PC: '{title}'."
    return "Desktop popup notification is only supported on Windows."


@tool(
    "pc_speak_aloud",
    "Speak a message aloud directly through the owner's physical PC speakers/headphones.",
    {
        "text": {"type": "string", "description": "Message to speak aloud on the PC speakers."},
    },
    ["text"],
)
async def pc_speak_aloud(ctx: ToolContext, text: str) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    clean = (text or "").strip()[:500]
    if not clean:
        return "Please provide text to speak."
    if os.name != "nt":
        return "PC speaker playback is only supported on Windows."

    safe_speech = clean.replace("'", " ").replace('"', " ")
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.Speak('{safe_speech}')"
    )
    await asyncio.to_thread(
        subprocess.run,
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        timeout=25,
    )
    return f"🔊 Spoke aloud on PC speakers: '{clean[:100]}'"
