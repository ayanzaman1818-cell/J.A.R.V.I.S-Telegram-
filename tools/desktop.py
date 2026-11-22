"""Real-time Screen Vision (see_screen) and Native Desktop Computer-Use GUI Automation (pyautogui + pygetwindow + mss)."""

from __future__ import annotations

import asyncio
import ctypes
import io
import logging
import os
import re
import secrets
from typing import Any

import config
from database.repository import db
from tools.base import ToolContext, tool

log = logging.getLogger("jarvis.tools.desktop")

# Set Per-Monitor v2 DPI awareness before pyautogui calls legacy SetProcessDPIAware()
if os.name == "nt":
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass

# Configure PyAutoGUI safety defaults at module import
try:
    import pyautogui

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.15
except Exception:
    pyautogui = None  # type: ignore[assignment]


def _ensure_pyautogui() -> Any:
    if pyautogui is None:
        raise RuntimeError("pyautogui is not installed. Run: pip install pyautogui")
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.15
    return pyautogui


def _require_owner(ctx: ToolContext) -> str | None:
    if not config.is_owner_user(ctx.user_id or ctx.chat_id):
        return "Permission denied: only the bot owner can use desktop computer-use tools."
    return None


def _queue_desktop_confirmation(
    ctx: ToolContext, kind: str, payload: dict[str, Any], summary: str
) -> str:
    token = secrets.token_hex(4)
    user_id = ctx.user_id or ctx.chat_id or config.OWNER_ID
    pending = db.create_pending_action(
        token=token,
        user_id=user_id,
        chat_id=ctx.chat_id or user_id,
        kind=kind,
        payload=payload,
        summary=summary,
    )
    return (
        f"⚠️ Confirmation required before desktop action:\n"
        f"{pending.summary}\n"
        f"Confirmation token: {pending.token}\n"
        "Ask the user for explicit approval, then call confirm_action with this token."
    )


# --- Password & Credential Guard ---

_CREDENTIAL_PATTERNS = (
    re.compile(r"\b(?:\d[ -]*?){13,19}\b"),  # Credit/debit card numbers
    re.compile(r"\b(?:sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|ghp_[A-Za-z0-9]{30,})\b"),
    re.compile(r"(?i)\b(?:password|passwd|passcode|pin\s*code|cvv|cvc|seed\s*phrase|private\s*key)\s*[:=]"),
)

_SENSITIVE_WINDOW_KEYWORDS = (
    "password",
    "sign in",
    "log in",
    "login",
    "1password",
    "bitwarden",
    "keepass",
    "checkout",
    "payment",
    "billing",
    "bank",
    "uac",
    "registry editor",
)

_SYSTEM_SETTINGS_KEYWORDS = (
    "settings",
    "control panel",
    "powershell",
    "command prompt",
    "cmd.exe",
    "terminal",
    "task manager",
    "device manager",
    " firewall",
)

_DESTRUCTIVE_HOTKEYS = {
    ("alt", "f4"),
    ("win", "r"),
    ("ctrl", "shift", "delete"),
    ("ctrl", "shift", "del"),
    ("ctrl", "alt", "delete"),
    ("ctrl", "alt", "del"),
    ("shift", "delete"),
    ("shift", "del"),
    ("ctrl", "w"),
    ("ctrl", "q"),
    ("delete",),
    ("del",),
}


def _looks_like_credential(text: str, active_window_title: str = "") -> str | None:
    """Return a rejection reason if `text` or target window involves passwords or financial credentials."""
    for pat in _CREDENTIAL_PATTERNS:
        if pat.search(text or ""):
            return "Refusing to type potential credential, API key, or payment card number."
    win_low = (active_window_title or "").lower()
    if any(k in win_low for k in ("1password", "bitwarden", "keepass")):
        return f"Refusing to type into password manager window ('{active_window_title}')."
    return None


def _active_window_title() -> str:
    try:
        import pygetwindow as gw

        win = gw.getActiveWindow()
        if win and win.title:
            return str(win.title).strip()
    except Exception:
        pass
    if os.name == "nt":
        try:
            user32 = ctypes.windll.user32
            fg = user32.GetForegroundWindow()
            if fg:
                length = user32.GetWindowTextLengthW(fg)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(fg, buf, length + 1)
                    return buf.value.strip()
        except Exception:
            pass
    return ""


def _cursor_position() -> tuple[int, int]:
    try:
        if pyautogui is not None:
            pos = pyautogui.position()
            return int(pos[0]), int(pos[1])
    except Exception:
        pass
    if os.name == "nt":
        try:
            class _POINT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

            pt = _POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            return int(pt.x), int(pt.y)
        except Exception:
            pass
    return (0, 0)


# --- Win32 Background Window Capture (PrintWindow) & Background Input (PostMessageW) ---


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


def _find_hwnd_by_title(window_title: str) -> tuple[int, str, tuple[int, int, int, int]] | None:
    """Locate a visible window HWND, full title, and (left, top, width, height) without focusing it."""
    if os.name != "nt":
        return None
    q = (window_title or "").strip().lower()
    if not q:
        return None
    user32 = ctypes.windll.user32
    found: list[tuple[int, str, tuple[int, int, int, int]]] = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _cb(hwnd: int, _lparam: int) -> bool:
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value.strip()
                if title and q in title.lower():
                    rc = _RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rc))
                    w = int(rc.right - rc.left)
                    h = int(rc.bottom - rc.top)
                    if w > 10 and h > 10:
                        found.append((int(hwnd), title, (int(rc.left), int(rc.top), w, h)))
                        return False
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return found[0] if found else None


def _capture_window_background_pil(hwnd: int, width: int, height: int) -> Any | None:
    """Capture a window's visual surface in the background via Win32 PrintWindow (never steals focus)."""
    if os.name != "nt" or width <= 10 or height <= 10:
        return None
    try:
        from PIL import Image

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        hwnd_dc = user32.GetWindowDC(hwnd)
        if not hwnd_dc:
            return None
        mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
        old_bmp = gdi32.SelectObject(mem_dc, bmp)

        # PW_RENDERFULLCONTENT = 0x00000002 renders DirectComposition/DWM surfaces off-screen
        ok = user32.PrintWindow(hwnd, mem_dc, 2)
        if not ok:
            ok = user32.PrintWindow(hwnd, mem_dc, 0)

        bmi = _BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.biWidth = width
        bmi.biHeight = -height  # top-down DIB
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0

        buf_len = width * height * 4
        raw_buf = ctypes.create_string_buffer(buf_len)
        gdi32.GetDIBits(mem_dc, bmp, 0, height, raw_buf, ctypes.byref(bmi), 0)

        gdi32.SelectObject(mem_dc, old_bmp)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)

        if not ok:
            return None
        return Image.frombuffer("RGB", (width, height), raw_buf, "raw", "BGRX", 0, 1).copy()
    except Exception:
        return None


def _find_editable_child_hwnd(parent_hwnd: int) -> int:
    """Find the primary inner input/text control inside a background window for WM_CHAR typing."""
    if os.name != "nt" or not parent_hwnd:
        return parent_hwnd
    user32 = ctypes.windll.user32
    preferred: list[int] = []
    fallback: list[int] = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _cb(child: int, _lparam: int) -> bool:
        if user32.IsWindowVisible(child):
            cls_buf = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(child, cls_buf, 64)
            cls_name = cls_buf.value.lower()
            if any(
                k in cls_name
                for k in ("edit", "richedit", "scintilla", "chrome_renderwidget", "console", "text")
            ):
                preferred.append(int(child))
            else:
                fallback.append(int(child))
        return True

    user32.EnumChildWindows(parent_hwnd, WNDENUMPROC(_cb), 0)
    if preferred:
        return preferred[0]
    if fallback:
        return fallback[0]
    return parent_hwnd


def _makelparam(lo: int, hi: int) -> int:
    return ((hi & 0xFFFF) << 16) | (lo & 0xFFFF)


# --- Real-Time Screen Vision (mss + Pillow + Background PrintWindow) ---


def capture_screen_jpeg(
    *,
    target: str = "monitor",
    window_title: str = "",
    max_width: int = 1600,
    quality: int = 80,
) -> tuple[bytes, dict[str, Any]]:
    """Capture the primary monitor (or a background window via PrintWindow) as a resized JPEG."""
    from PIL import Image

    max_w = max(320, min(int(max_width or 1600), 3840))
    qual = max(30, min(int(quality or 80), 95))
    active_title = _active_window_title()
    cursor = _cursor_position()

    img: Image.Image | None = None
    bbox: dict[str, int] | None = None
    captured_window_title = active_title or "(Desktop)"

    if window_title.strip():
        win_info = _find_hwnd_by_title(window_title)
        if win_info:
            hwnd, full_title, (wx, wy, ww, wh) = win_info
            captured_window_title = f"{full_title} (background capture)"
            bbox = {"left": wx, "top": wy, "width": ww, "height": wh}
            # Try off-screen background PrintWindow first so we never steal focus!
            img = _capture_window_background_pil(hwnd, ww, wh)

    if img is None and (target == "window" or window_title.strip()) and bbox is None:
        wins = _list_windows_sync()
        q = window_title.strip().lower()
        chosen = None
        if q:
            chosen = next((w for w in wins if q in w["title"].lower()), None)
        elif active_title:
            chosen = next((w for w in wins if w["title"] == active_title), None)
        if chosen and chosen["width"] > 20 and chosen["height"] > 20:
            bbox = {
                "left": int(chosen["left"]),
                "top": int(chosen["top"]),
                "width": int(chosen["width"]),
                "height": int(chosen["height"]),
            }

    if img is None:
        try:
            import mss

            mss_cls = getattr(mss, "MSS", mss.mss)
            with mss_cls() as sct:
                region = bbox if bbox else sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                raw = sct.grab(region)
                img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        except Exception:
            try:
                from PIL import ImageGrab

                grab_box = (
                    (bbox["left"], bbox["top"], bbox["left"] + bbox["width"], bbox["top"] + bbox["height"])
                    if bbox
                    else None
                )
                img = ImageGrab.grab(bbox=grab_box).convert("RGB")
            except Exception:
                w = bbox["width"] if bbox else 1280
                h = bbox["height"] if bbox else 720
                img = Image.new("RGB", (w, h), (15, 23, 42))

    orig_w, orig_h = img.size
    if orig_w > max_w:
        ratio = max_w / float(orig_w)
        new_h = max(1, int(orig_h * ratio))
        img = img.resize((max_w, new_h), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=qual, optimize=True)
    jpeg_bytes = buf.getvalue()

    meta = {
        "width": img.size[0],
        "height": img.size[1],
        "orig_width": orig_w,
        "orig_height": orig_h,
        "scale_x": round(orig_w / float(img.size[0]), 3) if img.size[0] else 1.0,
        "scale_y": round(orig_h / float(img.size[1]), 3) if img.size[1] else 1.0,
        "active_window": captured_window_title,
        "cursor": cursor,
        "size_kb": round(len(jpeg_bytes) / 1024.0, 1),
    }
    return jpeg_bytes, meta


@tool(
    "see_screen",
    "Capture the live primary monitor (or active/target window) as a resized JPEG (max 1600px width, quality 80) "
    "and feed the image directly into Gemini's multimodal vision pipeline so you can inspect UI elements, read text, "
    "find button coordinates, and verify UI state mid-task.",
    {
        "target": {
            "type": "string",
            "enum": ["monitor", "window"],
            "description": "Capture the full primary 'monitor' (default) or the active/matching 'window'.",
        },
        "window_title": {
            "type": "string",
            "description": "Optional window title substring to capture.",
        },
    },
)
async def see_screen(
    ctx: ToolContext, target: str = "monitor", window_title: str = ""
) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    jpeg_bytes, meta = await asyncio.to_thread(
        capture_screen_jpeg,
        target=target,
        window_title=window_title,
        max_width=1600,
        quality=80,
    )
    ctx.attach_media(jpeg_bytes, "image/jpeg")
    cx, cy = meta["cursor"]
    return (
        f"👁 Screen captured ({meta['width']}x{meta['height']} JPEG, {meta['size_kb']} KB; "
        f"native screen {meta['orig_width']}x{meta['orig_height']}, scale={meta['scale_x']}x).\n"
        f"Active window: {meta['active_window']}\n"
        f"Current cursor: ({cx}, {cy})\n"
        "The live screenshot is attached to this turn. Multiply image pixel coordinates by `scale` when calling desktop_click."
    )


# --- Window Listing & Focus (pygetwindow + Win32 fallback) ---


def _list_windows_sync() -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    try:
        import pygetwindow as gw

        for w in gw.getAllWindows():
            title = (w.title or "").strip()
            if not title or not getattr(w, "visible", True):
                continue
            if title in ("Program Manager", "Settings", "Microsoft Text Input Application"):
                continue
            windows.append(
                {
                    "title": title,
                    "left": int(getattr(w, "left", 0)),
                    "top": int(getattr(w, "top", 0)),
                    "width": int(getattr(w, "width", 0)),
                    "height": int(getattr(w, "height", 0)),
                }
            )
        if windows:
            return windows
    except Exception:
        pass

    if os.name == "nt":
        user32 = ctypes.windll.user32

        class _RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def _cb(hwnd: int, _lparam: int) -> bool:
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    title = buf.value.strip()
                    if title and title not in ("Program Manager", "Microsoft Text Input Application"):
                        rc = _RECT()
                        user32.GetWindowRect(hwnd, ctypes.byref(rc))
                        windows.append(
                            {
                                "title": title,
                                "left": int(rc.left),
                                "top": int(rc.top),
                                "width": int(rc.right - rc.left),
                                "height": int(rc.bottom - rc.top),
                            }
                        )
            return True

        user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return windows


@tool(
    "desktop_list_windows",
    "Return the titles, screen coordinates (left, top), and sizes (width x height) of open desktop windows.",
)
async def desktop_list_windows(ctx: ToolContext) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    wins = await asyncio.to_thread(_list_windows_sync)
    if not wins:
        return "No visible desktop windows found."
    lines = [f"🪟 Open Desktop Windows ({len(wins)}):"]
    for w in wins[:30]:
        lines.append(
            f"- {w['title']} — pos=({w['left']}, {w['top']}) size={w['width']}x{w['height']}"
        )
    return "\n".join(lines)


def _focus_window_sync(title: str) -> str:
    q = (title or "").strip().lower()
    if not q:
        return "Please provide a window title to focus."

    try:
        import pygetwindow as gw

        matches = [
            w for w in gw.getAllWindows() if (w.title or "").strip() and q in w.title.lower()
        ]
        if matches:
            target = matches[0]
            if getattr(target, "isMinimized", False):
                target.restore()
            try:
                target.activate()
                return f"Focused window: '{target.title}'."
            except Exception:
                pass
    except Exception:
        pass

    if os.name == "nt":
        from tools.pc_control import _find_window_by_title

        match = _find_window_by_title(title)
        if match:
            user32 = ctypes.windll.user32
            hwnd = match["hwnd"]
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            return f"Focused window: '{match['title']}'."

    return f"No open window matching '{title}' found."


@tool(
    "desktop_focus_window",
    "Bring a matching desktop window to the foreground by its title.",
    {
        "title": {
            "type": "string",
            "description": "Window title or application name substring to focus.",
        }
    },
    ["title"],
)
async def desktop_focus_window(ctx: ToolContext, title: str) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    return await asyncio.to_thread(_focus_window_sync, title)


# --- Mouse & Keyboard Automation (Background Win32 PostMessageW + PyAutoGUI fallback) ---


def _click_background_win32(
    x: int,
    y: int,
    button: str = "left",
    clicks: int = 1,
    window_title: str = "",
) -> str | None:
    """Send mouse click(s) directly to a window's Win32 message queue in the background without moving the cursor."""
    if os.name != "nt":
        return None
    user32 = ctypes.windll.user32

    target_hwnd = 0
    target_label = ""
    client_pt = _POINT(int(x), int(y))

    if window_title.strip():
        win_info = _find_hwnd_by_title(window_title)
        if not win_info:
            return f"No open window matching '{window_title}' found."
        top_hwnd, target_label, (wx, wy, ww, wh) = win_info
        # Accept either window-relative coordinates (0..ww, 0..wh) or global screen coordinates
        if 0 <= x <= ww and 0 <= y <= wh:
            screen_x, screen_y = wx + x, wy + y
        else:
            screen_x, screen_y = x, y
        pt_screen = _POINT(int(screen_x), int(screen_y))
        child = user32.ChildWindowFromPointEx(top_hwnd, _POINT(int(screen_x - wx), int(screen_y - wy)), 0x0001)
        target_hwnd = int(child or top_hwnd)
        client_pt = pt_screen
        user32.ScreenToClient(target_hwnd, ctypes.byref(client_pt))
    else:
        pt_screen = _POINT(int(x), int(y))
        target_hwnd = int(user32.WindowFromPoint(pt_screen) or 0)
        if not target_hwnd:
            return None
        length = user32.GetWindowTextLengthW(target_hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(target_hwnd, buf, length + 1)
            target_label = buf.value.strip()
        client_pt = _POINT(int(x), int(y))
        user32.ScreenToClient(target_hwnd, ctypes.byref(client_pt))

    lparam = _makelparam(int(client_pt.x), int(client_pt.y))
    if button == "right":
        msg_down, msg_up, wparam = 0x0204, 0x0205, 0x0002  # WM_RBUTTONDOWN / UP
    elif button == "middle":
        msg_down, msg_up, wparam = 0x0207, 0x0208, 0x0010  # WM_MBUTTONDOWN / UP
    else:
        msg_down, msg_up, wparam = 0x0201, 0x0202, 0x0001  # WM_LBUTTONDOWN / UP

    user32.PostMessageW(target_hwnd, 0x0200, 0, lparam)  # WM_MOUSEMOVE
    for _ in range(max(1, min(int(clicks), 3))):
        user32.PostMessageW(target_hwnd, msg_down, wparam, lparam)
        user32.PostMessageW(target_hwnd, msg_up, 0, lparam)

    win_desc = f" in '{target_label}'" if target_label else ""
    return f"Background-clicked {button} ({clicks}x) at ({x}, {y}){win_desc} (cursor & focus untouched)."


def _click_sync(
    x: int,
    y: int,
    button: str = "left",
    clicks: int = 1,
    window_title: str = "",
    background: bool = True,
) -> str:
    if x == 0 and y == 0:
        raise RuntimeError(
            "PyAutoGUI FAILSAFE triggered: refusing to click (0, 0) fail-safe corner."
        )
    if background and os.name == "nt":
        bg_res = _click_background_win32(
            int(x), int(y), button=button, clicks=int(clicks), window_title=window_title
        )
        if bg_res:
            return bg_res

    ag = _ensure_pyautogui()
    sw, sh = ag.size()
    if not (0 <= x < sw and 0 <= y < sh):
        raise ValueError(f"Coordinates ({x}, {y}) are outside screen bounds ({sw}x{sh}).")
    ag.click(x=int(x), y=int(y), clicks=max(1, min(int(clicks), 3)), button=button)
    return f"Clicked {button} ({clicks}x) at ({x}, {y})."


@tool(
    "desktop_click",
    "Click at coordinates (x, y) in a desktop window. Runs IN THE BACKGROUND by default via Win32 PostMessageW "
    "so the user's physical mouse cursor and active window focus are never interrupted.",
    {
        "x": {"type": "integer", "description": "Horizontal coordinate in pixels."},
        "y": {"type": "integer", "description": "Vertical coordinate in pixels."},
        "button": {
            "type": "string",
            "enum": ["left", "right", "middle"],
            "description": "Mouse button to click (default 'left').",
        },
        "clicks": {
            "type": "integer",
            "description": "Number of clicks (1 for single-click, 2 for double-click).",
        },
        "window_title": {
            "type": "string",
            "description": "Optional target window title to click inside in the background.",
        },
        "background": {
            "type": "boolean",
            "description": "Execute silently in the background without moving the user's mouse cursor (default true).",
        },
        "require_confirm": {
            "type": "boolean",
            "description": "Set True if this click submits a form, deletes data, or changes system settings.",
        },
    },
    ["x", "y"],
)
async def desktop_click(
    ctx: ToolContext,
    x: int,
    y: int,
    button: str = "left",
    clicks: int = 1,
    window_title: str = "",
    background: bool = True,
    require_confirm: bool = False,
) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    if x == 0 and y == 0:
        return "Aborted: (0, 0) is reserved for PyAutoGUI FAILSAFE emergency stop."

    target_win = window_title.strip() or _active_window_title()
    if require_confirm or any(k in target_win.lower() for k in _SENSITIVE_WINDOW_KEYWORDS):
        return _queue_desktop_confirmation(
            ctx,
            "desktop_click",
            {
                "x": int(x),
                "y": int(y),
                "button": button,
                "clicks": int(clicks),
                "window_title": window_title,
                "background": bool(background),
            },
            f"Click {button} ({clicks}x) at ({x}, {y}) in window '{target_win or 'Desktop'}'.",
        )
    return await asyncio.to_thread(
        _click_sync, int(x), int(y), button, int(clicks), window_title, bool(background)
    )


def _type_background_win32(
    text: str, press_enter: bool = False, window_title: str = ""
) -> str | None:
    """Send characters directly to a background window via Win32 WM_CHAR without stealing keyboard focus."""
    if os.name != "nt":
        return None
    user32 = ctypes.windll.user32
    top_hwnd = 0
    win_label = ""

    if window_title.strip():
        win_info = _find_hwnd_by_title(window_title)
        if not win_info:
            return f"No open window matching '{window_title}' found."
        top_hwnd, win_label, _ = win_info
    else:
        top_hwnd = int(user32.GetForegroundWindow() or 0)
        win_label = _active_window_title()

    if not top_hwnd:
        return None

    target_hwnd = _find_editable_child_hwnd(top_hwnd)
    WM_CHAR = 0x0102
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    VK_RETURN = 0x0D

    for ch in text:
        if ch == "\n":
            user32.PostMessageW(target_hwnd, WM_KEYDOWN, VK_RETURN, 0)
            user32.PostMessageW(target_hwnd, WM_KEYUP, VK_RETURN, 0)
        else:
            user32.PostMessageW(target_hwnd, WM_CHAR, ord(ch), 0)

    if press_enter:
        user32.PostMessageW(target_hwnd, WM_KEYDOWN, VK_RETURN, 0)
        user32.PostMessageW(target_hwnd, WM_KEYUP, VK_RETURN, 0)

    preview = text[:60] + ("..." if len(text) > 60 else "")
    dest = f" into '{win_label}'" if win_label else ""
    return (
        f"Background-typed '{preview}'{dest}"
        + (" and sent Enter" if press_enter else "")
        + " (focus untouched)."
    )


def _type_sync(
    text: str,
    press_enter: bool = False,
    window_title: str = "",
    background: bool = True,
) -> str:
    if background and os.name == "nt":
        bg_res = _type_background_win32(text, press_enter=press_enter, window_title=window_title)
        if bg_res:
            return bg_res

    ag = _ensure_pyautogui()
    if text.isascii():
        ag.write(text, interval=0.02)
    else:
        from tools.pc_control import _write_clipboard_sync

        _write_clipboard_sync(text)
        ag.hotkey("ctrl", "v")
    if press_enter:
        ag.press("enter")
    preview = text[:60] + ("..." if len(text) > 60 else "")
    return f"Typed '{preview}'" + (" and pressed Enter." if press_enter else ".")


@tool(
    "desktop_type",
    "Type text into a desktop window. Runs IN THE BACKGROUND by default via Win32 WM_CHAR messages "
    "(optionally targeting any background `window_title`) without stealing keyboard focus or interrupting the user.",
    {
        "text": {"type": "string", "description": "Text to type."},
        "window_title": {
            "type": "string",
            "description": "Optional target window title to type into in the background (e.g. 'Notepad', 'Visual Studio Code').",
        },
        "press_enter": {
            "type": "boolean",
            "description": "Send Enter after typing (requires user confirmation).",
        },
        "background": {
            "type": "boolean",
            "description": "Send keystrokes silently in the background via Win32 messages (default true).",
        },
    },
    ["text"],
)
async def desktop_type(
    ctx: ToolContext,
    text: str,
    window_title: str = "",
    press_enter: bool = False,
    background: bool = True,
) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    if not text:
        return "Cannot type empty text."

    target_win = window_title.strip() or _active_window_title()
    cred_err = _looks_like_credential(text, target_win)
    if cred_err:
        return f"⛔ Security Block: {cred_err}"

    win_low = target_win.lower()
    needs_confirm = bool(press_enter) or any(
        k in win_low for k in _SENSITIVE_WINDOW_KEYWORDS + _SYSTEM_SETTINGS_KEYWORDS
    )
    if needs_confirm:
        preview = text[:80] + ("..." if len(text) > 80 else "")
        return _queue_desktop_confirmation(
            ctx,
            "desktop_type",
            {
                "text": text,
                "press_enter": bool(press_enter),
                "window_title": window_title,
                "background": bool(background),
            },
            f"Type '{preview}'{' + press Enter' if press_enter else ''} into window '{target_win or 'Desktop'}'.",
        )

    return await asyncio.to_thread(_type_sync, text, False, window_title, bool(background))


_VK_CODE_MAP: dict[str, int] = {
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "tab": 0x09,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "win": 0x5B,
    "f1": 0x70,
    "f2": 0x71,
    "f3": 0x72,
    "f4": 0x73,
    "f5": 0x74,
    "f6": 0x75,
    "f7": 0x76,
    "f8": 0x77,
    "f9": 0x78,
    "f10": 0x79,
    "f11": 0x7A,
    "f12": 0x7B,
}


def _vk_for_key(k: str) -> int | None:
    low = k.strip().lower()
    if low in _VK_CODE_MAP:
        return _VK_CODE_MAP[low]
    if len(low) == 1 and low.isalnum():
        return ord(low.upper())
    return None


def _hotkey_background_win32(keys: list[str], window_title: str = "") -> str | None:
    if os.name != "nt" or not window_title.strip():
        return None
    win_info = _find_hwnd_by_title(window_title)
    if not win_info:
        return f"No open window matching '{window_title}' found."
    top_hwnd, win_label, _ = win_info
    target_hwnd = _find_editable_child_hwnd(top_hwnd)
    user32 = ctypes.windll.user32

    vks = [vk for k in keys if (vk := _vk_for_key(k)) is not None]
    if not vks:
        return None
    for vk in vks:
        user32.PostMessageW(target_hwnd, 0x0100, vk, 0)  # WM_KEYDOWN
    for vk in reversed(vks):
        user32.PostMessageW(target_hwnd, 0x0101, vk, 0)  # WM_KEYUP
    return f"Sent background hotkey {'+'.join(keys)} to '{win_label}' (focus untouched)."


def _hotkey_sync(
    keys: list[str], window_title: str = "", background: bool = True
) -> str:
    norm = [k.strip().lower() for k in keys if k and k.strip()]
    if not norm:
        return "No keys provided."
    if background and window_title.strip() and os.name == "nt":
        bg_res = _hotkey_background_win32(norm, window_title=window_title)
        if bg_res:
            return bg_res
    ag = _ensure_pyautogui()
    ag.hotkey(*norm)
    return f"Triggered hotkey: {'+'.join(norm)}."


@tool(
    "desktop_hotkey",
    "Trigger a keyboard shortcut on the desktop (e.g. ['ctrl', 's'], ['ctrl', 'c']). "
    "Pass `window_title` to send the keystrokes directly to a background window without stealing focus.",
    {
        "keys": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Ordered list of keys to press together, e.g. ['ctrl', 's'].",
        },
        "window_title": {
            "type": "string",
            "description": "Optional target window title to receive the shortcut in the background.",
        },
        "background": {
            "type": "boolean",
            "description": "Send in the background when window_title is provided (default true).",
        },
    },
    ["keys"],
)
async def desktop_hotkey(
    ctx: ToolContext,
    keys: list[str],
    window_title: str = "",
    background: bool = True,
) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    norm = tuple(k.strip().lower() for k in (keys or []) if k and k.strip())
    if not norm:
        return "Please provide at least one key."

    target_win = window_title.strip() or _active_window_title()
    win_low = target_win.lower()
    is_destructive = (
        norm in _DESTRUCTIVE_HOTKEYS
        or (norm == ("enter",) and any(k in win_low for k in _SENSITIVE_WINDOW_KEYWORDS + _SYSTEM_SETTINGS_KEYWORDS))
    )
    if is_destructive:
        combo = "+".join(norm)
        return _queue_desktop_confirmation(
            ctx,
            "desktop_hotkey",
            {
                "keys": list(norm),
                "window_title": window_title,
                "background": bool(background),
            },
            f"Send keyboard shortcut [{combo}] to window '{target_win or 'Desktop'}'.",
        )

    return await asyncio.to_thread(_hotkey_sync, list(norm), window_title, bool(background))


def _scroll_background_win32(clicks: int, window_title: str = "") -> str | None:
    if os.name != "nt":
        return None
    user32 = ctypes.windll.user32
    target_hwnd = 0
    win_label = ""
    cx, cy = _cursor_position()

    if window_title.strip():
        win_info = _find_hwnd_by_title(window_title)
        if not win_info:
            return f"No open window matching '{window_title}' found."
        target_hwnd, win_label, (wx, wy, ww, wh) = win_info
        cx, cy = wx + ww // 2, wy + wh // 2
    else:
        target_hwnd = int(user32.WindowFromPoint(_POINT(cx, cy)) or 0)

    if not target_hwnd:
        return None

    # WM_MOUSEWHEEL = 0x020A; high word of wParam is signed wheel delta (multiples of 120)
    steps = max(-20, min(20, int(clicks))) or (1 if clicks > 0 else -1)
    delta = (steps * 120) & 0xFFFF
    wparam = delta << 16
    lparam = _makelparam(cx, cy)
    user32.PostMessageW(target_hwnd, 0x020A, wparam, lparam)
    direction = "up" if clicks > 0 else "down"
    dest = f" in '{win_label}'" if win_label else ""
    return f"Background-scrolled {direction} ({abs(int(clicks))} ticks){dest} (focus untouched)."


def _scroll_sync(
    clicks: int, window_title: str = "", background: bool = True
) -> str:
    if background and os.name == "nt":
        bg_res = _scroll_background_win32(int(clicks), window_title=window_title)
        if bg_res:
            return bg_res
    ag = _ensure_pyautogui()
    ag.scroll(int(clicks))
    direction = "up" if clicks > 0 else "down"
    return f"Scrolled {direction} ({abs(int(clicks))} ticks) at cursor {_cursor_position()}."


@tool(
    "desktop_scroll",
    "Scroll up (positive clicks) or down (negative clicks) in a window. Runs IN THE BACKGROUND by default via Win32 WM_MOUSEWHEEL.",
    {
        "clicks": {
            "type": "integer",
            "description": "Scroll amount: positive to scroll up, negative to scroll down.",
        },
        "window_title": {
            "type": "string",
            "description": "Optional target window title to scroll in the background.",
        },
        "background": {
            "type": "boolean",
            "description": "Scroll in the background without stealing focus (default true).",
        },
    },
    ["clicks"],
)
async def desktop_scroll(
    ctx: ToolContext,
    clicks: int,
    window_title: str = "",
    background: bool = True,
) -> str:
    err = _require_owner(ctx)
    if err:
        return err
    return await asyncio.to_thread(_scroll_sync, int(clicks), window_title, bool(background))


async def execute_confirmed_desktop_action(kind: str, payload: dict[str, Any]) -> str:
    """Execute a desktop GUI action after the user explicitly confirms its pending_action token."""
    if kind == "desktop_click":
        return await asyncio.to_thread(
            _click_sync,
            int(payload["x"]),
            int(payload["y"]),
            str(payload.get("button", "left")),
            int(payload.get("clicks", 1)),
            str(payload.get("window_title", "")),
            bool(payload.get("background", True)),
        )
    if kind == "desktop_type":
        text = str(payload.get("text", ""))
        win_title = str(payload.get("window_title", ""))
        cred_err = _looks_like_credential(text, win_title or _active_window_title())
        if cred_err:
            return f"⛔ Security Block: {cred_err}"
        return await asyncio.to_thread(
            _type_sync,
            text,
            bool(payload.get("press_enter", False)),
            win_title,
            bool(payload.get("background", True)),
        )
    if kind == "desktop_hotkey":
        return await asyncio.to_thread(
            _hotkey_sync,
            list(payload.get("keys") or []),
            str(payload.get("window_title", "")),
            bool(payload.get("background", True)),
        )
    return f"Unknown desktop confirmation kind: {kind}"
