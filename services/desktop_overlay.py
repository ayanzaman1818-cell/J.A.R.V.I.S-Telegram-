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


# --- Native Windows MCI Microphone Recorder, Audio Player & WASAPI Live Audio Peak Meter ---


class _WindowsAudioPeakMeter:
    """Zero-dependency Win32 CoreAudio reader for both IAudioMeterInformation (visualizer) and IAudioEndpointVolume (exact system volume)."""

    def __init__(self) -> None:
        self._meter_ptr: Any = None
        self._get_peak_fn: Any = None
        self._vol_ptr: Any = None
        self._set_vol_fn: Any = None
        self._get_vol_fn: Any = None
        self._set_mute_fn: Any = None
        self._get_mute_fn: Any = None
        self._initialized = False

    def _ensure_init(self) -> None:
        if self._initialized or os.name != "nt":
            return
        self._initialized = True
        try:
            import uuid
            from ctypes import POINTER, Structure, byref, c_float, c_int, c_uint, c_void_p, HRESULT
            from ctypes.wintypes import DWORD

            class _GUID(Structure):
                _fields_ = [
                    ("Data1", DWORD),
                    ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort),
                    ("Data4", ctypes.c_ubyte * 8),
                ]

                def __init__(self, s: str) -> None:
                    u = uuid.UUID(s)
                    self.Data1 = u.time_low
                    self.Data2 = u.time_mid
                    self.Data3 = u.time_hi_version
                    for i, b in enumerate(u.bytes[8:]):
                        self.Data4[i] = b

            clsid_enum = _GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
            iid_enum = _GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
            iid_meter = _GUID("{C02216F6-8C67-4B5B-9D00-D008E73E0064}")
            iid_vol = _GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")

            ole32 = ctypes.windll.ole32
            ole32.CoInitialize(None)
            enum_ptr = c_void_p()
            if ole32.CoCreateInstance(byref(clsid_enum), None, 23, byref(iid_enum), byref(enum_ptr)) != 0:
                return
            vt = ctypes.cast(ctypes.cast(enum_ptr, POINTER(c_void_p))[0], POINTER(c_void_p))
            get_ep = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_uint, c_uint, POINTER(c_void_p))(vt[4])
            dev_ptr = c_void_p()
            if get_ep(enum_ptr, 0, 1, byref(dev_ptr)) != 0:
                return
            dev_vt = ctypes.cast(ctypes.cast(dev_ptr, POINTER(c_void_p))[0], POINTER(c_void_p))
            activate = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(_GUID), DWORD, c_void_p, POINTER(c_void_p))(dev_vt[3])

            meter_ptr = c_void_p()
            if activate(dev_ptr, byref(iid_meter), 23, None, byref(meter_ptr)) == 0:
                meter_vt = ctypes.cast(ctypes.cast(meter_ptr, POINTER(c_void_p))[0], POINTER(c_void_p))
                self._meter_ptr = meter_ptr
                self._get_peak_fn = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_float))(meter_vt[3])

            vol_ptr = c_void_p()
            if activate(dev_ptr, byref(iid_vol), 23, None, byref(vol_ptr)) == 0:
                vol_vt = ctypes.cast(ctypes.cast(vol_ptr, POINTER(c_void_p))[0], POINTER(c_void_p))
                self._vol_ptr = vol_ptr
                # IAudioEndpointVolume vtable: 7=SetMasterVolumeLevelScalar, 9=GetMasterVolumeLevelScalar, 14=SetMute, 15=GetMute
                self._set_vol_fn = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_float, c_void_p)(vol_vt[7])
                self._get_vol_fn = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_float))(vol_vt[9])
                self._set_mute_fn = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_int, c_void_p)(vol_vt[14])
                self._get_mute_fn = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_int))(vol_vt[15])
        except Exception:
            self._meter_ptr = None
            self._get_peak_fn = None
            self._vol_ptr = None

    def get_peak(self) -> float:
        """Return the instantaneous master audio output peak amplitude in [0.0, 1.0]."""
        self._ensure_init()
        if not self._get_peak_fn or not self._meter_ptr:
            return 0.0
        try:
            peak = ctypes.c_float(0.0)
            if self._get_peak_fn(self._meter_ptr, ctypes.byref(peak)) == 0:
                return max(0.0, min(1.0, float(peak.value)))
        except Exception:
            pass
        return 0.0

    def get_master_volume(self) -> tuple[float, bool]:
        """Return (scalar_level_0_to_1, is_muted) directly from Windows CoreAudio IAudioEndpointVolume."""
        self._ensure_init()
        if not self._vol_ptr or not self._get_vol_fn:
            return 0.5, False
        try:
            lvl = ctypes.c_float(0.5)
            muted = ctypes.c_int(0)
            self._get_vol_fn(self._vol_ptr, ctypes.byref(lvl))
            if self._get_mute_fn:
                self._get_mute_fn(self._vol_ptr, ctypes.byref(muted))
            return max(0.0, min(1.0, float(lvl.value))), bool(muted.value)
        except Exception:
            return 0.5, False

    def set_master_volume(self, level: float) -> None:
        """Set exact Windows master volume scalar (0.0 .. 1.0) immediately via IAudioEndpointVolume."""
        self._ensure_init()
        if not self._vol_ptr or not self._set_vol_fn:
            return
        try:
            clamped = max(0.0, min(1.0, float(level)))
            self._set_vol_fn(self._vol_ptr, ctypes.c_float(clamped), None)
            if self._set_mute_fn and clamped > 0.005:
                self._set_mute_fn(self._vol_ptr, ctypes.c_int(0), None)
        except Exception:
            pass

    def set_mute(self, muted: bool) -> None:
        """Set exact Windows master mute state via IAudioEndpointVolume."""
        self._ensure_init()
        if not self._vol_ptr or not self._set_mute_fn:
            return
        try:
            self._set_mute_fn(self._vol_ptr, ctypes.c_int(1 if muted else 0), None)
        except Exception:
            pass


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

        # Zero-overhead Win32 CPU delta counters & cached calendar events for the PC Home Dashboard
        self._cpu_prev_idle: int = 0
        self._cpu_prev_kernel: int = 0
        self._cpu_prev_user: int = 0
        self._last_cpu_pct: float = 12.0
        self._cal_events_cache: list[str] = []
        self._cal_events_ts: float = 0.0
        self._cal_fetching: bool = False

        # Live Now Playing album art, blurry background palette & WASAPI audio meter for Idle Notch Visualizer
        self._audio_meter = _WindowsAudioPeakMeter()
        self._now_playing_state: dict[str, Any] = {
            "playing": False,
            "track": "No song playing right now",
            "source": "Spotify / Media Idle",
        }
        self._now_playing_ts: float = 0.0
        self._album_art_cache: dict[str, dict[str, Any]] = {}
        self._album_fetching_track: str = ""
        self._current_art_bytes: bytes = b""
        self._current_art_track: str = ""
        self._current_art_palette: list[tuple[int, int, int]] = [
            (56, 189, 248),
            (168, 85, 247),
            (52, 211, 153),
        ]

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

    def _sample_cpu_ram_fast(self) -> tuple[float, float]:
        """Instant <0.1ms Win32 kernel32 GetSystemTimes & GlobalMemoryStatusEx check (no subprocess)."""
        cpu_pct = self._last_cpu_pct
        ram_pct = 45.0
        if os.name == "nt":
            try:
                class _FILETIME(ctypes.Structure):
                    _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]

                idle_ft = _FILETIME()
                kernel_ft = _FILETIME()
                user_ft = _FILETIME()
                if ctypes.windll.kernel32.GetSystemTimes(
                    ctypes.byref(idle_ft), ctypes.byref(kernel_ft), ctypes.byref(user_ft)
                ):
                    idle = (idle_ft.dwHighDateTime << 32) | idle_ft.dwLowDateTime
                    kernel = (kernel_ft.dwHighDateTime << 32) | kernel_ft.dwLowDateTime
                    user = (user_ft.dwHighDateTime << 32) | user_ft.dwLowDateTime
                    if self._cpu_prev_kernel > 0:
                        d_idle = idle - self._cpu_prev_idle
                        d_total = (kernel - self._cpu_prev_kernel) + (user - self._cpu_prev_user)
                        if d_total > 0:
                            cpu_pct = max(0.0, min(100.0, (1.0 - (d_idle / float(d_total))) * 100.0))
                            self._last_cpu_pct = cpu_pct
                    self._cpu_prev_idle = idle
                    self._cpu_prev_kernel = kernel
                    self._cpu_prev_user = user

                from tools.pc_control import _MEMORYSTATUSEX
                mem = _MEMORYSTATUSEX()
                mem.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
                    ram_pct = float(mem.dwMemoryLoad)
            except Exception:
                pass
        return cpu_pct, ram_pct

    def _detect_now_playing_fast(self) -> dict[str, Any]:
        """Fast Win32 EnumWindows check for currently playing Spotify / media window title (no PowerShell spawn)."""
        if os.name != "nt":
            return {"playing": False, "track": "No song playing right now", "source": "Media Idle"}

        spotify_titles: list[str] = []
        browser_media_titles: list[str] = []
        user32 = ctypes.windll.user32
        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        cls_buf = ctypes.create_unicode_buffer(128)

        def _cb(hwnd: int, _lparam: int) -> bool:
            # Do NOT require IsWindowVisible(hwnd) for Spotify: when Spotify is closed/minimized to the
            # Windows system tray, its top-level Chrome_WidgetWin_0 window becomes hidden (IsWindowVisible=False)
            # while keeping the active "Artist - Song Title" window text!
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            user32.GetClassNameW(hwnd, cls_buf, 128)
            cname = cls_buf.value
            if not cname.startswith("Chrome_WidgetWin"):
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.strip()
            if not title:
                return True
            t_low = title.lower()
            if t_low in ("spotify", "spotify free", "spotify premium"):
                spotify_titles.append("PAUSED")
            elif " - " in title:
                # Exclude browsers, VS Code, Discord, etc.
                if not any(
                    b in t_low
                    for b in (
                        "google chrome",
                        "microsoft​ edge",
                        "microsoft edge",
                        "mozilla firefox",
                        "visual studio code",
                        "antigravity",
                        "discord",
                        "lm studio",
                        "telegram",
                    )
                ):
                    spotify_titles.insert(0, title)
                elif user32.IsWindowVisible(hwnd) and (
                    "youtube music" in t_low or " - youtube" in t_low or "soundcloud" in t_low
                ):
                    cleaned = (
                        title.replace(" - YouTube Music", "")
                        .replace(" - YouTube", "")
                        .replace(" - Google Chrome", "")
                        .replace(" - Microsoft Edge", "")
                        .strip()
                    )
                    browser_media_titles.append(cleaned)
            return True

        try:
            user32.EnumWindows(EnumWindowsProc(_cb), 0)
        except Exception:
            pass

        for st in spotify_titles:
            if st != "PAUSED":
                res = {"playing": True, "track": st, "source": "Spotify · Playing Now"}
                self._now_playing_state = res
                self._now_playing_ts = time.time()
                self._ensure_album_art_async(st)
                return res
        if browser_media_titles:
            res = {"playing": True, "track": browser_media_titles[0], "source": "Web Media · Playing Now"}
            self._now_playing_state = res
            self._now_playing_ts = time.time()
            self._ensure_album_art_async(browser_media_titles[0])
            return res
        if "PAUSED" in spotify_titles:
            res = {"playing": False, "track": "Spotify is open (Paused)", "source": "Spotify · Paused"}
            self._now_playing_state = res
            self._now_playing_ts = time.time()
            return res
        res = {"playing": False, "track": "No song playing right now", "source": "Spotify / Media Idle"}
        self._now_playing_state = res
        self._now_playing_ts = time.time()
        return res

    def get_cached_now_playing(self) -> dict[str, Any]:
        """Return cached Now Playing info, refreshing every 1.2s so 33fps idle visualizer stays zero-overhead."""
        if time.time() - self._now_playing_ts > 1.2:
            return self._detect_now_playing_fast()
        return self._now_playing_state

    def _ensure_album_art_async(self, track_title: str) -> None:
        """Fetch album cover artwork and extract blurry vibrant palette colors in a background thread."""
        clean = (track_title or "").strip()
        if not clean or clean in ("No song playing right now", "Spotify is open (Paused)"):
            return
        if clean in self._album_art_cache:
            cached = self._album_art_cache[clean]
            self._current_art_bytes = cached.get("bytes", b"")
            self._current_art_palette = cached.get("palette", self._current_art_palette)
            self._current_art_track = clean
            return
        if self._album_fetching_track == clean:
            return
        self._album_fetching_track = clean
        threading.Thread(
            target=self._fetch_album_art_worker, args=(clean,), daemon=True
        ).start()

    def _fetch_album_art_worker(self, track_title: str) -> None:
        """Query iTunes Search API for the playing track's album cover & extract blurred dominant RGB colors."""
        import io
        from urllib.parse import quote
        import httpx

        art_bytes = b""
        palette: list[tuple[int, int, int]] = [
            (56, 189, 248),
            (168, 85, 247),
            (52, 211, 153),
        ]
        try:
            q = re.sub(r"\([^)]*\)|\[[^\]]*\]", "", track_title).replace(" - ", " ").strip()
            with httpx.Client(timeout=5.0) as client:
                # Primary: Deezer Search API (instant, high-res 250x250 cover art)
                r_dz = client.get(f"https://api.deezer.com/search?q={quote(q)}&limit=1")
                if r_dz.status_code == 200:
                    dz_data = (r_dz.json() or {}).get("data") or []
                    if dz_data:
                        album_obj = dz_data[0].get("album") or {}
                        art_url = str(album_obj.get("cover_medium") or album_obj.get("cover") or "")
                        if art_url:
                            art_res = client.get(art_url)
                            if art_res.status_code == 200:
                                art_bytes = art_res.content

                # Fallback: iTunes Search API
                if not art_bytes:
                    r_it = client.get(f"https://itunes.apple.com/search?term={quote(q)}&media=music&limit=1")
                    if r_it.status_code == 200:
                        results = (r_it.json() or {}).get("results") or []
                        if results:
                            art_url = str(results[0].get("artworkUrl100") or "")
                            if art_url:
                                art_res = client.get(art_url)
                                if art_res.status_code == 200:
                                    art_bytes = art_res.content
        except Exception:
            pass

        if art_bytes:
            try:
                from PIL import Image, ImageEnhance, ImageFilter

                img = Image.open(io.BytesIO(art_bytes)).convert("RGB")
                # Create a heavily blurred version of the album cover background and boost its saturation
                blurred = img.resize((24, 24)).filter(ImageFilter.GaussianBlur(radius=4))
                blurred = ImageEnhance.Color(blurred).enhance(1.65)
                blurred = ImageEnhance.Brightness(blurred).enhance(1.20)
                w, h = blurred.size
                sample_pts = [
                    (w // 4, h // 3),
                    (w // 2, h // 2),
                    (3 * w // 4, 2 * h // 3),
                    (w // 3, 2 * h // 3),
                ]
                extracted: list[tuple[int, int, int]] = []
                for sx, sy in sample_pts:
                    r_c, g_c, b_c = blurred.getpixel((sx, sy))
                    # Ensure enough brightness so visualizer bars glow against the dark notch
                    if max(r_c, g_c, b_c) < 110:
                        r_c = min(255, int(r_c * 1.6 + 55))
                        g_c = min(255, int(g_c * 1.6 + 55))
                        b_c = min(255, int(b_c * 1.6 + 55))
                    extracted.append((r_c, g_c, b_c))
                if len(extracted) >= 3:
                    palette = extracted
            except Exception:
                pass

        self._album_art_cache[track_title] = {
            "bytes": art_bytes,
            "palette": palette,
        }
        self._current_art_bytes = art_bytes
        self._current_art_palette = palette
        self._current_art_track = track_title
        if self._album_fetching_track == track_title:
            self._album_fetching_track = ""

    def _refresh_calendar_events_bg(self) -> None:
        """Background thread to refresh today's Google Calendar events via Composio every 3 minutes."""
        if self._cal_fetching:
            return
        self._cal_fetching = True
        try:
            from tools import integrations as integ
            if not integ.composio_enabled():
                return
            loop = self._get_async_loop()
            ctx = self._make_context()
            fut = asyncio.run_coroutine_threadsafe(integ.calendar_today(ctx), loop)
            raw = fut.result(timeout=12.0)
            parsed_events: list[str] = []
            # Parse summary lines or JSON from Composio calendar output
            for m in re.finditer(r'"summary"\s*:\s*"([^"]+)"', raw or ""):
                s = m.group(1).strip()
                if s and s not in parsed_events:
                    parsed_events.append(f"📅 Today: {s}")
            self._cal_events_cache = parsed_events[:3]
            self._cal_events_ts = time.time()
        except Exception:
            self._cal_events_ts = time.time()
        finally:
            self._cal_fetching = False

    def fetch_dashboard_snapshot(self) -> dict[str, Any]:
        """Collect live PC Dashboard data (Now Playing song, CPU/RAM in words with icons, and Important Events)."""
        media = self._detect_now_playing_fast()
        cpu_pct, ram_pct = self._sample_cpu_ram_fast()

        if cpu_pct < 35.0:
            cpu_word, cpu_icon, cpu_color = "Low", "🟢", "#34d399"
        elif cpu_pct < 75.0:
            cpu_word, cpu_icon, cpu_color = "Normal", "🔵", "#38bdf8"
        else:
            cpu_word, cpu_icon, cpu_color = "High", "🔴", "#f87171"

        if ram_pct < 50.0:
            ram_word, ram_icon, ram_color = "Low", "🟢", "#34d399"
        elif ram_pct < 80.0:
            ram_word, ram_icon, ram_color = "Normal", "🔵", "#38bdf8"
        else:
            ram_word, ram_icon, ram_color = "High", "🟠", "#fb923c"

        # Important Events: Upcoming Reminders + High/Open Tasks + Cached Google Calendar Events
        events: list[str] = []
        if time.time() - self._cal_events_ts > 180.0 and not self._cal_fetching:
            threading.Thread(target=self._refresh_calendar_events_bg, daemon=True).start()
        events.extend(self._cal_events_cache[:2])

        try:
            rems = db.upcoming_reminders(limit=4, user_id=self.owner_id)
            for r in rems:
                msg = str(r.get("message") or "").strip()
                if msg.startswith("[AGENT]"):
                    continue
                due = str(r.get("due_at") or "")[11:16] or str(r.get("due_at") or "")[:10]
                events.append(f"⏰ {due} — {msg[:46]}")
                if len(events) >= 3:
                    break
        except Exception:
            pass

        if len(events) < 3:
            try:
                tasks = db.list_tasks(status="open", limit=4, user_id=self.owner_id)
                for t in tasks:
                    prio = str(t.get("priority", "normal")).lower()
                    icon = "🔥" if prio == "high" else "📌"
                    due_s = f" (due {str(t['due_at'])[:10]})" if t.get("due_at") else ""
                    events.append(f"{icon} {t['title'][:44]}{due_s}")
                    if len(events) >= 3:
                        break
            except Exception:
                pass

        disk_pct = 0.0
        try:
            import shutil as _shutil
            usage = _shutil.disk_usage(config.BASE_DIR)
            if usage.total > 0:
                disk_pct = round((usage.used / usage.total) * 100.0, 1)
        except Exception:
            disk_pct = 0.0

        if disk_pct < 60.0:
            disk_word, disk_icon, disk_color = "Low", "🟢", "#34d399"
        elif disk_pct < 85.0:
            disk_word, disk_icon, disk_color = "Normal", "🟠", "#f59e0b"
        else:
            disk_word, disk_icon, disk_color = "High", "🔴", "#f87171"

        return {
            "media": media,
            "cpu_pct": round(cpu_pct, 1),
            "cpu_word": cpu_word,
            "cpu_icon": cpu_icon,
            "cpu_color": cpu_color,
            "ram_pct": round(ram_pct, 1),
            "ram_word": ram_word,
            "ram_icon": ram_icon,
            "ram_color": ram_color,
            "disk_pct": disk_pct,
            "disk_word": disk_word,
            "disk_icon": disk_icon,
            "disk_color": disk_color,
            "events": events[:3],
        }

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
            from PySide6.QtGui import QFont, QFontDatabase
            from PySide6.QtWidgets import QApplication

            app = QApplication.instance() or QApplication(sys.argv)
            # Load bundled Apple SF Pro Display & SF Pro Text fonts from assets/fonts
            fonts_dir = config.BASE_DIR / "assets" / "fonts"
            if fonts_dir.exists():
                for otf_path in sorted(fonts_dir.glob("*.otf")):
                    try:
                        QFontDatabase.addApplicationFont(str(otf_path))
                    except Exception:
                        pass
            app.setFont(QFont("SF Pro Display", 10))
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
        QImage,
        QLinearGradient,
        QPainter,
        QPainterPath,
        QPen,
        QPixmap,
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
    CANVAS_W = 860
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

    class _VerticalVolumeBar(QWidget):
        """Interactive vertical pill volume slider synced 1:1 with real Windows CoreAudio IAudioEndpointVolume."""

        def __init__(self, audio_meter: _WindowsAudioPeakMeter, parent: QWidget | None = None) -> None:
            super().__init__(parent)
            self._audio = audio_meter
            self.setFixedSize(26, 108)
            self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            self.setToolTip("Click, drag, or scroll to set exact Windows volume · Right-click to mute")
            self._level, self._muted = self._audio.get_master_volume()
            self._dragging: bool = False

        def sync_from_os(self) -> None:
            """Pull the live Windows master volume & mute state so the slider always shows the exact OS level."""
            if self._dragging:
                return
            lvl, muted = self._audio.get_master_volume()
            if abs(lvl - self._level) > 0.005 or muted != self._muted:
                self._level = lvl
                self._muted = muted
                self.update()

        def _apply_vol_from_y(self, y: float) -> None:
            top_y = 15.0
            bot_y = float(self.height()) - 18.0
            new_lvl = max(0.0, min(1.0, (bot_y - y) / max(1.0, bot_y - top_y)))
            self._level = new_lvl
            self._muted = new_lvl <= 0.005
            self._audio.set_master_volume(new_lvl)
            if self._muted:
                self._audio.set_mute(True)
            self.update()

        def wheelEvent(self, event: Any) -> None:
            delta = 0.04 if event.angleDelta().y() > 0 else -0.04
            new_lvl = max(0.0, min(1.0, self._level + delta))
            self._level = new_lvl
            self._muted = new_lvl <= 0.005
            self._audio.set_master_volume(new_lvl)
            self.update()
            event.accept()

        def mousePressEvent(self, event: Any) -> None:
            if event.button() == Qt.MouseButton.RightButton:
                self._muted = not self._muted
                self._audio.set_mute(self._muted)
                self.update()
                return
            if event.button() == Qt.MouseButton.LeftButton:
                # Clicking the speaker icon at the bottom toggles mute
                if float(event.position().y()) >= float(self.height()) - 18.0:
                    self._muted = not self._muted
                    self._audio.set_mute(self._muted)
                    self.update()
                    return
                self._dragging = True
                self._apply_vol_from_y(float(event.position().y()))

        def mouseMoveEvent(self, event: Any) -> None:
            if self._dragging:
                self._apply_vol_from_y(float(event.position().y()))

        def mouseReleaseEvent(self, event: Any) -> None:
            self._dragging = False
            super().mouseReleaseEvent(event)

        def paintEvent(self, _event: Any) -> None:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            w, h = float(self.width()), float(self.height())

            # Top numeric label ("100" / "65")
            p.setPen(QColor("#cbd5e1"))
            f = QFont("SF Pro Display", 7, QFont.Weight.Bold)
            p.setFont(f)
            pct_txt = "0" if self._muted else str(int(round(self._level * 100)))
            p.drawText(QRectF(0, 0, w, 12), Qt.AlignmentFlag.AlignCenter, pct_txt)

            # Vertical track
            track_w = 12.0
            track_x = (w - track_w) / 2.0
            track_top = 15.0
            track_h = h - 33.0
            track_rect = QRectF(track_x, track_top, track_w, track_h)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 28))
            p.drawRoundedRect(track_rect, 6.0, 6.0)

            # Active blue fill from bottom up
            fill_lvl = 0.0 if self._muted else self._level
            fill_h = max(0.0, track_h * fill_lvl)
            if fill_h > 2.0:
                fill_rect = QRectF(track_x, track_top + track_h - fill_h, track_w, fill_h)
                fg = QLinearGradient(0, fill_rect.top(), 0, fill_rect.bottom())
                fg.setColorAt(0.0, QColor("#93c5fd"))
                fg.setColorAt(1.0, QColor("#60a5fa"))
                p.setBrush(QBrush(fg))
                p.drawRoundedRect(fill_rect, 6.0, 6.0)

            # Bottom speaker glyph
            p.setPen(QColor("#94a3b8"))
            p.setFont(QFont("Segoe UI Emoji", 8))
            p.drawText(QRectF(0, h - 16, w, 16), Qt.AlignmentFlag.AlignCenter, "🔇" if self._muted else "🔊")

    class _DynamicNotchWindow(QWidget):
        """Fixed-canvas top-center Dynamic Island Notch window. Never shifts `x`; morphs symmetrically from center."""

        hotkey_triggered = Signal(str)
        reply_ready = Signal(str, str)
        composio_link_ready = Signal(str, str, str)
        shutdown_requested = Signal()

        # Target visual notch dimensions (width, height) inside the fixed 860x540 canvas
        SIZES: dict[str, tuple[int, int]] = {
            "tucked": (76, 3),         # Ultra-thin 3px top edge micro-line when cursor approaches tabs/titlebar below
            "idle": (96, 7),           # Compact micro-strip at top center when no music is playing
            "idle_music": (212, 23),   # Sleek horizontal music strip: Album Cover + JARVIS + Blurry-Color Visualizer
            "hover": (176, 20),        # Slightly larger hovered pill with status dot + JARVIS text
            "hover_music": (248, 25),  # Hovered horizontal music strip
            "dashboard": (790, 146),   # Wide sleek DockBar-style horizontal PC Dashboard strip!
            "suggestions": (488, 516), # Connections hub card
            "chat": (556, 336),        # JARVIS Quick Chat card
            "chat_active": (556, 468), # JARVIS Quick Chat card with active conversation
            "voice": (448, 276),       # Voice Cloud Orb card
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

            # Live 33fps Music Visualizer & Album Cover state for the Idle Dynamic Notch
            self._music_playing_active: bool = False
            self._tucked_dodge: bool = False
            self._vis_bars: list[float] = [0.22, 0.35, 0.50, 0.38, 0.28, 0.42]
            self._vis_phase: float = 0.0
            self._art_pixmap: QPixmap | None = None
            self._art_pixmap_track: str = ""

            # Adaptive App-Blending Notch Color (samples top of the active app right beside the notch)
            self._notch_bg_rgb: list[float] = [7.0, 8.0, 11.0]
            self._notch_target_rgb: list[float] = [7.0, 8.0, 11.0]
            self._notch_is_light: bool = False
            self._color_sample_ts: float = 0.0

            self._vis_timer = QTimer(self)
            self._vis_timer.timeout.connect(self._on_visualizer_tick)
            self._vis_timer.start(32)

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

            # --- Smart Title-Bar / Browser-Tab Dodge (Option 2) ---
            # When the notch is in idle/hover mode, if the user moves their mouse near the top title bar / browser tabs
            # (4 <= y <= 56 around the center) to click or see a tab underneath, automatically tuck the notch up into a
            # 3px micro-line so the tab underneath is 100% visible & clickable!
            # Pushing the cursor all the way to the very top edge of the monitor (y <= 2) untucks & opens the notch.
            pos = QCursor.pos()
            local = self.mapFromGlobal(pos)
            cx = CANVAS_W / 2.0
            in_center_x = abs(local.x() - cx) <= 165.0
            if self.state in ("idle", "hover"):
                if in_center_x and local.y() <= 2:
                    # User deliberately pushed cursor to the very top pixel edge of the monitor -> untuck & hover/open
                    if self._tucked_dodge:
                        self._tucked_dodge = False
                        self._animate_to("hover", duration_ms=360)
                    elif self.state == "idle":
                        self._animate_to("hover", duration_ms=380)
                elif in_center_x and 3 <= local.y() <= 54:
                    # User is hovering over a browser tab / window title bar right under the notch -> tuck up out of the way!
                    if not self._tucked_dodge and self.state == "idle":
                        self._tucked_dodge = True
                        self._animate_to("idle", duration_ms=340)
                else:
                    # Cursor moved away from the top tab bar -> restore the normal idle / music notch
                    if self._tucked_dodge:
                        self._tucked_dodge = False
                        self._animate_to("idle", duration_ms=460)

            if not just_clicked:
                return
            if self.pinned or self.state not in ("dashboard", "suggestions", "chat", "voice"):
                return

            # Check if the global cursor position is outside the current visible notch rectangle
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
            if getattr(self, "_tucked_dodge", False) and self.state == "idle" and h <= 6.0:
                # When tucked out of the way of a browser tab, only mask the top 2 pixels (y=0..2)
                # so the entire browser tab / title bar underneath is 100% OS click-through!
                tuck_w = 140
                self.setMask(QRegion(QRect((CANVAS_W - tuck_w) // 2, 0, tuck_w, 2)))
            else:
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

        def _sample_top_app_color(self) -> None:
            """Sample 2 pixels immediately left & right of the notch on the active app's titlebar and compute a subtle darker/lighter shade."""
            if os.name != "nt":
                return
            try:
                user32 = ctypes.windll.user32
                gdi32 = ctypes.windll.gdi32
                hdc = user32.GetDC(0)
                if not hdc:
                    return
                try:
                    sw = user32.GetSystemMetrics(0)
                    cx = sw // 2
                    offset = int(max(145, self._notch_w / 2.0 + 36))
                    samples: list[tuple[int, int, int]] = []
                    for dx in (-offset, offset):
                        sx = max(0, min(sw - 1, cx + dx))
                        col = gdi32.GetPixel(hdc, sx, 6)
                        if col != -1 and col != 0xFFFFFFFF:
                            samples.append((col & 0xFF, (col >> 8) & 0xFF, (col >> 16) & 0xFF))
                finally:
                    user32.ReleaseDC(0, hdc)

                if not samples:
                    return
                avg_r = sum(s[0] for s in samples) / len(samples)
                avg_g = sum(s[1] for s in samples) / len(samples)
                avg_b = sum(s[2] for s in samples) / len(samples)
                lum = 0.299 * avg_r + 0.587 * avg_g + 0.114 * avg_b

                if lum > 145.0:
                    # Light app titlebar (e.g. Chrome Light, Explorer, Word): use a sleek darker shade of that app's color
                    tr = max(0.0, avg_r - 28.0)
                    tg = max(0.0, avg_g - 28.0)
                    tb = max(0.0, avg_b - 28.0)
                    self._notch_is_light = True
                elif lum > 42.0:
                    # Mid-tone / dark-grey app titlebar (e.g. Discord, VS Code, Chrome Dark): slightly darker shade
                    tr = max(8.0, avg_r * 0.72)
                    tg = max(8.0, avg_g * 0.72)
                    tb = max(10.0, avg_b * 0.72)
                    self._notch_is_light = False
                else:
                    # Pitch-black / OLED dark app titlebar (e.g. Spotify, Dark Terminal): slightly lighter elevated charcoal shade
                    tr = min(255.0, avg_r + 18.0)
                    tg = min(255.0, avg_g + 19.0)
                    tb = min(255.0, avg_b + 23.0)
                    self._notch_is_light = False

                self._notch_target_rgb = [tr, tg, tb]
            except Exception:
                pass

        def _on_visualizer_tick(self) -> None:
            """30fps tick that syncs the Dynamic Island idle visualizer with real Windows audio output & album cover."""
            media = self.service.get_cached_now_playing()
            is_playing = bool(media.get("playing"))

            # Refresh cached QPixmap when album art bytes arrive for the active track
            cur_bytes = self.service._current_art_bytes
            cur_track = self.service._current_art_track
            if cur_bytes and cur_track != self._art_pixmap_track:
                img = QImage.fromData(cur_bytes)
                if not img.isNull():
                    self._art_pixmap = QPixmap.fromImage(img)
                    self._art_pixmap_track = cur_track
            elif not is_playing:
                self._art_pixmap = None
                self._art_pixmap_track = ""

            # Automatically morph the idle/hover notch size when music starts or stops
            if is_playing != self._music_playing_active:
                self._music_playing_active = is_playing
                if self.state in ("idle", "hover"):
                    self._animate_to(self.state, duration_ms=460)

            if not is_playing:
                return

            # Sample live Windows WASAPI master audio peak (0.0 .. 1.0) with balanced, natural responsiveness
            raw_peak = self.service._audio_meter.get_peak()
            prev_peak = getattr(self, "_prev_raw_peak", 0.0)
            self._prev_raw_peak = raw_peak

            # Gentle adaptive normalization so quiet and loud songs both sit in a natural middle range
            run_max = getattr(self, "_vis_running_max", 0.22)
            if raw_peak > run_max:
                run_max = run_max * 0.65 + raw_peak * 0.35
            else:
                run_max = max(0.08, run_max * 0.992)
            self._vis_running_max = run_max

            norm_peak = min(1.0, raw_peak / max(0.08, run_max))
            beat_delta = max(0.0, (raw_peak - prev_peak) / max(0.08, run_max))
            self._vis_phase += 0.22 + norm_peak * 0.28 + beat_delta * 0.35

            # Balanced energy: responsive to the beat without pegging at the top ceiling
            energy = min(0.92, max(0.22, math.pow(norm_peak, 0.72) * 0.85 + beat_delta * 0.55))
            freq_weights = (0.78, 1.05, 0.90, 1.10, 0.86, 0.72)
            phase_offsets = (0.0, 1.2, 2.5, 0.8, 2.0, 3.3)

            for i in range(len(self._vis_bars)):
                wave = 0.28 + 0.72 * abs(math.sin(self._vis_phase * (0.95 + i * 0.16) + phase_offsets[i]))
                target = max(0.15, min(0.95, energy * freq_weights[i] * wave))
                prev = self._vis_bars[i]
                # Smooth, fluid attack and decay
                if target > prev:
                    self._vis_bars[i] = prev * 0.42 + target * 0.58
                else:
                    self._vis_bars[i] = prev * 0.68 + target * 0.32

            if self.state in ("idle", "hover"):
                self.update()

        def _animate_to(self, new_state: str, duration_ms: int = 540) -> None:
            prev_state = self.state
            self.state = new_state
            if new_state == "chat" and self._has_messages:
                key = "chat_active"
            elif new_state == "idle" and getattr(self, "_tucked_dodge", False):
                key = "tucked"
            elif new_state == "idle" and getattr(self, "_music_playing_active", False):
                key = "idle_music"
            elif new_state == "hover" and getattr(self, "_music_playing_active", False):
                key = "hover_music"
            else:
                key = new_state
            tw, th = self.SIZES.get(key, self.SIZES["idle"])

            is_expanding = new_state in ("dashboard", "suggestions", "chat", "voice")
            was_expanded = prev_state in ("dashboard", "suggestions", "chat", "voice")

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
            if self.state in ("dashboard", "suggestions", "chat", "voice"):
                self._opacity_effect.setOpacity(1.0)
                self._content_root.show()
                if self.state == "chat":
                    self.activateWindow()
                    self._chat_input.setFocus()
            else:
                self._content_root.hide()

        def _switch_stack_page(self, mode: str) -> None:
            if mode == "dashboard":
                self._refresh_dashboard()
                self._stack.setCurrentIndex(0)
            elif mode == "suggestions":
                self._refresh_suggestions()
                self._stack.setCurrentIndex(1)
            elif mode == "chat":
                self._stack.setCurrentIndex(2)
            elif mode == "voice":
                self._stack.setCurrentIndex(3)
            self._content_root.show()

        # --- Hover & Click Events on the Notch ---

        def enterEvent(self, event: Any) -> None:
            if self.state == "idle":
                local = self.mapFromGlobal(QCursor.pos())
                # Only trigger hover expansion when pushing the cursor to the very top 2px edge of the screen;
                # if the cursor enters from below (e.g. aiming for a browser tab underneath), tuck out of the way!
                if local.y() <= 2:
                    self._tucked_dodge = False
                    self._animate_to("hover", duration_ms=420)
                elif not self._tucked_dodge:
                    self._tucked_dodge = True
                    self._animate_to("idle", duration_ms=340)
            super().enterEvent(event)

        def leaveEvent(self, event: Any) -> None:
            if self.state == "hover":
                local = self.mapFromGlobal(QCursor.pos())
                if 3 <= local.y() <= 54 and abs(local.x() - CANVAS_W / 2.0) <= 165.0:
                    self._tucked_dodge = True
                self._animate_to("idle", duration_ms=460)
            super().leaveEvent(event)

        def mousePressEvent(self, event: Any) -> None:
            if event.button() == Qt.MouseButton.LeftButton and self.state in ("idle", "hover"):
                # Open directly to the PC Home Dashboard (JARVIS Chat is tucked into its own ✨ icon)
                self._animate_to("dashboard", duration_ms=560)
                return
            super().mousePressEvent(event)

        def keyPressEvent(self, event: Any) -> None:
            if event.key() == Qt.Key.Key_Escape and self.state not in ("idle", "hover"):
                if self.service._mic.is_recording:
                    self.service._mic.stop()
                self._animate_to("idle", duration_ms=460)
                return
            super().keyPressEvent(event)

        def _on_hotkey_toggle(self, target_mode: str = "dashboard") -> None:
            mode = (target_mode or "dashboard").strip().lower()
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

            # Ctrl+/: Open directly into PC Home Dashboard (or collapse if already open)
            if self.state in ("dashboard", "chat", "suggestions"):
                self._animate_to("idle", duration_ms=460)
            else:
                if self.service._mic.is_recording:
                    self.service._mic.stop()
                self._orb.active_pulse = False
                self._animate_to("dashboard", duration_ms=520)

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

            # Solid pure black notch fill (#07080b) — no blue gradient glow!
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#07080b"))
            p.drawPath(path)

            if nh > 60:
                pass
            elif self.state in ("idle", "hover") and getattr(self, "_music_playing_active", False) and nh >= 13:
                # --- Sleek Horizontal Dynamic Island Music Strip (Pitch-Black Notch + Album Cover + Blurry Background-Colored Visualizer) ---
                p.save()
                p.setClipPath(path)

                palette = self.service._current_art_palette or [
                    (56, 189, 248),
                    (168, 85, 247),
                    (52, 211, 153),
                ]
                c0 = QColor(*palette[0])
                c1 = QColor(*palette[1 % len(palette)])
                c2 = QColor(*palette[2 % len(palette)])

                # 1. Left Side: Compact Rounded Album Cover Artwork Thumbnail inside the pure black notch
                art_size = max(13.0, min(18.0, nh - 5.0))
                art_x = left + 9.0
                art_y = (nh - art_size) / 2.0
                art_rect = QRectF(art_x, art_y, art_size, art_size)

                art_clip = QPainterPath()
                art_clip.addRoundedRect(art_rect, 4.2, 4.2)
                p.save()
                p.setClipPath(art_clip)
                if self._art_pixmap is not None and not self._art_pixmap.isNull():
                    p.drawPixmap(art_rect.toRect(), self._art_pixmap)
                else:
                    disc_g = QLinearGradient(art_x, art_y, art_x + art_size, art_y + art_size)
                    disc_g.setColorAt(0.0, c0)
                    disc_g.setColorAt(1.0, c1)
                    p.setBrush(QBrush(disc_g))
                    p.drawRect(art_rect)
                p.restore()

                p.setPen(QPen(QColor(255, 255, 255, 50), 0.7))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(art_rect, 4.2, 4.2)

                # 2. Center: Crisp "JARVIS" label in the middle between the Album Cover and Visualizer
                p.setPen(QColor(226, 232, 240, 215))
                font = QFont("SF Pro Display", 8, QFont.Weight.Bold)
                font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.8)
                p.setFont(font)
                p.drawText(QRectF(left, 0.0, nw, nh), Qt.AlignmentFlag.AlignCenter, "JARVIS")

                # 3. Right Side: Live Audio Visualizer (colored with the blurry album background palette on the pure black notch)
                bars = self._vis_bars
                bar_w = 2.6
                gap = 2.1
                vis_total_w = len(bars) * bar_w + (len(bars) - 1) * gap
                vis_right_pad = 10.0
                vis_start_x = right - vis_right_pad - vis_total_w

                max_bar_h = max(10.0, nh - 6.0)
                cy_vis = nh / 2.0

                bar_grad = QLinearGradient(vis_start_x, cy_vis - max_bar_h / 2.0, vis_start_x + vis_total_w, cy_vis + max_bar_h / 2.0)
                bar_grad.setColorAt(0.0, c0)
                bar_grad.setColorAt(0.5, c1)
                bar_grad.setColorAt(1.0, c2)

                p.setPen(Qt.PenStyle.NoPen)
                for i, amp in enumerate(bars):
                    bx = vis_start_x + i * (bar_w + gap)
                    bh = max(2.2, min(max_bar_h, amp * max_bar_h))
                    by = cy_vis - bh / 2.0
                    # Soft blurry colour halo directly around each visualizer bar
                    glow_col = QColor(palette[i % len(palette)][0], palette[i % len(palette)][1], palette[i % len(palette)][2], 85)
                    p.setBrush(glow_col)
                    p.drawRoundedRect(QRectF(bx - 1.0, by - 1.0, bar_w + 2.0, bh + 2.0), 1.9, 1.9)
                    # Crisp inner bar
                    p.setBrush(QBrush(bar_grad))
                    p.drawRoundedRect(QRectF(bx, by, bar_w, bh), 1.3, 1.3)

                p.restore()
            elif self.state == "hover":
                # Glowing emerald status dot + crisp 'JARVIS' micro-label ONLY when actively hovering the notch
                if nh >= 15:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QColor("#34d399"))
                    p.drawEllipse(QPointF(left + 14.0, nh / 2.0), 2.8, 2.8)
                    p.setPen(QColor(226, 232, 240, 210))
                    font = QFont("SF Pro Display", 8, QFont.Weight.Bold)
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
            self.setStyleSheet("QWidget { font-family: 'SF Pro Display', 'SF Pro Text', sans-serif; }")
            self._content_root = QWidget(self)
            self._opacity_effect = QGraphicsOpacityEffect(self._content_root)
            self._opacity_effect.setOpacity(1.0)
            self._content_root.setGraphicsEffect(self._opacity_effect)
            root_layout = QVBoxLayout(self._content_root)
            root_layout.setContentsMargins(0, 0, 0, 0)
            root_layout.setSpacing(0)

            self._stack = QStackedWidget(self._content_root)
            self._stack.addWidget(self._build_dashboard_page())
            self._stack.addWidget(self._build_suggestions_page())
            self._stack.addWidget(self._build_chat_page())
            self._stack.addWidget(self._build_voice_page())

            root_layout.addWidget(self._stack)
            self._content_root.hide()

            # Auto-refresh dashboard every 2.5 seconds while visible
            self._dash_timer = QTimer(self)
            self._dash_timer.timeout.connect(self._on_dash_timer_tick)
            self._dash_timer.start(2500)

        def _on_dash_timer_tick(self) -> None:
            if self.state == "dashboard" and self.isVisible():
                self._refresh_dashboard()

        def _build_dashboard_page(self) -> QWidget:
            """Wide DockBar-style Horizontal Notch Dashboard:
            [Album Art + Track/Artist + ⏮⏯⏭] | [Month + 7-Day Week Strip + Clock + Event] | [System Monitor: CPU/RAM/DISK bars] | [Volume Bar] [AI / Voice / Connections / Pin]
            """
            page = QWidget()
            outer = QVBoxLayout(page)
            outer.setContentsMargins(6, 4, 6, 4)
            outer.setSpacing(4)

            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(12)

            # ================= SECTION 1: NOW PLAYING (Left) =================
            media_wrap = QHBoxLayout()
            media_wrap.setSpacing(10)

            self._dash_music_icon = QLabel("🎵")
            self._dash_music_icon.setFixedSize(78, 78)
            self._dash_music_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._dash_music_icon.setStyleSheet(
                "background: rgba(255, 255, 255, 0.06); border: 1px solid rgba(255, 255, 255, 0.14); "
                "border-radius: 14px; font-size: 28px;"
            )
            media_wrap.addWidget(self._dash_music_icon, alignment=Qt.AlignmentFlag.AlignVCenter)

            m_info = QVBoxLayout()
            m_info.setSpacing(2)
            m_info.setContentsMargins(0, 4, 4, 2)

            self._dash_media_track = QLabel("Nothing Playing")
            self._dash_media_track.setFixedWidth(132)
            self._dash_media_track.setStyleSheet(
                "color: #ffffff; font-size: 13px; font-weight: 800; background: transparent;"
            )
            self._dash_media_artist = QLabel("Spotify Idle")
            self._dash_media_artist.setFixedWidth(132)
            self._dash_media_artist.setStyleSheet(
                "color: #94a3b8; font-size: 11px; font-weight: 600; background: transparent;"
            )
            m_info.addWidget(self._dash_media_track)
            m_info.addWidget(self._dash_media_artist)
            m_info.addSpacing(4)

            ctrl_row = QHBoxLayout()
            ctrl_row.setSpacing(6)
            for sym, act, tip in (
                ("⏮", "prev_track", "Previous Track"),
                ("⏯", "play_pause", "Play / Pause"),
                ("⏭", "next_track", "Next Track"),
            ):
                b = QPushButton(sym)
                b.setFixedSize(30, 26)
                b.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
                b.setToolTip(tip)
                b.setStyleSheet(
                    "QPushButton { background: rgba(255,255,255,0.08); color: #f8fafc; "
                    "border: none; border-radius: 7px; font-size: 12px; }"
                    "QPushButton:hover { background: rgba(255,255,255,0.20); }"
                )
                b.clicked.connect(lambda _c=False, a=act: self._on_dash_media_control(a))
                ctrl_row.addWidget(b)
            ctrl_row.addStretch(1)
            m_info.addLayout(ctrl_row)

            # Subtle sleek progress accent bar underneath controls (stays cleanly left of the divider!)
            prog_track = QFrame()
            prog_track.setFixedSize(124, 3)
            prog_track.setStyleSheet("background: rgba(255,255,255,0.14); border-radius: 1.5px;")
            m_info.addSpacing(4)
            m_info.addWidget(prog_track)
            m_info.addStretch(1)

            media_wrap.addLayout(m_info)
            row.addLayout(media_wrap)

            # Vertical hairline divider 1
            div1 = QFrame()
            div1.setFixedSize(1, 86)
            div1.setStyleSheet("background: rgba(255,255,255,0.12);")
            row.addWidget(div1, alignment=Qt.AlignmentFlag.AlignVCenter)

            # ================= SECTION 2: CALENDAR STRIP + LIVE CLOCK + NEXT EVENT (Center) =================
            cal_col = QVBoxLayout()
            cal_col.setSpacing(4)
            cal_col.setContentsMargins(2, 2, 2, 0)

            top_cal_row = QHBoxLayout()
            top_cal_row.setSpacing(10)

            self._dash_month_lbl = QLabel("Nov")
            self._dash_month_lbl.setStyleSheet(
                "color: #ffffff; font-size: 24px; font-weight: 800; background: transparent;"
            )
            top_cal_row.addWidget(self._dash_month_lbl, alignment=Qt.AlignmentFlag.AlignVCenter)

            # 7-day calendar strip (M T W T F S S)
            week_row = QHBoxLayout()
            week_row.setSpacing(5)
            self._dash_week_day_lbls: list[QLabel] = []
            self._dash_week_num_lbls: list[QLabel] = []
            for _ in range(7):
                dc = QVBoxLayout()
                dc.setSpacing(1)
                dc.setAlignment(Qt.AlignmentFlag.AlignCenter)
                dl = QLabel("M")
                dl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                dl.setStyleSheet("color: #64748b; font-size: 8.5px; font-weight: 700; background: transparent;")
                nl = QLabel("1")
                nl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                nl.setStyleSheet("color: #e2e8f0; font-size: 11px; font-weight: 700; background: transparent;")
                dc.addWidget(dl)
                dc.addWidget(nl)
                week_row.addLayout(dc)
                self._dash_week_day_lbls.append(dl)
                self._dash_week_num_lbls.append(nl)
            top_cal_row.addLayout(week_row)
            cal_col.addLayout(top_cal_row)

            # Crisp Centered Clock + Next Event Pill
            self._dash_clock_lbl = QLabel("14:48")
            self._dash_clock_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._dash_clock_lbl.setStyleSheet(
                "color: #ffffff; font-size: 19px; font-weight: 800; letter-spacing: 0.6px; background: transparent;"
            )
            cal_col.addWidget(self._dash_clock_lbl)

            self._dash_event_pill = QLabel("✨ No urgent events")
            self._dash_event_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._dash_event_pill.setStyleSheet(
                "color: #93c5fd; font-size: 10px; font-weight: 700; background: transparent;"
            )
            cal_col.addWidget(self._dash_event_pill)
            row.addLayout(cal_col)

            # Vertical hairline divider 2
            div2 = QFrame()
            div2.setFixedSize(1, 86)
            div2.setStyleSheet("background: rgba(255,255,255,0.12);")
            row.addWidget(div2, alignment=Qt.AlignmentFlag.AlignVCenter)

            # ================= SECTION 3: SYSTEM MONITOR (CPU / RAM / DISK BARS + WORDS) =================
            sys_col = QVBoxLayout()
            sys_col.setSpacing(6)
            sys_col.setContentsMargins(2, 4, 2, 2)

            sys_hdr_row = QHBoxLayout()
            sys_title = QLabel("🖥  System Monitor")
            sys_title.setStyleSheet("color: #f8fafc; font-size: 11px; font-weight: 800; background: transparent;")
            self._dash_health_badge = QLabel("OK")
            self._dash_health_badge.setStyleSheet(
                "background: rgba(34, 197, 94, 0.22); color: #4ade80; "
                "border-radius: 5px; padding: 1px 6px; font-size: 9px; font-weight: 800;"
            )
            sys_hdr_row.addWidget(sys_title)
            sys_hdr_row.addStretch(1)
            sys_hdr_row.addWidget(self._dash_health_badge)
            sys_col.addLayout(sys_hdr_row)

            def _make_metric_row(icon: str, name: str, bar_color: str) -> tuple[QHBoxLayout, QFrame, QLabel]:
                mr = QHBoxLayout()
                mr.setSpacing(6)
                lbl = QLabel(f"{icon} {name}")
                lbl.setFixedWidth(48)
                lbl.setStyleSheet("color: #cbd5e1; font-size: 10px; font-weight: 700; background: transparent;")
                track = QFrame()
                track.setFixedSize(76, 5)
                track.setStyleSheet("background: rgba(255,255,255,0.12); border-radius: 2.5px;")
                fill = QFrame(track)
                fill.setGeometry(0, 0, 24, 5)
                fill.setStyleSheet(f"background: {bar_color}; border-radius: 2.5px;")
                word_lbl = QLabel("Low")
                word_lbl.setFixedWidth(48)
                word_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                word_lbl.setStyleSheet(f"color: {bar_color}; font-size: 10px; font-weight: 800; background: transparent;")
                mr.addWidget(lbl)
                mr.addWidget(track)
                mr.addWidget(word_lbl)
                return mr, fill, word_lbl

            cpu_r, self._dash_cpu_bar, self._dash_cpu_word = _make_metric_row("⚡", "CPU", "#38bdf8")
            ram_r, self._dash_ram_bar, self._dash_ram_word = _make_metric_row("🧠", "RAM", "#4ade80")
            dsk_r, self._dash_disk_bar, self._dash_disk_word = _make_metric_row("💾", "DISK", "#f59e0b")
            sys_col.addLayout(cpu_r)
            sys_col.addLayout(ram_r)
            sys_col.addLayout(dsk_r)
            sys_col.addStretch(1)
            row.addLayout(sys_col)

            # ================= SECTION 4: VERTICAL VOLUME SLIDER + JARVIS / VOICE / CONNECTIONS / PIN =================
            self._vol_slider = _VerticalVolumeBar(self.service._audio_meter)
            row.addWidget(self._vol_slider, alignment=Qt.AlignmentFlag.AlignVCenter)

            right_grid = QVBoxLayout()
            right_grid.setSpacing(5)
            right_grid.setAlignment(Qt.AlignmentFlag.AlignVCenter)

            btn_jarvis = _VectorIconButton("chat", "Open JARVIS AI Quick Chat", size=28)
            btn_jarvis.clicked.connect(lambda: self._animate_to("chat", duration_ms=460))
            btn_wave = _VectorIconButton("waveform", "Live Voice Cloud Orb Mode", size=28)
            btn_wave.clicked.connect(self._enter_voice_mode)
            btn_conn = _VectorIconButton("connections", "Composio App Connections Hub", size=28)
            btn_conn.clicked.connect(lambda: self._animate_to("suggestions", duration_ms=460))
            self._pin_btn_dash = _VectorIconButton("pin", "Pin Notch Open", size=28)
            self._pin_btn_dash.clicked.connect(self._toggle_pin)

            top_r_btns = QHBoxLayout()
            top_r_btns.setSpacing(5)
            top_r_btns.addWidget(btn_jarvis)
            top_r_btns.addWidget(btn_wave)
            bot_r_btns = QHBoxLayout()
            bot_r_btns.setSpacing(5)
            bot_r_btns.addWidget(btn_conn)
            bot_r_btns.addWidget(self._pin_btn_dash)

            right_grid.addLayout(top_r_btns)
            right_grid.addLayout(bot_r_btns)
            row.addLayout(right_grid)

            outer.addLayout(row, stretch=1)

            # Bottom subtle drag/grabber pill (just like the reference image!)
            grabber = QFrame()
            grabber.setFixedSize(44, 3)
            grabber.setStyleSheet("background: rgba(255,255,255,0.28); border-radius: 1.5px;")
            outer.addWidget(grabber, alignment=Qt.AlignmentFlag.AlignHCenter)
            return page

        def _on_dash_media_control(self, action: str) -> None:
            """Send Windows hardware media key + WM_APPCOMMAND (play_pause, next_track, prev_track) from the Dashboard Now Playing card."""
            def _run() -> None:
                try:
                    from tools.pc_control import send_media_transport_command
                    send_media_transport_command(action)
                    time.sleep(0.35)
                except Exception:
                    pass
            threading.Thread(target=_run, daemon=True).start()
            QTimer.singleShot(420, self._refresh_dashboard)

        def _refresh_dashboard(self) -> None:
            """Update the wide DockBar-style Horizontal Dashboard with live Media, 7-Day Calendar, Clock, Events & System Monitor."""
            from datetime import timedelta as _td

            now = datetime.now(config.get_zoneinfo())
            self._dash_month_lbl.setText(now.strftime("%b"))
            # 12-hour clock format without leading zero (e.g. "2:55 PM")
            h12 = now.hour % 12 or 12
            ampm = "AM" if now.hour < 12 else "PM"
            self._dash_clock_lbl.setText(f"{h12}:{now.minute:02d} {ampm}")
            if hasattr(self, "_vol_slider"):
                self._vol_slider.sync_from_os()

            # Populate 7-day week strip (Mon..Sun of current week) with today highlighted in bright blue & weekends in red
            monday = now - _td(days=now.weekday())
            day_letters = ("M", "T", "W", "T", "F", "S", "S")
            for idx in range(7):
                d = monday + _td(days=idx)
                is_today = d.date() == now.date()
                is_weekend = idx >= 5
                dl = self._dash_week_day_lbls[idx]
                nl = self._dash_week_num_lbls[idx]
                dl.setText(now.strftime("%a").upper() if is_today else day_letters[idx])
                nl.setText(str(d.day))
                if is_today:
                    dl.setStyleSheet("color: #f8fafc; font-size: 8.5px; font-weight: 800; background: transparent;")
                    nl.setStyleSheet("color: #60a5fa; font-size: 13px; font-weight: 900; background: transparent;")
                elif is_weekend:
                    dl.setStyleSheet("color: #ef4444; font-size: 8.5px; font-weight: 700; background: transparent;")
                    nl.setStyleSheet("color: #f87171; font-size: 11px; font-weight: 700; background: transparent;")
                else:
                    dl.setStyleSheet("color: #64748b; font-size: 8.5px; font-weight: 700; background: transparent;")
                    nl.setStyleSheet("color: #cbd5e1; font-size: 11px; font-weight: 700; background: transparent;")

            snap = self.service.fetch_dashboard_snapshot()
            media = snap["media"]
            raw_track = str(media.get("track") or "Nothing Playing")
            if " - " in raw_track:
                artist_part, song_part = (p.strip() for p in raw_track.split(" - ", 1))
            else:
                song_part, artist_part = raw_track.strip(), str(media.get("source") or "Spotify Idle")
            # Pixel-accurate eliding using the exact bold stylesheet font so long song names NEVER cross the divider line
            from PySide6.QtGui import QFontMetrics
            song_font = QFont("SF Pro Display")
            song_font.setPixelSize(13)
            song_font.setWeight(QFont.Weight.ExtraBold)
            elided_song = QFontMetrics(song_font).elidedText(song_part, Qt.TextElideMode.ElideRight, 122)

            art_font = QFont("SF Pro Display")
            art_font.setPixelSize(11)
            art_font.setWeight(QFont.Weight.DemiBold)
            elided_artist = QFontMetrics(art_font).elidedText(artist_part, Qt.TextElideMode.ElideRight, 122)

            self._dash_media_track.setText(elided_song)
            self._dash_media_track.setToolTip(song_part)
            self._dash_media_artist.setText(elided_artist)
            self._dash_media_artist.setToolTip(artist_part)

            if self._art_pixmap is not None and not self._art_pixmap.isNull():
                scaled_art = self._art_pixmap.scaled(
                    76,
                    76,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self._dash_music_icon.setPixmap(scaled_art)
            else:
                self._dash_music_icon.setPixmap(QPixmap())
                self._dash_music_icon.setText("🎧" if media.get("playing") else "🎵")

            # System Monitor: progress bars + Words (Low / Normal / High)
            cpu_w = max(6, int(76.0 * min(1.0, float(snap.get("cpu_pct", 10.0)) / 100.0)))
            ram_w = max(6, int(76.0 * min(1.0, float(snap.get("ram_pct", 50.0)) / 100.0)))
            dsk_w = max(6, int(76.0 * min(1.0, float(snap.get("disk_pct", 40.0)) / 100.0)))

            self._dash_cpu_bar.setFixedWidth(cpu_w)
            self._dash_cpu_bar.setStyleSheet(f"background: {snap['cpu_color']}; border-radius: 2.5px;")
            self._dash_cpu_word.setText(snap["cpu_word"])
            self._dash_cpu_word.setStyleSheet(f"color: {snap['cpu_color']}; font-size: 10px; font-weight: 800; background: transparent;")

            self._dash_ram_bar.setFixedWidth(ram_w)
            self._dash_ram_bar.setStyleSheet(f"background: {snap['ram_color']}; border-radius: 2.5px;")
            self._dash_ram_word.setText(snap["ram_word"])
            self._dash_ram_word.setStyleSheet(f"color: {snap['ram_color']}; font-size: 10px; font-weight: 800; background: transparent;")

            self._dash_disk_bar.setFixedWidth(dsk_w)
            self._dash_disk_bar.setStyleSheet(f"background: {snap['disk_color']}; border-radius: 2.5px;")
            self._dash_disk_word.setText(snap["disk_word"])
            self._dash_disk_word.setStyleSheet(f"color: {snap['disk_color']}; font-size: 10px; font-weight: 800; background: transparent;")

            is_high = snap["cpu_word"] == "High" or snap["ram_word"] == "High"
            self._dash_health_badge.setText("HIGH" if is_high else "GOOD")
            self._dash_health_badge.setStyleSheet(
                f"background: {'rgba(239, 68, 68, 0.22)' if is_high else 'rgba(34, 197, 94, 0.22)'}; "
                f"color: {'#f87171' if is_high else '#4ade80'}; "
                "border-radius: 5px; padding: 1px 6px; font-size: 9px; font-weight: 800;"
            )

            events = snap.get("events") or []
            if events:
                ev_str = str(events[0])
                if len(ev_str) > 34:
                    ev_str = ev_str[:32] + "…"
                self._dash_event_pill.setText(ev_str)
            else:
                self._dash_event_pill.setText("✨ No urgent events today")

        def _build_suggestions_page(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(4, 2, 4, 2)
            layout.setSpacing(8)

            top = QHBoxLayout()
            top.setContentsMargins(0, 0, 0, 0)

            left_nav = QHBoxLayout()
            left_nav.setSpacing(6)
            btn_home = _VectorIconButton("home", "Back to PC Home Dashboard")
            btn_home.clicked.connect(lambda: self._animate_to("dashboard", duration_ms=480))
            btn_jarvis_sugg = _VectorIconButton("chat", "Open JARVIS AI Chat")
            btn_jarvis_sugg.clicked.connect(lambda: self._animate_to("chat", duration_ms=480))
            self._sugg_nav_btn_page0 = _VectorIconButton(
                "connections",
                "Composio Universal Connections Hub (1-click connect apps without .env edits)",
                badge_count=0,
            )
            self._sugg_nav_btn_page0.active_state = True
            self._sugg_nav_btn_page0.clicked.connect(lambda: self._animate_to("dashboard", duration_ms=480))
            left_nav.addWidget(btn_home)
            left_nav.addWidget(btn_jarvis_sugg)
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

            self._sugg_footer_btn = QPushButton("Back to PC Dashboard")
            self._sugg_footer_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            self._sugg_footer_btn.setStyleSheet(
                "QPushButton { background: transparent; color: #94a3b8; border: none; "
                "font-size: 12px; font-weight: 600; padding: 3px; }"
                "QPushButton:hover { color: #38bdf8; }"
            )
            self._sugg_footer_btn.clicked.connect(lambda: self._animate_to("dashboard", duration_ms=360))
            layout.addWidget(self._sugg_footer_btn, alignment=Qt.AlignmentFlag.AlignHCenter)
            return page

        def _refresh_suggestions(self) -> None:
            """Populate the scrollable list of connectable Composio apps."""
            while self._sugg_list_layout.count():
                item = self._sugg_list_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()

            self._sugg_heading.setText("Connections")
            self._sugg_footer_btn.setText("Back to PC Dashboard")
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
            btn_home = _VectorIconButton("home", "PC Home Dashboard")
            btn_home.clicked.connect(lambda: self._animate_to("dashboard", duration_ms=460))
            btn_jarvis_chat = _VectorIconButton("chat", "JARVIS AI Chat")
            btn_jarvis_chat.active_state = True
            btn_jarvis_chat.clicked.connect(self._reset_to_chat_hero)
            self._sugg_nav_btn_chat = _VectorIconButton(
                "connections",
                "Composio Universal Connections Hub (1-click connect apps without .env edits)",
                badge_count=0,
            )
            self._sugg_nav_btn_chat.clicked.connect(lambda: self._animate_to("suggestions", duration_ms=480))

            # Live LLM Model Switcher Pill in the Notch Top Bar (Cloud Gemini/Gemma + Local LM Studio)
            self._model_cycle_list = [
                ("lmstudio:qwen3-8b", "⚡ LM Studio: Qwen3 8B"),
                ("lmstudio:auto", "💻 LM Studio (Active Model)"),
                ("gemma-4-26b-a4b-it", "🧠 Gemma 4 26B"),
                ("gemini-3.5-flash-lite", "⚡ Gemini 3.5 Lite"),
                ("gemini-3.8-flash", "☁️ Gemini 3.8"),
                ("lmstudio:bonsai-27b", "🌿 LM Studio: Bonsai 27B"),
            ]
            self._model_pill_btn = QPushButton(self._current_model_label())
            self._model_pill_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            self._model_pill_btn.setFixedHeight(30)
            self._model_pill_btn.setToolTip("Click to switch active LLM model (Cloud Gemini/Gemma or Local Ollama)")
            self._model_pill_btn.setStyleSheet(
                "QPushButton { background: rgba(255,255,255,0.07); color: #cbd5e1; "
                "border: 1px solid rgba(255,255,255,0.14); border-radius: 10px; "
                "padding: 0 11px; font-size: 11px; font-weight: 700; }"
                "QPushButton:hover { background: rgba(56,189,248,0.16); color: #ffffff; "
                "border: 1px solid rgba(56,189,248,0.45); }"
            )
            self._model_pill_btn.clicked.connect(self._cycle_active_model)

            btn_wave = _VectorIconButton("waveform", "Voice Orb Mode")
            btn_wave.clicked.connect(self._enter_voice_mode)
            self._pin_btn_chat = _VectorIconButton("pin", "Pin Overlay Open")
            self._pin_btn_chat.clicked.connect(self._toggle_pin)
            btn_collapse = _VectorIconButton("close", "Collapse to Top Notch (Esc)")
            btn_collapse.clicked.connect(lambda: self._animate_to("idle", duration_ms=460))
            top.addWidget(btn_home)
            top.addWidget(btn_jarvis_chat)
            top.addWidget(self._sugg_nav_btn_chat)
            top.addStretch(1)
            top.addWidget(self._model_pill_btn)
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
                "padding: 10px 12px; font-size: 13px; font-family: 'SF Pro Text', 'SF Pro Display', sans-serif; }"
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

        def _current_model_label(self) -> str:
            cur = (config.LLM_MODEL or "").strip()
            for m_id, lbl in getattr(self, "_model_cycle_list", []):
                if m_id == cur:
                    return lbl
            if cur.lower().startswith(("ollama:", "local:")):
                return f"💻 {cur.split(':', 1)[1][:14]}"
            return f"🧠 {cur[:15]}"

        def _cycle_active_model(self) -> None:
            from brain.gemini import set_active_model

            cur = (config.LLM_MODEL or "").strip()
            ids = [m[0] for m in self._model_cycle_list]
            try:
                nxt = ids[(ids.index(cur) + 1) % len(ids)]
            except ValueError:
                nxt = ids[0]
            set_active_model(nxt)
            self._model_pill_btn.setText(self._current_model_label())

        def _reset_to_chat_hero(self) -> None:
            self._has_messages = False
            self._chat_output.hide()
            self._approve_btn.hide()
            self._hero_widget.show()
            self._animate_to("chat", duration_ms=420)

        def _toggle_pin(self) -> None:
            self.pinned = not self.pinned
            if hasattr(self, "_pin_btn_dash"):
                self._pin_btn_dash.active_state = self.pinned
                self._pin_btn_dash.update()
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
                    reply = fut.result(timeout=180.0)
                    self.reply_ready.emit(query, reply)
                except TimeoutError:
                    self.reply_ready.emit(
                        query,
                        "LM Studio is still generating tokens (took longer than 180s). "
                        "Tip: Use Qwen3-8B (non-VL) or click the green 👁 icon on the left of the input bar to turn off Auto-See Screen.",
                    )
                except Exception as exc:
                    self.reply_ready.emit(query, f"Error: {type(exc).__name__}: {exc}")
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
