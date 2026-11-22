"""Dynamic Island / Top-Notch Desktop Companion Overlay (getjarvis.eu / Merlin style).

Why the notch NEVER slides from the right and is always visible on Windows DWM:
- Uses a FIXED-CANVAS top-centered translucent window (`CANVAS_W = 580, CANVAS_H = 540`) permanently positioned at:
  `x = (screen_width - CANVAS_W) // 2, y = 0`.
- The OS window's `(x, y, width, height)` NEVER changes after startup, so Windows DWM and Qt layouts never shift `x` or snap the window width to the right.
- Inside this motionless canvas, `cx = CANVAS_W / 2` is permanently locked to the exact center pixel of the monitor.
- On every 60fps animation frame (`QVariantAnimation`), the black Dynamic Island notch is painted symmetrically around `cx` from `cx - notch_w/2` to `cx + notch_w/2` (height `notch_h`), and `setMask()` updates the clickable region to that exact centered rectangle so everything outside the current notch is 100% OS click-through.
"""

from __future__ import annotations

import asyncio
import ctypes
import html
import logging
import math
import os
import re
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import config
from database.repository import db
from tools.base import ToolContext

log = logging.getLogger("jarvis.desktop_overlay")

_OVERLAY_INSTANCE: DesktopOverlayService | None = None


def _ensure_stdlib_before_pyside() -> None:
    """Resolve Python 3.15 lazy stdlib attributes and silence redundant Qt DPI warning."""
    import concurrent.futures
    import multiprocessing

    os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.window=false")
    _ = (
        concurrent.futures.ThreadPoolExecutor,
        concurrent.futures.ProcessPoolExecutor,
        multiprocessing.Process,
    )


# --- Native Windows MCI Microphone Recorder & Audio Player ---


class _WindowsMicRecorder:
    """Record microphone audio to a temporary WAV file using Win32 winmm.dll MCI or sounddevice."""

    def __init__(self) -> None:
        self._recording = False
        self._alias = "jarvis_ptt_mic"
        self._wav_path: str = ""
        self._sd_frames: list[Any] = []
        self._sd_stream: Any = None

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start(self) -> bool:
        if self._recording:
            return True
        fd, self._wav_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)

        try:
            import numpy as np  # noqa: F401
            import sounddevice as sd

            self._sd_frames = []

            def _cb(indata: Any, _frames: int, _time: Any, _status: Any) -> None:
                self._sd_frames.append(indata.copy())

            self._sd_stream = sd.InputStream(
                samplerate=16000, channels=1, dtype="int16", callback=_cb
            )
            self._sd_stream.start()
            self._recording = True
            return True
        except Exception:
            self._sd_stream = None

        if os.name == "nt":
            winmm = ctypes.windll.winmm
            winmm.mciSendStringW(f"close {self._alias}", None, 0, 0)
            rc1 = winmm.mciSendStringW(
                f"open new type waveaudio alias {self._alias}", None, 0, 0
            )
            rc2 = winmm.mciSendStringW(f"record {self._alias}", None, 0, 0)
            if rc1 == 0 and rc2 == 0:
                self._recording = True
                return True
        return False

    def stop(self) -> bytes:
        if not self._recording:
            return b""
        self._recording = False

        if self._sd_stream is not None:
            try:
                import wave
                import numpy as np

                self._sd_stream.stop()
                self._sd_stream.close()
                self._sd_stream = None
                if self._sd_frames:
                    audio = np.concatenate(self._sd_frames, axis=0)
                    with wave.open(self._wav_path, "wb") as wf:
                        wf.setnchannels(1)
                        wf.setsampwidth(2)
                        wf.setframerate(16000)
                        wf.writeframes(audio.tobytes())
            except Exception:
                log.debug("sounddevice stop failed", exc_info=True)

        elif os.name == "nt":
            winmm = ctypes.windll.winmm
            winmm.mciSendStringW(f"stop {self._alias}", None, 0, 0)
            winmm.mciSendStringW(f'save {self._alias} "{self._wav_path}"', None, 0, 0)
            winmm.mciSendStringW(f"close {self._alias}", None, 0, 0)

        try:
            p = Path(self._wav_path)
            if p.exists() and p.stat().st_size > 100:
                return p.read_bytes()
        finally:
            try:
                if self._wav_path and os.path.exists(self._wav_path):
                    os.unlink(self._wav_path)
            except OSError:
                pass
        return b""


def play_audio_file_sync(path: str) -> None:
    """Play an MP3/WAV file aloud on the PC speakers and unlink it afterwards."""
    try:
        if os.name == "nt" and os.path.exists(path):
            ps = (
                "Add-Type -AssemblyName presentationCore; "
                "$p = New-Object System.Windows.Media.MediaPlayer; "
                f"$p.Open([Uri]::new('{path}')); "
                "$p.Play(); Start-Sleep -Milliseconds 400; "
                "while ($p.Position -lt $p.NaturalDuration.TimeSpan) { Start-Sleep -Milliseconds 200 }; "
                "$p.Close()"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                capture_output=True,
                timeout=60,
            )
    except Exception:
        log.debug("Desktop audio playback failed", exc_info=True)
    finally:
        try:
            if path and os.path.exists(path):
                os.unlink(path)
        except OSError:
            pass


def _format_reply_html(user_query: str, reply_text: str, thinking: bool = False) -> str:
    """Format conversation history as clean styled HTML with code blocks and bold text."""
    q_esc = html.escape(user_query)
    header = (
        f"<div style='margin-bottom:8px;'>"
        f"<span style='background:rgba(56,189,248,0.2);color:#7dd3fc;padding:3px 9px;"
        f"border-radius:10px;font-size:11px;font-weight:700;'>YOU</span> "
        f"<span style='color:#f8fafc;font-size:13px;font-weight:600;'>{q_esc}</span></div>"
    )
    if thinking:
        return (
            header
            + "<div style='color:#38bdf8;font-size:12.5px;font-weight:600;margin-top:6px;'>"
            "✨ JARVIS is analyzing and executing tools...</div>"
        )

    body = html.escape(reply_text or "")
    # Code blocks ```...```
    body = re.sub(
        r"```(?:\w+)?\n([\s\S]*?)```",
        r"<pre style='background:#090b10;color:#38bdf8;padding:8px 10px;border-radius:8px;"
        r"font-family:Consolas,monospace;font-size:11.5px;margin:6px 0;'>\1</pre>",
        body,
    )
    # Inline code `...`
    body = re.sub(
        r"`([^`]+)`",
        r"<code style='background:rgba(255,255,255,0.1);color:#bae6fd;padding:1px 5px;"
        r"border-radius:4px;font-family:Consolas,monospace;font-size:12px;'>\1</code>",
        body,
    )
    # Bold **...**
    body = re.sub(r"\*\*([^*]+)\*\*", r"<b style='color:#ffffff;'>\1</b>", body)
    body = body.replace("\n", "<br/>")
    return (
        header
        + "<div style='margin-top:6px;'>"
        "<span style='background:rgba(132,204,22,0.2);color:#bef264;padding:3px 9px;"
        "border-radius:10px;font-size:11px;font-weight:700;'>JARVIS</span>"
        f"<div style='color:#e2e8f0;font-size:13px;line-height:1.48;margin-top:5px;'>{body}</div></div>"
    )


# --- Core Service Class ---


class DesktopOverlayService:
    """Manages the Dynamic Island Top-Notch Overlay lifecycle, hotkeys, and shared Gemini Brain."""

    def __init__(
        self,
        *,
        owner_id: int | None = None,
        bot: Any = None,
        job_queue: Any = None,
        async_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.owner_id: int = int(owner_id or config.OWNER_ID or 1)
        self.bot = bot
        self.job_queue = job_queue
        self._external_loop = async_loop
        self._private_loop: asyncio.AbstractEventLoop | None = None

        self.auto_see_screen: bool = config.DESKTOP_AUTO_SEE_DEFAULT
        self.voice_reply_enabled: bool = True
        self.visible: bool = True
        self._busy: bool = False
        self._pending_token: str | None = None
        self._completed_suggestions: set[str] = set()
        self._mic = _WindowsMicRecorder()

        self._ui_thread: threading.Thread | None = None
        self._hotkey_listener: Any = None
        self._stop_event = threading.Event()
        self._qt_app: Any = None
        self._qt_window: Any = None

    def _get_async_loop(self) -> asyncio.AbstractEventLoop:
        if self._external_loop and self._external_loop.is_running():
            return self._external_loop
        if self._private_loop and self._private_loop.is_running():
            return self._private_loop

        loop = asyncio.new_event_loop()
        self._private_loop = loop
        t = threading.Thread(
            target=loop.run_forever, name="jarvis-overlay-async", daemon=True
        )
        t.start()
        return loop

    def _make_context(self) -> ToolContext:
        return ToolContext(
            bot=self.bot,
            chat_id=self.owner_id,
            job_queue=self.job_queue,
            user_id=self.owner_id,
        )

    async def submit_query_async(
        self,
        query_text: str,
        *,
        auto_see: bool | None = None,
        speak_reply: bool = False,
    ) -> str:
        """Submit a query (with optional live monitor screenshot) directly to brain.agent.respond()."""
        from brain.agent import respond as brain_respond
        from tools.desktop import capture_screen_jpeg

        text = (query_text or "").strip()
        if not text:
            return ""

        use_screen = self.auto_see_screen if auto_see is None else bool(auto_see)
        media_bytes: bytes | None = None
        mime_type: str | None = None

        if use_screen:
            try:
                jpeg_bytes, _meta = await asyncio.to_thread(
                    capture_screen_jpeg,
                    target="monitor",
                    max_width=1600,
                    quality=80,
                )
                media_bytes = jpeg_bytes
                mime_type = "image/jpeg"
            except Exception:
                log.warning(
                    "Auto-See Screen capture failed; continuing without image",
                    exc_info=True,
                )

        ctx = self._make_context()
        reply = await brain_respond(
            ctx,
            text,
            media_bytes=media_bytes,
            mime_type=mime_type,
        )

        pending = db.get_latest_pending_action(self.owner_id)
        if pending and pending.token in (reply or ""):
            self._pending_token = pending.token
        else:
            self._pending_token = None

        if speak_reply and reply:
            try:
                from services.voice import tts_file

                mp3_path = await tts_file(reply[:600], user_id=self.owner_id)
                threading.Thread(
                    target=play_audio_file_sync, args=(mp3_path,), daemon=True
                ).start()
            except Exception:
                log.debug("Overlay TTS playback skipped", exc_info=True)

        return reply

    async def approve_pending_async(self) -> str:
        if not self._pending_token:
            return "No pending action to approve."
        from tools.integrations import confirm_action

        token = self._pending_token
        self._pending_token = None
        ctx = self._make_context()
        return await confirm_action(ctx, token=token)

    def mark_suggestion_done(self, suggestion_id: str) -> None:
        """Mark a suggestion as completed so it is removed from the Suggestions icon badge and list."""
        if suggestion_id:
            self._completed_suggestions.add(suggestion_id)

    def fetch_composio_hub_cards(self) -> list[dict[str, Any]]:
        """Return all connectable Composio apps (unconnected first, then connected) without blocking the UI thread."""
        from tools import integrations as integ

        # Trigger a non-blocking background refresh if cache is empty or stale
        if integ.composio_enabled() and (time.time() - integ._CACHE_TS > 45.0):
            threading.Thread(
                target=integ.get_connected_toolkits_sync, args=(True,), daemon=True
            ).start()

        active = dict(integ._CONNECTED_ACCOUNTS_CACHE)
        if config.COMPOSIO_GMAIL_ACCOUNT:
            active.setdefault("gmail", config.COMPOSIO_GMAIL_ACCOUNT)
        if config.COMPOSIO_GOOGLEDRIVE_ACCOUNT:
            active.setdefault("googledrive", config.COMPOSIO_GOOGLEDRIVE_ACCOUNT)
        if config.COMPOSIO_GOOGLECALENDAR_ACCOUNT:
            active.setdefault("googlecalendar", config.COMPOSIO_GOOGLECALENDAR_ACCOUNT)

        cards: list[dict[str, Any]] = []
        for app in integ.POPULAR_COMPOSIO_APPS:
            tk = app["toolkit"]
            ca_id = active.get(tk, "")
            cards.append(
                {
                    "toolkit": tk,
                    "name": app["name"],
                    "icon": app["icon"],
                    "desc": app["desc"],
                    "connected": bool(ca_id),
                    "account_id": ca_id,
                }
            )
        # Show apps that can be connected first, followed by already-connected apps
        cards.sort(key=lambda c: 1 if c["connected"] else 0)
        return cards

    def fetch_live_suggestions(self) -> list[dict[str, str]]:
        """Build live contextual suggestion cards, excluding any that the user has already completed."""
        now_time = datetime.now(config.get_zoneinfo()).strftime("today %H:%M")
        cards: list[dict[str, str]] = [
            {
                "id": "screen_analyze",
                "icon": "prism",
                "title": "Analyze what's on your screen",
                "meta": f"Your screen · {now_time}",
                "body": "Inspect your active window, debug errors, or summarize content.",
                "btn": "Tell me more",
                "prompt": "Look at my screen right now, tell me what I'm working on, and offer helpful suggestions.",
                "auto_see": "true",
            }
        ]

        try:
            watches = db.list_price_watches(user_id=self.owner_id)
        except Exception:
            watches = []
        if watches:
            w = watches[0]
            last_p = (
                f"Last: {w['last_price']} {w['currency']}"
                if w.get("last_price")
                else f"Target: {w['target_price']} {w['currency']}"
            )
            cards.append(
                {
                    "id": "price_watch",
                    "icon": "cloud",
                    "title": f"{w['product_name'][:34]} Price Tracking",
                    "meta": last_p,
                    "body": "",
                    "btn": "Show more",
                    "prompt": "Check my active price watches and give me a status update.",
                    "auto_see": "false",
                }
            )
        else:
            cards.append(
                {
                    "id": "price_watch",
                    "icon": "cloud",
                    "title": "RTX 5060 Ti Price Tracking",
                    "meta": "6m",
                    "body": "",
                    "btn": "Show more",
                    "prompt": "List my active price watches or tell me how to track a product price.",
                    "auto_see": "false",
                }
            )

        try:
            tasks = db.list_tasks(status="open", limit=5, user_id=self.owner_id)
        except Exception:
            tasks = []
        task_desc = (
            f"You have {len(tasks)} open task(s): '{tasks[0]['title'][:42]}'..."
            if tasks
            else "I can connect your Google Calendar and open tasks so you're ready before each meeting."
        )
        cards.append(
            {
                "id": "meetings_prep",
                "icon": "calendar",
                "title": "Get ready for your meetings",
                "meta": "I can do this",
                "body": task_desc,
                "btn": "Show more",
                "prompt": "Check my upcoming schedule, reminders, and open high-priority tasks.",
                "auto_see": "false",
            }
        )

        try:
            notes = db.list_knowledge_notes(limit=3, user_id=self.owner_id)
        except Exception:
            notes = []
        if notes:
            n = notes[0]
            cards.append(
                {
                    "id": "second_brain",
                    "icon": "prism",
                    "title": n["title"][:40],
                    "meta": f"Second Brain · {now_time}",
                    "body": str(n.get("content", "")).replace("\n", " ")[:68],
                    "btn": "Show more",
                    "prompt": f"Summarize my Second Brain note '{n['title']}' and key takeaways.",
                    "auto_see": "false",
                }
            )
        else:
            cards.append(
                {
                    "id": "second_brain",
                    "icon": "prism",
                    "title": "Autonomous Student & Second Brain",
                    "meta": f"Self-Study · {now_time}",
                    "body": "Review what I've studied on the web or quiz yourself with flashcards.",
                    "btn": "Show more",
                    "prompt": "Show me your current autonomous study curriculum and latest study notes.",
                    "auto_see": "false",
                }
            )
        remaining = [c for c in cards if c["id"] not in self._completed_suggestions]
        return remaining[:4]

    # --- Global Hotkeys (Ctrl+/ -> Chat Mode, Ctrl+Alt+Space -> Voice Orb Mode) ---

    def _start_hotkey_listener(self) -> None:
        try:
            from pynput import keyboard as pynput_kb

            hotkeys = {
                "<ctrl>+<alt>+<space>": self.toggle_voice_hotkey_threadsafe,
                "<ctrl>+/": self.toggle_chat_hotkey_threadsafe,
            }
            self._hotkey_listener = pynput_kb.GlobalHotKeys(hotkeys)
            self._hotkey_listener.daemon = True
            self._hotkey_listener.start()
            log.info(
                "Dynamic Notch global hotkeys registered (Ctrl+/ = Chat Mode, Ctrl+Alt+Space = Voice Mode)"
            )
            return
        except Exception:
            log.debug("pynput GlobalHotKeys fallback to Win32 RegisterHotKey", exc_info=True)

        if os.name == "nt":
            threading.Thread(
                target=self._win32_hotkey_loop, name="jarvis-win32-hotkey", daemon=True
            ).start()

    def _win32_hotkey_loop(self) -> None:
        user32 = ctypes.windll.user32
        # ID 1 = Ctrl+Alt+Space (MOD_ALT 0x0001 | MOD_CONTROL 0x0002, VK_SPACE 0x20) -> Voice Mode
        user32.RegisterHotKey(None, 1, 0x0001 | 0x0002, 0x20)
        # ID 2 = Ctrl+/ (MOD_CONTROL 0x0002, VK_OEM_2 0xBF) -> Chat Mode
        user32.RegisterHotKey(None, 2, 0x0002, 0xBF)

        class _MSG(ctypes.Structure):
            _fields_ = [
                ("hwnd", ctypes.c_void_p),
                ("message", ctypes.c_uint),
                ("wParam", ctypes.c_size_t),
                ("lParam", ctypes.c_ssize_t),
                ("time", ctypes.c_ulong),
                ("pt_x", ctypes.c_long),
                ("pt_y", ctypes.c_long),
            ]

        msg = _MSG()
        try:
            while not self._stop_event.is_set():
                if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    if msg.message == 0x0312:
                        if msg.wParam == 1:
                            self.toggle_voice_hotkey_threadsafe()
                        else:
                            self.toggle_chat_hotkey_threadsafe()
                time.sleep(0.05)
        finally:
            user32.UnregisterHotKey(None, 1)
            user32.UnregisterHotKey(None, 2)

    def toggle_chat_hotkey_threadsafe(self) -> None:
        if self._qt_window is not None:
            self._qt_window.hotkey_triggered.emit("chat")

    def toggle_voice_hotkey_threadsafe(self) -> None:
        if self._qt_window is not None:
            self._qt_window.hotkey_triggered.emit("voice")

    def toggle_visibility_threadsafe(self) -> None:
        self.toggle_chat_hotkey_threadsafe()

    def start(self) -> None:
        if self._ui_thread and self._ui_thread.is_alive():
            return
        self._stop_event.clear()
        self._ui_thread = threading.Thread(
            target=self._run_qt_loop, name="jarvis-dynamic-notch", daemon=True
        )
        self._ui_thread.start()
        log.info("JARVIS Dynamic Island Top-Notch Overlay started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._hotkey_listener is not None:
            try:
                self._hotkey_listener.stop()
            except Exception:
                pass
        if self._qt_window is not None:
            try:
                self._qt_window.shutdown_requested.emit()
            except Exception:
                pass
        if self._private_loop and self._private_loop.is_running():
            try:
                self._private_loop.call_soon_threadsafe(self._private_loop.stop)
            except Exception:
                pass
        if self._ui_thread and self._ui_thread.is_alive():
            self._ui_thread.join(timeout=1.2)

    def _run_qt_loop(self) -> None:
        try:
            import sys

            _ensure_stdlib_before_pyside()
            from PySide6.QtWidgets import QApplication

            app = QApplication.instance() or QApplication(sys.argv)
            self._qt_app = app
            window = _create_qt_notch_window(self)
            self._qt_window = window
            window.show()
            window.raise_()
            self._start_hotkey_listener()
            app.exec()
        except Exception:
            log.warning("PySide6 Dynamic Notch failed to initialize", exc_info=True)


# --- Fixed-Canvas Center-Locked PySide6 Dynamic Island Top-Notch Window ---


def _create_qt_notch_window(service: DesktopOverlayService) -> Any:
    _ensure_stdlib_before_pyside()
    from PySide6.QtCore import (
        QEasingCurve,
        QPointF,
        QRect,
        QRectF,
        QSize,
        QSizeF,
        Qt,
        QTimer,
        QVariantAnimation,
        Signal,
    )
    from PySide6.QtGui import (
        QBrush,
        QColor,
        QCursor,
        QFont,
        QLinearGradient,
        QPainter,
        QPainterPath,
        QPen,
        QPolygonF,
        QRadialGradient,
        QRegion,
    )
    from PySide6.QtWidgets import (
        QApplication,
        QFrame,
        QGraphicsOpacityEffect,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QPushButton,
        QScrollArea,
        QStackedWidget,
        QTextBrowser,
        QVBoxLayout,
        QWidget,
    )

    # Fixed motionless canvas dimensions anchored at top center of the primary screen
    CANVAS_W = 580
    CANVAS_H = 540

    class _VectorIconButton(QPushButton):
        """Spring-animated glassmorphic vector icon button (Home, Suggestions Sparkles, Waveform, Pin, Close, Mic)."""

        def __init__(
            self,
            icon_kind: str,
            tooltip: str = "",
            size: int = 34,
            pill_white: bool = False,
            badge_count: int = 0,
            parent: QWidget | None = None,
        ) -> None:
            super().__init__(parent)
            self.icon_kind = icon_kind
            self.active_state = False
            self.pill_white = pill_white
            self.badge_count = badge_count
            self._hovered = False
            self._hover_t: float = 0.0
            self.setFixedSize(size, size)
            self.setToolTip(tooltip)
            self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            self._hover_anim = QVariantAnimation(self)
            self._hover_anim.setDuration(240)
            self._hover_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._hover_anim.valueChanged.connect(self._on_hover_val)

        def _on_hover_val(self, val: Any) -> None:
            self._hover_t = float(val)
            self.update()

        def enterEvent(self, event: Any) -> None:
            self._hovered = True
            self._hover_anim.stop()
            self._hover_anim.setStartValue(self._hover_t)
            self._hover_anim.setEndValue(1.0)
            self._hover_anim.start()
            super().enterEvent(event)

        def leaveEvent(self, event: Any) -> None:
            self._hovered = False
            self._hover_anim.stop()
            self._hover_anim.setStartValue(self._hover_t)
            self._hover_anim.setEndValue(0.0)
            self._hover_anim.start()
            super().leaveEvent(event)

        def paintEvent(self, _event: Any) -> None:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            w, h = float(self.width()), float(self.height())
            cx, cy = w / 2.0, h / 2.0
            t = self._hover_t

            # Smooth sub-pixel scale spring on hover
            scale = 1.0 + 0.07 * t
            p.save()
            p.translate(cx, cy)
            p.scale(scale, scale)
            p.translate(-cx, -cy)

            r = w / 2.0 if self.pill_white or self.icon_kind in ("close_circle", "mic") else 10.5

            if self.pill_white:
                bg_grad = QLinearGradient(0, 0, 0, h)
                if t > 0.1:
                    bg_grad.setColorAt(0.0, QColor("#ffffff"))
                    bg_grad.setColorAt(1.0, QColor("#dbeafe"))
                else:
                    bg_grad.setColorAt(0.0, QColor("#ffffff"))
                    bg_grad.setColorAt(1.0, QColor("#e2e8f0"))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(bg_grad))
                p.drawRoundedRect(QRectF(1.5, 1.5, w - 3.0, h - 3.0), r, r)
                fg = QColor("#09090b")
            elif self.active_state:
                bg_grad = QLinearGradient(0, 0, w, h)
                bg_grad.setColorAt(0.0, QColor(56, 189, 248, int(68 + 30 * t)))
                bg_grad.setColorAt(1.0, QColor(37, 99, 235, int(48 + 25 * t)))
                p.setPen(QPen(QColor(56, 189, 248, int(130 + 60 * t)), 1.15))
                p.setBrush(QBrush(bg_grad))
                p.drawRoundedRect(QRectF(1.5, 1.5, w - 3.0, h - 3.0), r, r)
                fg = QColor("#7dd3fc")
            else:
                bg_alpha = int(20 + 34 * t)
                border_alpha = int(28 + 48 * t)
                p.setPen(QPen(QColor(255, 255, 255, border_alpha), 1.05))
                p.setBrush(QColor(255, 255, 255, bg_alpha))
                p.drawRoundedRect(QRectF(1.5, 1.5, w - 3.0, h - 3.0), r, r)
                fg = QColor("#ffffff") if t > 0.3 else QColor("#cbd5e1")

            pen = QPen(fg, 1.85, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)

            if self.icon_kind == "home":
                # Rounded modern house with glowing door
                path = QPainterPath()
                path.moveTo(cx - 7.0, cy - 0.5)
                path.lineTo(cx, cy - 7.0)
                path.lineTo(cx + 7.0, cy - 0.5)
                p.drawPath(path)
                body = QPainterPath()
                body.moveTo(cx - 5.0, cy - 1.2)
                body.lineTo(cx - 5.0, cy + 5.8)
                body.lineTo(cx + 5.0, cy + 5.8)
                body.lineTo(cx + 5.0, cy - 1.2)
                p.drawPath(body)
                p.drawLine(QPointF(cx, cy + 2.2), QPointF(cx, cy + 5.6))
            elif self.icon_kind in ("suggestions", "chat"):
                # Crisp 4-point AI Sparkle / Suggestions icon + notification dot
                sparkle = QPainterPath()
                sr = 6.8
                sparkle.moveTo(cx - 1.0, cy - sr)
                sparkle.quadTo(cx - 1.0, cy - 0.5, cx - 1.0 + sr, cy - 0.5)
                sparkle.quadTo(cx - 1.0, cy - 0.5, cx - 1.0, cy - 0.5 + sr)
                sparkle.quadTo(cx - 1.0, cy - 0.5, cx - 1.0 - sr, cy - 0.5)
                sparkle.quadTo(cx - 1.0, cy - 0.5, cx - 1.0, cy - sr)
                sparkle.closeSubpath()
                s_grad = QLinearGradient(cx - 6, cy - 6, cx + 6, cy + 6)
                s_grad.setColorAt(0.0, QColor("#fde047") if (self.active_state or t > 0.2) else fg)
                s_grad.setColorAt(1.0, QColor("#38bdf8") if (self.active_state or t > 0.2) else fg)
                p.setPen(QPen(QBrush(s_grad), 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                p.setBrush(QBrush(s_grad) if (self.active_state or t > 0.35) else Qt.BrushStyle.NoBrush)
                p.drawPath(sparkle)
                # Mini secondary star top-right
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor("#38bdf8") if not self.active_state else QColor("#fde047"))
                p.drawEllipse(QPointF(cx + 5.2, cy - 5.2), 1.65, 1.65)
                p.drawEllipse(QPointF(cx - 5.4, cy + 4.8), 1.15, 1.15)
            elif self.icon_kind == "connections":
                # Interlocking Composio Bridge Link / Nodes icon (shown once all suggestions are completed)
                c_grad = QLinearGradient(cx - 6, cy - 6, cx + 6, cy + 6)
                c_grad.setColorAt(0.0, QColor("#34d399") if (self.active_state or t > 0.2) else fg)
                c_grad.setColorAt(1.0, QColor("#38bdf8") if (self.active_state or t > 0.2) else fg)
                p.setPen(QPen(QBrush(c_grad), 1.85, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                p.drawRoundedRect(QRectF(cx - 6.8, cy - 3.6, 7.6, 5.2), 2.5, 2.5)
                p.drawRoundedRect(QRectF(cx - 0.8, cy - 1.6, 7.6, 5.2), 2.5, 2.5)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor("#34d399"))
                p.drawEllipse(QPointF(cx + 5.5, cy - 5.2), 1.7, 1.7)
            elif self.icon_kind == "waveform":
                bars = [(-5.6, 2.4), (-2.8, 5.4), (0.0, 7.2), (2.8, 4.6), (5.6, 2.4)]
                for idx, (dx, bh) in enumerate(bars):
                    c = QColor("#38bdf8") if (idx == 2 and (t > 0.2 or self.active_state)) else fg
                    p.setPen(QPen(c, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                    p.drawLine(QPointF(cx + dx, cy - bh), QPointF(cx + dx, cy + bh))
            elif self.icon_kind == "pin":
                p.save()
                p.translate(cx, cy)
                p.rotate(38 - 38 * (1.0 if self.active_state else 0.0))
                p.drawRoundedRect(QRectF(-3.4, -5.2, 6.8, 2.4), 1.1, 1.1)
                p.drawLine(QPointF(-2.2, -2.8), QPointF(-3.2, 1.2))
                p.drawLine(QPointF(2.2, -2.8), QPointF(3.2, 1.2))
                p.drawLine(QPointF(-4.4, 1.2), QPointF(4.4, 1.2))
                p.drawLine(QPointF(0.0, 1.2), QPointF(0.0, 6.0))
                p.restore()
            elif self.icon_kind in ("close", "close_circle"):
                p.drawLine(QPointF(cx - 4.0, cy - 4.0), QPointF(cx + 4.0, cy + 4.0))
                p.drawLine(QPointF(cx + 4.0, cy - 4.0), QPointF(cx - 4.0, cy + 4.0))
            elif self.icon_kind == "mic":
                p.setBrush(fg)
                p.drawRoundedRect(QRectF(cx - 2.5, cy - 6.6, 5.0, 8.0), 2.5, 2.5)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawArc(QRectF(cx - 5.2, cy - 4.6, 10.4, 9.0), 180 * 16, 180 * 16)
                p.drawLine(QPointF(cx, cy + 4.4), QPointF(cx, cy + 6.8))
                p.drawLine(QPointF(cx - 2.6, cy + 6.8), QPointF(cx + 2.6, cy + 6.8))

            p.restore()

            # Optional notification badge pill (e.g. number of live suggestions stored inside the icon)
            if self.badge_count > 0:
                p.setPen(QPen(QColor("#07080b"), 1.4))
                p.setBrush(QColor("#38bdf8"))
                p.drawEllipse(QPointF(w - 6.0, 6.0), 4.2, 4.2)

    class _WizardMascotWidget(QWidget):
        """Animated 3D glowing green wizard mascot ('Merlin / Jarvis') from Images 3 & 4."""

        def __init__(self, diameter: int = 58, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.setFixedSize(diameter, diameter)
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            self._phase = 0.0
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._tick)
            self._timer.start(40)

        def _tick(self) -> None:
            self._phase += 0.07
            if self.isVisible():
                self.update()

        def paintEvent(self, _event: Any) -> None:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            w = float(self.width())
            h = float(self.height())
            s = min(w, h) / 100.0
            p.scale(s, s)
            bob = 1.8 * math.sin(self._phase)
            p.translate(0.0, bob)

            # Soft green outer glow
            glow = QRadialGradient(QPointF(48, 60), 42)
            glow.setColorAt(0.0, QColor(132, 204, 22, 70))
            glow.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(glow))
            p.drawEllipse(QRectF(6, 18, 84, 80))

            # Glowing green slime body
            body_grad = QRadialGradient(QPointF(42, 54), 32)
            body_grad.setColorAt(0.0, QColor("#ecfccb"))
            body_grad.setColorAt(0.35, QColor("#a3e635"))
            body_grad.setColorAt(0.80, QColor("#4d7c0f"))
            body_grad.setColorAt(1.0, QColor("#1a2e05"))
            p.setBrush(QBrush(body_grad))
            p.drawEllipse(QRectF(25, 42, 46, 42))

            # Glowing white vertical eyes
            p.setBrush(QColor("#ffffff"))
            p.drawRoundedRect(QRectF(39, 54, 4.6, 10.2), 2.3, 2.3)
            p.drawRoundedRect(QRectF(52, 54, 4.6, 10.2), 2.3, 2.3)

            # Dark wizard hat
            hat_grad = QLinearGradient(25, 12, 72, 48)
            hat_grad.setColorAt(0.0, QColor("#52525b"))
            hat_grad.setColorAt(0.5, QColor("#27272a"))
            hat_grad.setColorAt(1.0, QColor("#09090b"))
            p.setBrush(QBrush(hat_grad))
            hat_path = QPainterPath()
            hat_path.moveTo(21, 45)
            hat_path.quadTo(48, 37, 75, 45)
            hat_path.lineTo(63, 39)
            hat_path.lineTo(54, 13)
            hat_path.quadTo(48, 9, 41, 18)
            hat_path.lineTo(33, 39)
            hat_path.closeSubpath()
            p.drawPath(hat_path)
            p.drawEllipse(QRectF(21, 41, 54, 9.5))

            # Star wand on left
            p.setPen(QPen(QColor("#a16207"), 3.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(17, 39), QPointF(29, 58))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#facc15"))
            p.drawEllipse(QRectF(11, 32, 11, 11))

            # Mini cauldron + rainbow magic swirl on right
            p.setBrush(QColor("#18181b"))
            p.setPen(QPen(QColor("#3f3f46"), 1.8))
            p.drawEllipse(QRectF(59, 66, 24, 18))
            p.setPen(QPen(QColor("#f472b6"), 2.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(QRectF(62, 54, 16, 18), 30 * 16, 140 * 16)
            p.setPen(QPen(QColor("#38bdf8"), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(QRectF(65, 50, 14, 16), 20 * 16, 130 * 16)

    class _CloudOrbWidget(QWidget):
        """Animated 60fps 3D Blue & White Cloud/Plasma Sphere Orb from Image 5."""

        clicked = Signal()

        def __init__(self, diameter: int = 152, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.setFixedSize(diameter, diameter)
            self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            self._phase: float = 0.0
            self.active_pulse: bool = False
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._tick)
            self._timer.start(25)

        def _tick(self) -> None:
            self._phase += 0.085 if self.active_pulse else 0.032
            if self.isVisible():
                self.update()

        def mousePressEvent(self, event: Any) -> None:
            if event.button() == Qt.MouseButton.LeftButton:
                self.clicked.emit()

        def paintEvent(self, _event: Any) -> None:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            w = float(self.width())
            h = float(self.height())
            cx, cy = w / 2.0, h / 2.0
            pulse = 1.0 + (0.045 if self.active_pulse else 0.015) * math.sin(self._phase)
            r = (min(w, h) / 2.0 - 9.0) * pulse

            aura = QRadialGradient(QPointF(cx, cy), r + 9.0)
            aura.setColorAt(0.0, QColor(56, 189, 248, 110 if self.active_pulse else 75))
            aura.setColorAt(0.75, QColor(2, 132, 199, 40))
            aura.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(aura))
            p.drawEllipse(QPointF(cx, cy), r + 9.0, r + 9.0)

            clip = QPainterPath()
            clip.addEllipse(QPointF(cx, cy), r, r)
            p.setClipPath(clip)

            hx = cx + r * 0.28 * math.cos(self._phase * 0.5)
            hy = cy - r * 0.34 + r * 0.08 * math.sin(self._phase * 0.7)
            sphere = QRadialGradient(QPointF(hx, hy), r * 1.35)
            sphere.setColorAt(0.0, QColor("#ffffff"))
            sphere.setColorAt(0.30, QColor("#e0f2fe"))
            sphere.setColorAt(0.56, QColor("#38bdf8"))
            sphere.setColorAt(0.84, QColor("#0284c7"))
            sphere.setColorAt(1.0, QColor("#0369a1"))
            p.setBrush(QBrush(sphere))
            p.drawEllipse(QPointF(cx, cy), r, r)

            for i in range(5):
                ang = self._phase * (0.55 + i * 0.14) + i * 1.25
                wx = cx + math.cos(ang) * (r * 0.33)
                wy = cy - r * 0.12 + math.sin(ang) * (r * 0.27)
                wisp = QRadialGradient(QPointF(wx, wy), r * 0.60)
                wisp.setColorAt(0.0, QColor(255, 255, 255, 175))
                wisp.setColorAt(0.52, QColor(224, 242, 254, 80))
                wisp.setColorAt(1.0, QColor(56, 189, 248, 0))
                p.setBrush(QBrush(wisp))
                p.drawEllipse(QPointF(wx, wy), r * 0.60, r * 0.46)

    class _SuggestionIconWidget(QWidget):
        """3D glassmorphic squircle icon badge for Suggestions cards (prism, cloud, calendar)."""

        def __init__(self, kind: str = "prism", parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self.kind = kind
            self.setFixedSize(46, 46)

        def paintEvent(self, _event: Any) -> None:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

            # Soft outer squircle container with subtle inner specular border
            bg_grad = QLinearGradient(0, 0, 46, 46)
            if self.kind == "cloud":
                bg_grad.setColorAt(0.0, QColor(56, 189, 248, 38))
                bg_grad.setColorAt(1.0, QColor(15, 23, 42, 160))
                border_col = QColor(56, 189, 248, 80)
            elif self.kind == "calendar":
                bg_grad.setColorAt(0.0, QColor(59, 130, 246, 42))
                bg_grad.setColorAt(1.0, QColor(15, 23, 42, 160))
                border_col = QColor(96, 165, 250, 85)
            else:
                bg_grad.setColorAt(0.0, QColor(250, 204, 21, 36))
                bg_grad.setColorAt(0.5, QColor(56, 189, 248, 36))
                bg_grad.setColorAt(1.0, QColor(15, 23, 42, 160))
                border_col = QColor(125, 211, 252, 85)

            p.setPen(QPen(border_col, 1.1))
            p.setBrush(QBrush(bg_grad))
            p.drawRoundedRect(QRectF(2.0, 2.0, 42.0, 42.0), 13.0, 13.0)

            if self.kind == "cloud":
                p.setPen(Qt.PenStyle.NoPen)
                grad = QRadialGradient(QPointF(21, 18), 20)
                grad.setColorAt(0.0, QColor("#ffffff"))
                grad.setColorAt(0.68, QColor("#e2e8f0"))
                grad.setColorAt(1.0, QColor("#94a3b8"))
                p.setBrush(QBrush(grad))
                p.drawEllipse(QRectF(8, 20, 16, 14))
                p.drawEllipse(QRectF(15, 13, 18, 18))
                p.drawEllipse(QRectF(24, 20, 14, 14))
                p.drawRoundedRect(QRectF(11, 24, 24, 10), 5, 5)
                p.setBrush(QColor("#09090b"))
                p.drawRoundedRect(QRectF(19.5, 19.5, 2.8, 5.8), 1.4, 1.4)
                p.drawRoundedRect(QRectF(25.0, 19.5, 2.8, 5.8), 1.4, 1.4)
            elif self.kind == "calendar":
                p.setPen(Qt.PenStyle.NoPen)
                body_g = QLinearGradient(10, 12, 10, 36)
                body_g.setColorAt(0.0, QColor("#f8fafc"))
                body_g.setColorAt(1.0, QColor("#cbd5e1"))
                p.setBrush(QBrush(body_g))
                p.drawRoundedRect(QRectF(10, 12, 26, 24), 6.0, 6.0)
                hdr_g = QLinearGradient(10, 12, 36, 20)
                hdr_g.setColorAt(0.0, QColor("#3b82f6"))
                hdr_g.setColorAt(1.0, QColor("#1d4ed8"))
                p.setBrush(QBrush(hdr_g))
                p.drawRoundedRect(QRectF(10, 12, 26, 8), 5.0, 5.0)
                p.setBrush(QColor("#64748b"))
                for row in range(2):
                    for col in range(3):
                        p.drawRoundedRect(
                            QRectF(14.5 + col * 6.5, 23.0 + row * 5.2, 4.2, 3.2), 1.2, 1.2
                        )
            else:
                grad = QLinearGradient(10, 10, 36, 36)
                grad.setColorAt(0.0, QColor("#fde047"))
                grad.setColorAt(0.48, QColor("#38bdf8"))
                grad.setColorAt(1.0, QColor("#818cf8"))
                p.setPen(QPen(QColor(255, 255, 255, 130), 1.15))
                p.setBrush(QBrush(grad))
                p.drawRoundedRect(QRectF(10, 10, 26, 26), 8.5, 8.5)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(255, 255, 255, 235))
                tri = QPolygonF([QPointF(19.0, 16.0), QPointF(19.0, 30.0), QPointF(29.5, 23.0)])
                p.drawPolygon(tri)

    class _DynamicNotchWindow(QWidget):
        """Fixed-canvas top-center Dynamic Island Notch window. Never shifts `x`; morphs symmetrically from center."""

        hotkey_triggered = Signal(str)
        reply_ready = Signal(str, str)
        composio_link_ready = Signal(str, str, str)
        shutdown_requested = Signal()

        # Target visual notch dimensions (width, height) inside the fixed 580x540 canvas
        SIZES: dict[str, tuple[int, int]] = {
            "idle": (96, 7),           # Compact micro-strip at top center
            "hover": (162, 20),        # Slightly larger hovered pill with status dot + JARVIS text
            "suggestions": (468, 516), # Image 3: Suggestions card
            "chat": (536, 336),        # Image 4: Quick Chat card
            "chat_active": (536, 468), # Quick Chat card with active conversation
            "voice": (448, 276),       # Image 5: Voice Cloud Orb card
        }
        EAR_RADIUS = 16

        def __init__(self, svc: DesktopOverlayService) -> None:
            super().__init__(None)
            self.service = svc
            self.state: str = "idle"
            self.pinned: bool = False
            self._has_messages: bool = False

            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.setMouseTracking(True)

            # Lock the OS window at a FIXED centered geometry — x NEVER moves!
            screen = QApplication.primaryScreen()
            sw = int(screen.geometry().width()) if screen else 1920
            self.setFixedSize(CANVAS_W, CANVAS_H)
            self.move((sw - CANVAS_W) // 2, 0)

            self._notch_w: float = float(self.SIZES["idle"][0])
            self._notch_h: float = float(self.SIZES["idle"][1])

            self._anim = QVariantAnimation(self)
            self._anim.setDuration(520)
            self._anim.setEasingCurve(QEasingCurve.Type.OutQuint)
            self._anim.valueChanged.connect(self._on_anim_frame)
            self._anim.finished.connect(self._on_anim_finished)

            self._fade_anim = QVariantAnimation(self)
            self._fade_anim.setDuration(360)
            self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._fade_anim.valueChanged.connect(self._on_fade_frame)

            self._build_ui()
            self._update_notch_frame(self._notch_w, self._notch_h)

            self.hotkey_triggered.connect(self._on_hotkey_toggle)
            self.reply_ready.connect(self._on_reply_ready)
            self.composio_link_ready.connect(self._on_composio_link_ready)
            self.shutdown_requested.connect(self._on_shutdown_requested)

            # Poll mouse clicks outside the notch when expanded (and unpinned) so clicking anywhere outside closes it
            self._mouse_down_prev: bool = False
            self._outside_click_timer = QTimer(self)
            self._outside_click_timer.timeout.connect(self._check_outside_mouse_click)
            self._outside_click_timer.start(45)

        def _check_outside_mouse_click(self) -> None:
            """Collapse the notch to idle whenever the user clicks anywhere outside the notch bounds (unless pinned)."""
            if os.name != "nt":
                return
            try:
                user32 = ctypes.windll.user32
                is_down = bool(
                    (user32.GetAsyncKeyState(0x01) & 0x8000)
                    or (user32.GetAsyncKeyState(0x02) & 0x8000)
                )
            except Exception:
                return

            just_clicked = is_down and not self._mouse_down_prev
            self._mouse_down_prev = is_down

            if not just_clicked:
                return
            if self.pinned or self.state not in ("suggestions", "chat", "voice"):
                return

            # Check if the global cursor position is outside the current visible notch rectangle
            pos = QCursor.pos()
            local = self.mapFromGlobal(pos)
            ear = self.EAR_RADIUS if self._notch_h > 30 else 6
            left = (CANVAS_W - self._notch_w) / 2.0 - ear - 4.0
            right = (CANVAS_W + self._notch_w) / 2.0 + ear + 4.0
            if not (left <= local.x() <= right and -4.0 <= local.y() <= self._notch_h + 6.0):
                if self.service._mic.is_recording:
                    self.service._mic.stop()
                self._orb.active_pulse = False
                self._voice_lbl.setText("Connected")
                self._animate_to("idle", duration_ms=460)

        def _on_shutdown_requested(self) -> None:
            try:
                self._outside_click_timer.stop()
                self._anim.stop()
                self._fade_anim.stop()
                self.close()
                app = QApplication.instance()
                if app is not None:
                    app.quit()
            except Exception:
                pass

        def _notch_rect(self) -> QRectF:
            """Return the current centered notch bounding box (excluding top concave ears)."""
            left = (CANVAS_W - self._notch_w) / 2.0
            return QRectF(left, 0.0, float(self._notch_w), float(self._notch_h))

        def _update_notch_frame(self, w: float, h: float) -> None:
            self._notch_w = float(w)
            self._notch_h = float(h)
            # Round mask width to an even integer so (CANVAS_W - mask_w) // 2 is always 100% symmetric
            ear = self.EAR_RADIUS if h > 30 else max(4, int(h * 0.6))
            raw_mw = int(math.ceil(self._notch_w)) + ear * 2 + 6
            if raw_mw % 2 == 1:
                raw_mw += 1
            mask_w = min(CANVAS_W, raw_mw)
            mask_h = min(CANVAS_H, int(math.ceil(self._notch_h)) + 6)
            mask_left = (CANVAS_W - mask_w) // 2
            self.setMask(QRegion(QRect(mask_left, 0, mask_w, mask_h)))

            # Keep content container centered and smoothly gliding down during transition
            slide_offset = getattr(self, "_slide_y", 0.0)
            pad_x = 18
            cur_w_even = int(round(self._notch_w / 2.0)) * 2
            left = (CANVAS_W - cur_w_even) // 2 + pad_x
            inner_w = max(10, cur_w_even - pad_x * 2)
            inner_h = max(10, int(round(self._notch_h)) - 14)
            self._content_root.setGeometry(left, int(round(8.0 + slide_offset)), inner_w, inner_h)
            self.update()

        def _on_fade_frame(self, val: Any) -> None:
            t = float(val)
            self._slide_y = (1.0 - t) * -10.0
            if hasattr(self, "_opacity_effect") and self._opacity_effect is not None:
                self._opacity_effect.setOpacity(t)
            self._update_notch_frame(self._notch_w, self._notch_h)

        def _animate_to(self, new_state: str, duration_ms: int = 540) -> None:
            prev_state = self.state
            self.state = new_state
            key = "chat_active" if (new_state == "chat" and self._has_messages) else new_state
            tw, th = self.SIZES.get(key, self.SIZES["idle"])

            is_expanding = new_state in ("suggestions", "chat", "voice")
            was_expanded = prev_state in ("suggestions", "chat", "voice")

            self._fade_anim.stop()
            if not is_expanding:
                self._content_root.hide()
            else:
                self._switch_stack_page(new_state)
                start_op = 0.25 if was_expanded else 0.0
                self._opacity_effect.setOpacity(start_op)
                self._fade_anim.setDuration(max(320, int(duration_ms * 0.85)))
                self._fade_anim.setStartValue(start_op)
                self._fade_anim.setEndValue(1.0)
                self._fade_anim.start()

            self._anim.stop()
            self._anim.setDuration(duration_ms)
            if new_state == "hover":
                curve = QEasingCurve.Type.OutBack
            elif new_state == "idle":
                curve = QEasingCurve.Type.InOutQuint
            elif was_expanded and is_expanding:
                curve = QEasingCurve.Type.InOutQuint
            else:
                curve = QEasingCurve.Type.OutQuint
            self._anim.setEasingCurve(curve)
            self._anim.setStartValue(QSizeF(float(self._notch_w), float(self._notch_h)))
            self._anim.setEndValue(QSizeF(float(tw), float(th)))
            self._anim.start()

        def _on_anim_frame(self, val: Any) -> None:
            if isinstance(val, (QSizeF, QSize)):
                self._update_notch_frame(float(val.width()), float(val.height()))

        def _on_anim_finished(self) -> None:
            if self.state in ("suggestions", "chat", "voice"):
                self._opacity_effect.setOpacity(1.0)
                self._content_root.show()
                if self.state == "chat":
                    self.activateWindow()
                    self._chat_input.setFocus()
            else:
                self._content_root.hide()

        def _switch_stack_page(self, mode: str) -> None:
            if mode == "suggestions":
                self._refresh_suggestions()
                self._stack.setCurrentIndex(0)
            elif mode == "chat":
                self._stack.setCurrentIndex(1)
            elif mode == "voice":
                self._stack.setCurrentIndex(2)
            self._content_root.show()

        # --- Hover & Click Events on the Notch ---

        def enterEvent(self, event: Any) -> None:
            if self.state == "idle":
                self._animate_to("hover", duration_ms=380)
            super().enterEvent(event)

        def leaveEvent(self, event: Any) -> None:
            if self.state == "hover":
                self._animate_to("idle", duration_ms=420)
            super().leaveEvent(event)

        def mousePressEvent(self, event: Any) -> None:
            if event.button() == Qt.MouseButton.LeftButton and self.state in ("idle", "hover"):
                # Open directly to the clean Quick Chat home view (Suggestions are stored in the ✨ Sparkles icon)
                self._animate_to("chat", duration_ms=560)
                return
            super().mousePressEvent(event)

        def keyPressEvent(self, event: Any) -> None:
            if event.key() == Qt.Key.Key_Escape and self.state not in ("idle", "hover"):
                if self.service._mic.is_recording:
                    self.service._mic.stop()
                self._animate_to("idle", duration_ms=460)
                return
            super().keyPressEvent(event)

        def _on_hotkey_toggle(self, target_mode: str = "chat") -> None:
            mode = (target_mode or "chat").strip().lower()
            if mode == "voice":
                # Ctrl+Alt+Space: Open directly into Voice Cloud Orb Mode (or collapse if already in Voice Mode)
                if self.state == "voice":
                    if self.service._mic.is_recording:
                        self.service._mic.stop()
                    self._orb.active_pulse = False
                    self._voice_lbl.setText("Connected")
                    self._animate_to("idle", duration_ms=460)
                else:
                    self._enter_voice_mode()
                return

            # Ctrl+/: Open directly into Quick Chat Mode (or collapse if already in Chat Mode)
            if self.state == "chat":
                self._animate_to("idle", duration_ms=460)
            else:
                if self.service._mic.is_recording:
                    self.service._mic.stop()
                self._orb.active_pulse = False
                self._animate_to("chat", duration_ms=520)

        # --- Center-Locked Dynamic Island Silhouette Painting ---

        def paintEvent(self, _event: Any) -> None:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

            nw = float(self._notch_w)
            nh = float(self._notch_h)
            cx = CANVAS_W / 2.0
            left = cx - nw / 2.0
            right = cx + nw / 2.0
            ear = float(self.EAR_RADIUS) if nh > 30 else max(3.5, float(nh * 0.55))
            bot_r = 28.0 if nh > 60 else max(3.5, nh * 0.60)

            path = QPainterPath()
            path.moveTo(left - ear, 0.0)
            path.quadTo(left, 0.0, left, ear)
            path.lineTo(left, max(ear, nh - bot_r))
            path.quadTo(left, nh, left + bot_r, nh)
            path.lineTo(right - bot_r, nh)
            path.quadTo(right, nh, right, max(ear, nh - bot_r))
            path.lineTo(right, ear)
            path.quadTo(right, 0.0, right + ear, 0.0)
            path.closeSubpath()

            # Pitch-black notch fill (#07080b)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#07080b"))
            p.drawPath(path)

            if nh > 60:
                p.save()
                p.setClipPath(path)
                glow = QRadialGradient(QPointF(cx, nh + 12.0), nw * 0.55)
                glow.setColorAt(0.0, QColor(14, 165, 233, 54))
                glow.setColorAt(0.55, QColor(30, 41, 59, 20))
                glow.setColorAt(1.0, QColor(0, 0, 0, 0))
                p.setBrush(QBrush(glow))
                p.drawRect(QRectF(left - ear, 0, nw + ear * 2, nh))
                p.restore()

                p.setPen(QPen(QColor(255, 255, 255, 28), 1.1))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(path)
            elif self.state == "hover" or nh > 11:
                p.setPen(QPen(QColor(255, 255, 255, 36), 1.0))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(path)
                # Glowing emerald status dot + crisp 'JARVIS' micro-label inside hovered pill
                if nh >= 15:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QColor("#34d399"))
                    p.drawEllipse(QPointF(left + 14.0, nh / 2.0), 2.8, 2.8)
                    p.setPen(QColor(226, 232, 240, 210))
                    font = QFont("Segoe UI", 8, QFont.Weight.Bold)
                    font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.8)
                    p.setFont(font)
                    p.drawText(QRectF(left, 0, nw, nh), Qt.AlignmentFlag.AlignCenter, "JARVIS")
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QColor("#38bdf8"))
                    p.drawEllipse(QPointF(right - 14.0, nh / 2.0), 2.4, 2.4)

        # --- UI Pages Construction (Suggestions, Quick Chat, Voice Orb) ---

        def _build_ui(self) -> None:
            # _content_root is manually positioned via setGeometry inside _update_notch_frame
            # so child minimum sizes NEVER force the main window to resize or shift!
            self._content_root = QWidget(self)
            self._opacity_effect = QGraphicsOpacityEffect(self._content_root)
            self._opacity_effect.setOpacity(1.0)
            self._content_root.setGraphicsEffect(self._opacity_effect)
            root_layout = QVBoxLayout(self._content_root)
            root_layout.setContentsMargins(0, 0, 0, 0)
            root_layout.setSpacing(0)

            self._stack = QStackedWidget(self._content_root)
            self._stack.addWidget(self._build_suggestions_page())
            self._stack.addWidget(self._build_chat_page())
            self._stack.addWidget(self._build_voice_page())

            root_layout.addWidget(self._stack)
            self._content_root.hide()

        def _build_suggestions_page(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(4, 2, 4, 2)
            layout.setSpacing(8)

            top = QHBoxLayout()
            top.setContentsMargins(0, 0, 0, 0)

            left_nav = QHBoxLayout()
            left_nav.setSpacing(6)
            btn_home = _VectorIconButton("home", "Back to Quick Chat Home")
            btn_home.clicked.connect(lambda: self._animate_to("chat", duration_ms=480))
            self._sugg_nav_btn_page0 = _VectorIconButton(
                "connections",
                "Composio Universal Connections Hub (1-click connect apps without .env edits)",
                badge_count=0,
            )
            self._sugg_nav_btn_page0.active_state = True
            self._sugg_nav_btn_page0.clicked.connect(lambda: self._animate_to("chat", duration_ms=480))
            left_nav.addWidget(btn_home)
            left_nav.addWidget(self._sugg_nav_btn_page0)

            center_col = QVBoxLayout()
            center_col.setSpacing(2)
            center_col.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            mascot = _WizardMascotWidget(diameter=56)
            badge = QLabel("Jarvis")
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setFixedWidth(64)
            badge.setStyleSheet(
                "background: rgba(255,255,255,0.09); color: #e2e8f0; "
                "border: 1px solid rgba(255,255,255,0.16); border-radius: 10px; "
                "padding: 2px 8px; font-size: 11px; font-weight: 700;"
            )
            center_col.addWidget(mascot, alignment=Qt.AlignmentFlag.AlignHCenter)
            center_col.addWidget(badge, alignment=Qt.AlignmentFlag.AlignHCenter)

            right_nav = QHBoxLayout()
            right_nav.setSpacing(6)
            btn_wave = _VectorIconButton("waveform", "Voice Orb Mode")
            btn_wave.clicked.connect(self._enter_voice_mode)
            self._pin_btn_sugg = _VectorIconButton("pin", "Pin Overlay Open")
            self._pin_btn_sugg.clicked.connect(self._toggle_pin)
            btn_collapse = _VectorIconButton("close", "Collapse to Top Notch (Esc)")
            btn_collapse.clicked.connect(lambda: self._animate_to("idle", duration_ms=460))
            right_nav.addWidget(btn_wave)
            right_nav.addWidget(self._pin_btn_sugg)
            right_nav.addWidget(btn_collapse)

            top.addLayout(left_nav)
            top.addStretch(1)
            top.addLayout(center_col)
            top.addStretch(1)
            top.addLayout(right_nav)
            layout.addLayout(top)

            self._sugg_heading = QLabel("Connections")
            self._sugg_heading.setStyleSheet(
                "color: #ffffff; font-size: 19px; font-weight: 800; margin-top: -4px;"
            )
            layout.addWidget(self._sugg_heading)

            # Dedicated 'Composio wants to connect <App Name>' banner card (shown when user clicks Connect)
            self._composio_link_banner = QFrame()
            self._composio_link_banner.setStyleSheet(
                "QFrame { background: rgba(14, 165, 233, 0.14); "
                "border: 1px solid rgba(56, 189, 248, 0.55); border-radius: 14px; }"
            )
            bl = QVBoxLayout(self._composio_link_banner)
            bl.setContentsMargins(12, 9, 12, 9)
            bl.setSpacing(4)
            self._composio_banner_title = QLabel("🔗 Composio wants to connect App")
            self._composio_banner_title.setStyleSheet(
                "color: #ffffff; font-size: 13px; font-weight: 800; border: none; background: transparent;"
            )
            self._composio_banner_link = QLabel("")
            self._composio_banner_link.setOpenExternalLinks(True)
            self._composio_banner_link.setWordWrap(True)
            self._composio_banner_link.setStyleSheet(
                "color: #38bdf8; font-size: 12px; font-weight: 700; border: none; background: transparent;"
            )
            bl.addWidget(self._composio_banner_title)
            bl.addWidget(self._composio_banner_link)
            self._composio_link_banner.hide()
            layout.addWidget(self._composio_link_banner)

            # Scrollable list of all connectable apps
            self._sugg_scroll = QScrollArea()
            self._sugg_scroll.setWidgetResizable(True)
            self._sugg_scroll.setFrameShape(QFrame.Shape.NoFrame)
            self._sugg_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self._sugg_scroll.setStyleSheet(
                "QScrollArea { background: transparent; border: none; }"
                "QScrollBar:vertical { background: rgba(255,255,255,0.04); width: 6px; border-radius: 3px; }"
                "QScrollBar::handle:vertical { background: rgba(255,255,255,0.22); min-height: 24px; border-radius: 3px; }"
                "QScrollBar::handle:vertical:hover { background: #38bdf8; }"
                "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }"
            )
            self._sugg_container = QWidget()
            self._sugg_container.setStyleSheet("background: transparent;")
            self._sugg_list_layout = QVBoxLayout(self._sugg_container)
            self._sugg_list_layout.setContentsMargins(0, 0, 4, 0)
            self._sugg_list_layout.setSpacing(6)
            self._sugg_scroll.setWidget(self._sugg_container)
            layout.addWidget(self._sugg_scroll, stretch=1)

            self._sugg_footer_btn = QPushButton("Back to Quick Chat")
            self._sugg_footer_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            self._sugg_footer_btn.setStyleSheet(
                "QPushButton { background: transparent; color: #94a3b8; border: none; "
                "font-size: 12px; font-weight: 600; padding: 3px; }"
                "QPushButton:hover { color: #38bdf8; }"
            )
            self._sugg_footer_btn.clicked.connect(lambda: self._animate_to("chat", duration_ms=360))
            layout.addWidget(self._sugg_footer_btn, alignment=Qt.AlignmentFlag.AlignHCenter)
            return page

        def _refresh_suggestions(self) -> None:
            """Populate the scrollable list of connectable Composio apps."""
            while self._sugg_list_layout.count():
                item = self._sugg_list_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()

            self._sugg_heading.setText("Connections")
            self._sugg_footer_btn.setText("Back to Quick Chat")
            self._populate_composio_connections_hub()

        def _populate_composio_connections_hub(self) -> None:
            """Render the scrollable list of all apps that can be connected via Composio."""
            hub_cards = self.service.fetch_composio_hub_cards()
            for app in hub_cards:
                row = QFrame()
                row.setFixedHeight(58)
                is_conn = bool(app.get("connected"))
                row.setStyleSheet(
                    "QFrame { background: rgba(255, 255, 255, 0.038); "
                    "border: 1px solid rgba(255, 255, 255, 0.085); border-radius: 14px; }"
                    "QFrame:hover { background: rgba(56, 189, 248, 0.075); "
                    "border: 1px solid rgba(56, 189, 248, 0.35); }"
                )
                rl = QHBoxLayout(row)
                rl.setContentsMargins(10, 6, 10, 6)
                rl.setSpacing(10)

                icon_w = _SuggestionIconWidget(kind=app.get("icon", "prism"))
                rl.addWidget(icon_w, alignment=Qt.AlignmentFlag.AlignVCenter)

                text_col = QVBoxLayout()
                text_col.setSpacing(2)
                title_txt = f"{app['name']}  ·  ✅ Connected" if is_conn else app["name"]
                t_lbl = QLabel(title_txt)
                t_lbl.setStyleSheet(
                    f"color: {'#6ee7b7' if is_conn else '#ffffff'}; font-size: 13px; font-weight: 700; border: none; background: transparent;"
                )
                b_lbl = QLabel(app["desc"])
                b_lbl.setStyleSheet(
                    "color: #94a3b8; font-size: 11px; border: none; background: transparent;"
                )
                text_col.addWidget(t_lbl)
                text_col.addWidget(b_lbl)
                rl.addLayout(text_col, stretch=1)

                tk_slug = app["toolkit"]
                app_name = app["name"]
                pill = QPushButton("Connect")
                pill.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
                pill.setFixedHeight(28)
                pill.setStyleSheet(
                    "QPushButton { background: #38bdf8; color: #020617; border: none; "
                    "border-radius: 14px; padding: 0 15px; font-size: 11.5px; font-weight: 800; }"
                    "QPushButton:hover { background: #7dd3fc; }"
                )
                pill.clicked.connect(
                    lambda _checked=False, slug=tk_slug, name=app_name: self._on_composio_app_clicked(slug, name)
                )
                rl.addWidget(pill, alignment=Qt.AlignmentFlag.AlignVCenter)
                self._sugg_list_layout.addWidget(row)
            self._sugg_list_layout.addStretch(1)

        def _on_composio_app_clicked(self, toolkit_slug: str, app_name: str = "") -> None:
            """Generate a 'Composio wants to connect (App Name)' link card right inside the Connections tab."""
            display_name = app_name or toolkit_slug.title()
            self._composio_banner_title.setText(f"⏳ Composio wants to connect {display_name}...")
            self._composio_banner_link.setText(
                "<span style='color:#cbd5e1;'>Generating one-click Composio authorization link...</span>"
            )
            self._composio_link_banner.show()

            def _oauth_worker() -> None:
                try:
                    from tools.integrations import authorize_toolkit_sync

                    info = authorize_toolkit_sync(toolkit_slug)
                    url = info.get("redirect_url") or ""
                    self.composio_link_ready.emit(display_name, url, "")
                except Exception as exc:
                    self.composio_link_ready.emit(display_name, "", str(exc))

            threading.Thread(target=_oauth_worker, daemon=True).start()

        def _on_composio_link_ready(self, app_name: str, url: str, err: str) -> None:
            if err or not url:
                self._composio_banner_title.setText(f"⚠️ Could not create link for {app_name}")
                self._composio_banner_link.setText(
                    f"<span style='color:#fca5a5;'>{html.escape(err or 'No redirect URL returned')}</span>"
                )
                self._composio_link_banner.show()
                return

            esc_url = html.escape(url)
            esc_name = html.escape(app_name)
            self._composio_banner_title.setText(f"🔗 Composio wants to connect {app_name}")
            self._composio_banner_link.setText(
                f"<a href='{esc_url}' style='color:#38bdf8;font-weight:800;text-decoration:underline;'>"
                f"Click here: Composio wants to connect {esc_name} ↗</a>"
            )
            self._composio_link_banner.show()

        def _run_suggestion(self, suggestion_id: str, prompt: str, use_see: bool) -> None:
            # Remove this suggestion from the icon badge & list immediately upon execution
            self.service.mark_suggestion_done(suggestion_id)
            remaining = self.service.fetch_live_suggestions()
            self._sync_sugg_nav_buttons(len(remaining))
            if use_see:
                self.service.auto_see_screen = True
                self._sync_plus_btn_style()
            self._animate_to("chat", duration_ms=320)
            self._submit_prompt(prompt, speak_reply=False)

        def _build_chat_page(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(6, 2, 6, 4)
            layout.setSpacing(10)

            top = QHBoxLayout()
            top.setSpacing(6)
            btn_home = _VectorIconButton("home", "Quick Chat Home")
            btn_home.active_state = True
            btn_home.clicked.connect(self._reset_to_chat_hero)
            self._sugg_nav_btn_chat = _VectorIconButton(
                "connections",
                "Composio Universal Connections Hub (1-click connect apps without .env edits)",
                badge_count=0,
            )
            self._sugg_nav_btn_chat.clicked.connect(lambda: self._animate_to("suggestions", duration_ms=480))
            btn_wave = _VectorIconButton("waveform", "Voice Orb Mode")
            btn_wave.clicked.connect(self._enter_voice_mode)
            self._pin_btn_chat = _VectorIconButton("pin", "Pin Overlay Open")
            self._pin_btn_chat.clicked.connect(self._toggle_pin)
            btn_collapse = _VectorIconButton("close", "Collapse to Top Notch (Esc)")
            btn_collapse.clicked.connect(lambda: self._animate_to("idle", duration_ms=460))
            top.addWidget(btn_home)
            top.addWidget(self._sugg_nav_btn_chat)
            top.addStretch(1)
            top.addWidget(btn_wave)
            top.addWidget(self._pin_btn_chat)
            top.addWidget(btn_collapse)
            layout.addLayout(top)

            self._hero_widget = QWidget()
            hero_l = QVBoxLayout(self._hero_widget)
            hero_l.setContentsMargins(0, 2, 0, 6)
            hero_l.setSpacing(10)
            hero_l.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hero_mascot = _WizardMascotWidget(diameter=84)
            hero_title = QLabel("Ready for a quick chat?")
            hero_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hero_title.setStyleSheet("color: #ffffff; font-size: 23px; font-weight: 800;")
            hero_l.addWidget(hero_mascot, alignment=Qt.AlignmentFlag.AlignHCenter)
            hero_l.addWidget(hero_title, alignment=Qt.AlignmentFlag.AlignHCenter)
            layout.addWidget(self._hero_widget, stretch=1)

            self._chat_output = QTextBrowser()
            self._chat_output.setOpenExternalLinks(True)
            self._chat_output.setStyleSheet(
                "QTextBrowser { background: rgba(15, 23, 42, 0.52); color: #f1f5f9; "
                "border: 1px solid rgba(255,255,255,0.11); border-radius: 16px; "
                "padding: 10px 12px; font-size: 13px; font-family: 'Segoe UI', sans-serif; }"
            )
            self._chat_output.hide()
            layout.addWidget(self._chat_output, stretch=1)

            self._approve_btn = QPushButton("✅ Approve Pending Action")
            self._approve_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            self._approve_btn.setFixedHeight(30)
            self._approve_btn.setStyleSheet(
                "QPushButton { background: #10b981; color: #052e16; border: none; "
                "border-radius: 15px; font-size: 12px; font-weight: 800; padding: 0 16px; }"
                "QPushButton:hover { background: #34d399; }"
            )
            self._approve_btn.clicked.connect(self._on_approve_clicked)
            self._approve_btn.hide()
            layout.addWidget(self._approve_btn, alignment=Qt.AlignmentFlag.AlignRight)

            # Stadium Input Pill (Image 4 exact layout)
            pill_bar = QFrame()
            pill_bar.setFixedHeight(52)
            pill_bar.setStyleSheet(
                "QFrame { background: #101217; border: 1px solid rgba(255,255,255,0.28); "
                "border-radius: 26px; }"
            )
            pl = QHBoxLayout(pill_bar)
            pl.setContentsMargins(8, 6, 8, 6)
            pl.setSpacing(10)

            self._plus_btn = QPushButton("+")
            self._plus_btn.setFixedSize(36, 36)
            self._plus_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            self._plus_btn.setToolTip("Toggle Auto-See Screen (attach live monitor screenshot)")
            self._plus_btn.clicked.connect(self._toggle_auto_see)
            self._sync_plus_btn_style()
            pl.addWidget(self._plus_btn)

            self._chat_input = QLineEdit()
            self._chat_input.setPlaceholderText("Ask Jarvis anything")
            self._chat_input.setStyleSheet(
                "QLineEdit { background: transparent; color: #ffffff; border: none; "
                "font-size: 14px; }"
            )
            self._chat_input.returnPressed.connect(self._on_enter_pressed)
            pl.addWidget(self._chat_input, stretch=1)

            mic_btn = _VectorIconButton("mic", "Switch to Live Voice Cloud Orb", size=36, pill_white=True)
            mic_btn.clicked.connect(self._enter_voice_mode)
            pl.addWidget(mic_btn)

            layout.addWidget(pill_bar)
            return page

        def _build_voice_page(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(6, 4, 6, 10)
            layout.setSpacing(6)

            top = QHBoxLayout()
            top.addStretch(1)
            close_btn = _VectorIconButton("close_circle", "Close Voice Mode", size=32)
            close_btn.clicked.connect(self._exit_voice_mode)
            top.addWidget(close_btn)
            layout.addLayout(top)

            self._orb = _CloudOrbWidget(diameter=152)
            self._orb.clicked.connect(self._toggle_voice_recording)
            layout.addWidget(self._orb, alignment=Qt.AlignmentFlag.AlignHCenter)

            self._voice_lbl = QLabel("Connected")
            self._voice_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._voice_lbl.setStyleSheet(
                "color: #ffffff; font-size: 16px; font-weight: 800; margin-top: 4px;"
            )
            layout.addWidget(self._voice_lbl, alignment=Qt.AlignmentFlag.AlignHCenter)
            return page

        # --- Actions & Backend Wiring ---

        def _reset_to_chat_hero(self) -> None:
            self._has_messages = False
            self._chat_output.hide()
            self._approve_btn.hide()
            self._hero_widget.show()
            self._animate_to("chat", duration_ms=420)

        def _toggle_pin(self) -> None:
            self.pinned = not self.pinned
            self._pin_btn_sugg.active_state = self.pinned
            self._pin_btn_chat.active_state = self.pinned
            self._pin_btn_sugg.update()
            self._pin_btn_chat.update()

        def _toggle_auto_see(self) -> None:
            self.service.auto_see_screen = not self.service.auto_see_screen
            self._sync_plus_btn_style()

        def _sync_plus_btn_style(self) -> None:
            if self.service.auto_see_screen:
                self._plus_btn.setText("👁")
                self._plus_btn.setStyleSheet(
                    "QPushButton { background: #10b981; color: #052e16; border: none; "
                    "border-radius: 18px; font-size: 15px; font-weight: 800; }"
                )
            else:
                self._plus_btn.setText("+")
                self._plus_btn.setStyleSheet(
                    "QPushButton { background: #262932; color: #e2e8f0; border: none; "
                    "border-radius: 18px; font-size: 20px; font-weight: 400; }"
                    "QPushButton:hover { background: #333742; }"
                )

        def _on_enter_pressed(self) -> None:
            if self.service._busy:
                return
            q = self._chat_input.text().strip()
            if not q:
                return
            self._chat_input.clear()
            self._submit_prompt(q, speak_reply=False)

        def _submit_prompt(self, query: str, *, speak_reply: bool = False) -> None:
            self.service._busy = True
            if not self._has_messages:
                self._has_messages = True
                self._hero_widget.hide()
                self._chat_output.show()
                self._animate_to("chat", duration_ms=220)

            see_now = self.service.auto_see_screen
            self._chat_output.setHtml(_format_reply_html(query, "", thinking=True))
            self._approve_btn.hide()

            def _worker() -> None:
                try:
                    loop = self.service._get_async_loop()
                    fut = asyncio.run_coroutine_threadsafe(
                        self.service.submit_query_async(
                            query, auto_see=see_now, speak_reply=speak_reply
                        ),
                        loop,
                    )
                    reply = fut.result(timeout=75.0)
                    self.reply_ready.emit(query, reply)
                except Exception as exc:
                    self.reply_ready.emit(query, f"Error: {exc}")
                finally:
                    self.service._busy = False

            threading.Thread(target=_worker, daemon=True).start()

        def _on_reply_ready(self, query: str, reply: str) -> None:
            if self.state != "chat":
                self._orb.active_pulse = False
                self._voice_lbl.setText("Connected")
                self._animate_to("chat", duration_ms=240)
            self._hero_widget.hide()
            self._chat_output.show()
            self._chat_output.setHtml(_format_reply_html(query, reply, thinking=False))
            self._chat_input.setFocus()
            if self.service._pending_token:
                self._approve_btn.show()
            else:
                self._approve_btn.hide()

        def _on_approve_clicked(self) -> None:
            if self.service._busy or not self.service._pending_token:
                return
            self.service._busy = True
            self._approve_btn.hide()
            self._chat_output.setHtml(_format_reply_html("Approve action", "", thinking=True))

            def _worker() -> None:
                try:
                    loop = self.service._get_async_loop()
                    res = asyncio.run_coroutine_threadsafe(
                        self.service.approve_pending_async(), loop
                    ).result(timeout=45.0)
                    self.reply_ready.emit("Approve action", f"✅ Confirmed:\n{res}")
                except Exception as exc:
                    self.reply_ready.emit("Approve action", f"Confirmation error: {exc}")
                finally:
                    self.service._busy = False

            threading.Thread(target=_worker, daemon=True).start()

        def _enter_voice_mode(self) -> None:
            self._animate_to("voice", duration_ms=480)
            self._voice_lbl.setText("Connected")
            self._orb.active_pulse = False
            QTimer.singleShot(480, self._toggle_voice_recording)

        def _exit_voice_mode(self) -> None:
            if self.service._mic.is_recording:
                self.service._mic.stop()
            self._orb.active_pulse = False
            self._voice_lbl.setText("Connected")
            self._animate_to("chat", duration_ms=460)

        def _toggle_voice_recording(self) -> None:
            if self.service._busy:
                return
            if not self.service._mic.is_recording:
                if self.service._mic.start():
                    self._orb.active_pulse = True
                    self._voice_lbl.setText("Listening... (tap orb to send)")
                else:
                    self._voice_lbl.setText("Connected (mic unavailable)")
                return

            self._orb.active_pulse = True
            self._voice_lbl.setText("Transcribing...")
            wav_bytes = self.service._mic.stop()
            if not wav_bytes:
                self._orb.active_pulse = False
                self._voice_lbl.setText("Connected")
                return

            self.service._busy = True

            def _stt_worker() -> None:
                try:
                    from services.voice import transcribe_audio

                    loop = self.service._get_async_loop()
                    heard = asyncio.run_coroutine_threadsafe(
                        transcribe_audio(
                            wav_bytes, filename="notch_mic.wav", mime_type="audio/wav"
                        ),
                        loop,
                    ).result(timeout=30.0)
                    heard = (heard or "").strip()
                    self.service._busy = False
                    if not heard:
                        self.reply_ready.emit("(Voice)", "I didn't catch anything in that recording.")
                        return
                    self._submit_prompt(heard, speak_reply=True)
                except Exception as exc:
                    self.service._busy = False
                    self.reply_ready.emit("(Voice)", f"Voice transcription failed: {exc}")

            threading.Thread(target=_stt_worker, daemon=True).start()

    return _DynamicNotchWindow(service)


def start_desktop_overlay(
    *,
    owner_id: int | None = None,
    bot: Any = None,
    job_queue: Any = None,
    async_loop: asyncio.AbstractEventLoop | None = None,
) -> DesktopOverlayService:
    """Launch the global hotkey Dynamic Island Top-Notch Overlay in a non-blocking daemon thread."""
    global _OVERLAY_INSTANCE
    if _OVERLAY_INSTANCE is None:
        _OVERLAY_INSTANCE = DesktopOverlayService(
            owner_id=owner_id,
            bot=bot,
            job_queue=job_queue,
            async_loop=async_loop,
        )
    _OVERLAY_INSTANCE.start()
    return _OVERLAY_INSTANCE


def stop_desktop_overlay() -> None:
    """Cleanly stop the Desktop Companion Overlay if running."""
    global _OVERLAY_INSTANCE
    if _OVERLAY_INSTANCE is not None:
        _OVERLAY_INSTANCE.stop()
        _OVERLAY_INSTANCE = None
