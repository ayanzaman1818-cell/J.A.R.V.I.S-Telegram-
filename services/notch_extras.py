"""Modular native Windows backend services for The J.A.R.V.I.S Notch extras.

Provides zero-UI-blocking trackers for:
- Battery & AC charging state (Win32 GetSystemPowerStatus)
- Live Mic & Camera privacy indicators (Win32 CapabilityAccessManager registry)
- Active Downloads watcher (.crdownload / .part / .tmp + completion checkmark flash)
- Live Weather (Open-Meteo, 30-min cache, zero API key)
- Synced Live Lyrics (lrclib.net, free, zero API key)
- Audio Output Device listing & 1-click default switching (CoreAudio COM + PowerShell fallback)
- Display Brightness reading (WMI / laptop display)
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("jarvis.notch_extras")


# ---------------------------------------------------------------------------
# 1. Battery & Charging Status (Win32 GetSystemPowerStatus — < 0.1ms)
# ---------------------------------------------------------------------------

class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_byte),
        ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_byte),
        ("SystemStatusFlag", ctypes.c_byte),
        ("BatteryLifeTime", wt.DWORD),
        ("BatteryFullLifeTime", wt.DWORD),
    ]


def get_battery_status() -> dict[str, Any]:
    """Read Windows battery level & charging state in <0.1ms via Win32 GetSystemPowerStatus."""
    if os.name != "nt":
        return {"present": False, "percent": 100, "charging": False, "ac_online": True, "secs_left": -1}
    try:
        sps = _SYSTEM_POWER_STATUS()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(sps)):
            # BatteryFlag 128 = no system battery (desktop PC)
            no_batt = bool(sps.BatteryFlag & 128) or (sps.BatteryLifePercent == 255)
            ac_online = sps.ACLineStatus == 1
            charging = bool(sps.BatteryFlag & 8) or (ac_online and not no_batt and sps.BatteryLifePercent < 100)
            pct = 100 if no_batt else max(0, min(100, int(sps.BatteryLifePercent)))
            secs = -1 if sps.BatteryLifeTime == 0xFFFFFFFF else int(sps.BatteryLifeTime)
            return {
                "present": not no_batt,
                "percent": pct,
                "charging": charging,
                "ac_online": ac_online,
                "secs_left": secs,
            }
    except Exception:
        pass
    return {"present": False, "percent": 100, "charging": False, "ac_online": True, "secs_left": -1}


# ---------------------------------------------------------------------------
# 2. Live Mic & Camera Privacy Indicators (Hardware CoreAudio + Win32 FrameServer + ConsentStore)
# ---------------------------------------------------------------------------

class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_str(cls, s: str) -> "_GUID":
        import uuid

        u = uuid.UUID(s)
        return cls(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))


class _SERVICE_STATUS_PROCESS(ctypes.Structure):
    _fields_ = [
        ("dwServiceType", wt.DWORD),
        ("dwCurrentState", wt.DWORD),
        ("dwControlsAccepted", wt.DWORD),
        ("dwWin32ExitCode", wt.DWORD),
        ("dwServiceSpecificExitCode", wt.DWORD),
        ("dwCheckPoint", wt.DWORD),
        ("dwWaitHint", wt.DWORD),
        ("dwProcessId", wt.DWORD),
        ("dwServiceFlags", wt.DWORD),
    ]


def _is_coreaudio_mic_active() -> bool:
    """Hardware-level (<0.5ms) CoreAudio eCapture IAudioSessionManager2 check for ANY active microphone stream."""
    if os.name != "nt":
        return False
    try:
        from ctypes import POINTER, byref, c_int, c_uint, c_void_p, HRESULT

        ole32 = ctypes.windll.ole32
        ole32.CoInitialize(None)
        clsid = _GUID.from_str("BCDE0395-E52F-467C-8E3D-C4579291692E")
        iid_enum = _GUID.from_str("A95664D2-9614-4F35-A746-DE8DB63617E6")
        iid_sm2 = _GUID.from_str("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F")

        enum_p = c_void_p()
        if ole32.CoCreateInstance(byref(clsid), None, 23, byref(iid_enum), byref(enum_p)) != 0 or not enum_p:
            return False
        vt = ctypes.cast(ctypes.cast(enum_p, POINTER(c_void_p))[0], POINTER(c_void_p))
        enum_ep = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_int, c_uint, POINTER(c_void_p))(vt[3])
        release_enum = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(vt[2])

        col_p = c_void_p()
        if enum_ep(enum_p, 1, 1, byref(col_p)) != 0 or not col_p:  # eCapture=1, DEVICE_STATE_ACTIVE=1
            release_enum(enum_p)
            return False

        cvt = ctypes.cast(ctypes.cast(col_p, POINTER(c_void_p))[0], POINTER(c_void_p))
        get_cnt = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_uint))(cvt[3])
        get_item = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_uint, POINTER(c_void_p))(cvt[4])
        release_col = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(cvt[2])

        cnt = c_uint(0)
        get_cnt(col_p, byref(cnt))
        found_active = False

        for i in range(cnt.value):
            dev_p = c_void_p()
            if get_item(col_p, i, byref(dev_p)) != 0 or not dev_p:
                continue
            dvt = ctypes.cast(ctypes.cast(dev_p, POINTER(c_void_p))[0], POINTER(c_void_p))
            act = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(_GUID), c_uint, c_void_p, POINTER(c_void_p))(dvt[3])
            rel_dev = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(dvt[2])

            sm_p = c_void_p()
            if act(dev_p, byref(iid_sm2), 23, None, byref(sm_p)) == 0 and sm_p:
                svt = ctypes.cast(ctypes.cast(sm_p, POINTER(c_void_p))[0], POINTER(c_void_p))
                get_se = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_void_p))(svt[5])
                rel_sm = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(svt[2])

                se_p = c_void_p()
                if get_se(sm_p, byref(se_p)) == 0 and se_p:
                    sevt = ctypes.cast(ctypes.cast(se_p, POINTER(c_void_p))[0], POINTER(c_void_p))
                    scnt_fn = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_int))(sevt[3])
                    sget_fn = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_int, POINTER(c_void_p))(sevt[4])
                    rel_se = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(sevt[2])

                    sc = c_int(0)
                    scnt_fn(se_p, byref(sc))
                    for j in range(sc.value):
                        ctrl_p = c_void_p()
                        if sget_fn(se_p, j, byref(ctrl_p)) == 0 and ctrl_p:
                            ctvt = ctypes.cast(ctypes.cast(ctrl_p, POINTER(c_void_p))[0], POINTER(c_void_p))
                            gstate = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(c_int))(ctvt[3])
                            rel_c = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(ctvt[2])
                            st = c_int(0)
                            if gstate(ctrl_p, byref(st)) == 0 and st.value == 1:  # AudioSessionStateActive == 1
                                found_active = True
                            rel_c(ctrl_p)
                        if found_active:
                            break
                    rel_se(se_p)
                rel_sm(sm_p)
            rel_dev(dev_p)
            if found_active:
                break

        release_col(col_p)
        release_enum(enum_p)
        return found_active
    except Exception:
        return False


def _get_frameserver_state_and_pid() -> tuple[bool, int]:
    """Hardware-level (<0.1ms) Win32 SCM query returning (is_running, pid) for Windows Camera FrameServer."""
    if os.name != "nt":
        return False, 0
    try:
        adv = ctypes.windll.advapi32
        adv.OpenSCManagerW.argtypes = [wt.LPCWSTR, wt.LPCWSTR, wt.DWORD]
        adv.OpenSCManagerW.restype = wt.HANDLE
        adv.OpenServiceW.argtypes = [wt.HANDLE, wt.LPCWSTR, wt.DWORD]
        adv.OpenServiceW.restype = wt.HANDLE
        adv.QueryServiceStatusEx.argtypes = [
            wt.HANDLE, wt.DWORD, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
        ]
        adv.QueryServiceStatusEx.restype = wt.BOOL
        adv.CloseServiceHandle.argtypes = [wt.HANDLE]
        adv.CloseServiceHandle.restype = wt.BOOL
        scm = adv.OpenSCManagerW(None, None, 0x0001)  # SC_MANAGER_CONNECT
        if not scm:
            return False, 0
        try:
            svc = adv.OpenServiceW(scm, "FrameServer", 0x0004)  # SERVICE_QUERY_STATUS
            if not svc:
                return False, 0
            try:
                ssp = _SERVICE_STATUS_PROCESS()
                needed = wt.DWORD(0)
                if adv.QueryServiceStatusEx(svc, 0, ctypes.byref(ssp), ctypes.sizeof(ssp), ctypes.byref(needed)):
                    return int(ssp.dwCurrentState) == 4, int(ssp.dwProcessId)
            finally:
                adv.CloseServiceHandle(svc)
        finally:
            adv.CloseServiceHandle(scm)
    except Exception:
        pass
    return False, 0


def _get_process_cpu_ticks_nt(target_pid: int) -> int:
    """Read KernelTime + UserTime (100ns ticks) for ANY PID (including LocalService) via NtQuerySystemInformation (<0.4ms)."""
    if os.name != "nt" or target_pid <= 0:
        return -1
    try:
        import struct

        ntdll = ctypes.windll.ntdll
        buf_size = 512 * 1024
        for _ in range(4):
            buf = ctypes.create_string_buffer(buf_size)
            ret_len = ctypes.c_ulong(0)
            st = ntdll.NtQuerySystemInformation(5, buf, buf_size, ctypes.byref(ret_len))
            if st == 0:
                raw = buf.raw
                off = 0
                while True:
                    next_off = struct.unpack_from("<I", raw, off)[0]
                    pid = struct.unpack_from("<Q", raw, off + 80)[0]
                    if pid == target_pid:
                        kt, ut = struct.unpack_from("<qq", raw, off + 40)
                        return int(kt + ut)
                    if next_off == 0:
                        break
                    off += next_off
                return -1
            buf_size = max(buf_size * 2, ret_len.value + 65536)
    except Exception:
        pass
    return -1


def _is_frameserver_webcam_active() -> bool:
    running, _pid = _get_frameserver_state_and_pid()
    return running


def _scan_consent_store(device_name: str) -> tuple[bool, str]:
    """Fallback ConsentStore scan for UWP apps when Windows privacy activity logging is enabled."""
    if os.name != "nt":
        return False, ""
    try:
        import winreg

        base = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\{device_name}"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as root:
            def _check_subkey(parent_key: Any, sub_name: str) -> tuple[bool, str]:
                try:
                    with winreg.OpenKey(parent_key, sub_name, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as sk:
                        try:
                            start_val, _ = winreg.QueryValueEx(sk, "LastUsedTimeStart")
                            stop_val, _ = winreg.QueryValueEx(sk, "LastUsedTimeStop")
                            if int(start_val) > 0 and int(stop_val) == 0:
                                clean = sub_name.split("#")[-1].replace(".exe", "").strip()
                                if "_" in clean:
                                    clean = clean.split("_")[0]
                                return True, clean or "App"
                        except FileNotFoundError:
                            pass
                except OSError:
                    pass
                return False, ""

            idx = 0
            while True:
                try:
                    name = winreg.EnumKey(root, idx)
                    idx += 1
                except OSError:
                    break
                if name == "NonPackaged":
                    try:
                        with winreg.OpenKey(root, "NonPackaged", 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as np_key:
                            n_idx = 0
                            while True:
                                try:
                                    np_name = winreg.EnumKey(np_key, n_idx)
                                    n_idx += 1
                                except OSError:
                                    break
                                active, app = _check_subkey(np_key, np_name)
                                if active:
                                    return True, app
                    except OSError:
                        pass
                else:
                    active, app = _check_subkey(root, name)
                    if active:
                        return True, app
    except Exception:
        pass
    return False, ""


class PrivacyIndicatorTracker:
    """Instant (<0.25s) hardware CoreAudio mic + FrameServer live-stream CPU-delta webcam tracker."""

    def __init__(self) -> None:
        self._last_ts: float = 0.0
        self._fs_prev_pid: int = 0
        self._fs_prev_ticks: int = -1
        self._fs_idle_strikes: int = 0
        self._state: dict[str, Any] = {
            "mic_active": False,
            "cam_active": False,
            "mic_app": "",
            "cam_app": "",
        }

    def get_status(self) -> dict[str, Any]:
        now = time.time()
        if now - self._last_ts >= 0.25:
            self._last_ts = now
            mic_cs, mic_app = _scan_consent_store("microphone")
            cam_cs, cam_app = _scan_consent_store("webcam")
            mic_hw = _is_coreaudio_mic_active()

            # Check if FrameServer is running AND actively streaming frames (turns off in ~0.5s when camera closes!)
            fs_running, fs_pid = _get_frameserver_state_and_pid()
            cam_hw = False
            if fs_running and fs_pid > 0:
                ticks = _get_process_cpu_ticks_nt(fs_pid)
                if fs_pid != self._fs_prev_pid or self._fs_prev_ticks < 0:
                    # Brand-new FrameServer process just spawned -> camera just turned on!
                    self._fs_prev_pid = fs_pid
                    self._fs_prev_ticks = ticks
                    self._fs_idle_strikes = 0
                    cam_hw = True
                elif ticks > self._fs_prev_ticks:
                    # FrameServer actively burning CPU copying live video frames!
                    self._fs_prev_ticks = ticks
                    self._fs_idle_strikes = 0
                    cam_hw = True
                else:
                    # FrameServer is idle/lingering after camera closed -> turn off indicator after 2 idle ticks (~0.5s)!
                    self._fs_idle_strikes += 1
                    cam_hw = self._fs_idle_strikes < 2
            else:
                self._fs_prev_pid = 0
                self._fs_prev_ticks = -1
                self._fs_idle_strikes = 0

            self._state = {
                "mic_active": bool(mic_hw or mic_cs),
                "cam_active": bool(cam_hw or cam_cs),
                "mic_app": mic_app or ("Microphone" if mic_hw else ""),
                "cam_app": cam_app or ("Webcam" if cam_hw else ""),
            }
        return self._state


# ---------------------------------------------------------------------------
# 3. Live Downloads Watcher (~/Downloads active .crdownload / .part / .tmp)
# ---------------------------------------------------------------------------

class DownloadsWatcher:
    """Watches ~/Downloads for active browser downloads and flashes completion for 3.8s."""

    _PARTIAL_EXTS = (".crdownload", ".part", ".opdownload", ".tmp", ".download")

    def __init__(self) -> None:
        self._dl_dir = Path.home() / "Downloads"
        self._last_scan: float = 0.0
        self._active_file: str = ""
        self._prev_size: int = 0
        self._prev_ts: float = 0.0
        self._speed_bps: float = 0.0
        self._size_bytes: int = 0
        self._completed_name: str = ""
        self._completed_until: float = 0.0

    def get_status(self) -> dict[str, Any]:
        now = time.time()
        if now - self._last_scan >= 1.0:
            self._last_scan = now
            self._scan(now)

        if self._active_file:
            mb = self._size_bytes / (1024.0 * 1024.0)
            spd_mb = self._speed_bps / (1024.0 * 1024.0)
            detail = f"{mb:.1f} MB" if spd_mb < 0.05 else f"{mb:.1f} MB · {spd_mb:.1f}/s"
            return {
                "state": "downloading",
                "name": self._active_file,
                "detail": detail,
                "bytes": self._size_bytes,
            }
        if now < self._completed_until and self._completed_name:
            return {
                "state": "done",
                "name": self._completed_name,
                "detail": "Downloaded",
                "bytes": 0,
            }
        return {"state": "idle", "name": "", "detail": "", "bytes": 0}

    def _scan(self, now: float) -> None:
        if not self._dl_dir.exists():
            return
        try:
            newest_path: Path | None = None
            newest_mtime = 0.0
            for entry in os.scandir(self._dl_dir):
                if not entry.is_file():
                    continue
                low = entry.name.lower()
                if any(low.endswith(ext) for ext in self._PARTIAL_EXTS):
                    st = entry.stat()
                    # Ignore stale abandoned partial files older than 3 minutes
                    if now - st.st_mtime <= 180.0 and st.st_mtime > newest_mtime:
                        newest_mtime = st.st_mtime
                        newest_path = Path(entry.path)

            if newest_path is not None:
                st = newest_path.stat()
                clean_name = newest_path.name
                for ext in self._PARTIAL_EXTS:
                    if clean_name.lower().endswith(ext):
                        clean_name = clean_name[: -len(ext)]
                        break
                if clean_name == self._active_file and self._prev_ts > 0:
                    dt = max(0.1, now - self._prev_ts)
                    delta = max(0, st.st_size - self._prev_size)
                    self._speed_bps = self._speed_bps * 0.4 + (delta / dt) * 0.6
                else:
                    self._speed_bps = 0.0
                self._active_file = clean_name or "Downloading…"
                self._size_bytes = st.st_size
                self._prev_size = st.st_size
                self._prev_ts = now
            else:
                if self._active_file:
                    # Previously active download just disappeared -> finished!
                    self._completed_name = self._active_file
                    self._completed_until = now + 3.8
                self._active_file = ""
                self._size_bytes = 0
                self._speed_bps = 0.0
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 4. Live Weather Tracker (Open-Meteo, 30-min cache, zero API key)
# ---------------------------------------------------------------------------

class WeatherTracker:
    """Fetches current temperature & weather condition for config.LOCATION in the background."""

    def __init__(self, location: str = "Dhaka") -> None:
        self._location = (location or "Dhaka").strip()
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {"ok": False, "temp_c": 0, "condition": "clear", "label": ""}
        self._last_ts: float = 0.0
        self._fetching: bool = False
        self.trigger_refresh_async()

    def get_weather(self) -> dict[str, Any]:
        if time.time() - self._last_ts > 1800.0 and not self._fetching:
            self.trigger_refresh_async()
        with self._lock:
            return dict(self._data)

    def trigger_refresh_async(self) -> None:
        if self._fetching:
            return
        self._fetching = True
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        try:
            import httpx
            from urllib.parse import quote

            with httpx.Client(timeout=6.0) as client:
                geo = client.get(
                    f"https://geocoding-api.open-meteo.com/v1/search?name={quote(self._location)}&count=1&language=en&format=json"
                )
                lat, lon = 23.8103, 90.4125  # Default Dhaka fallback
                if geo.status_code == 200:
                    res = (geo.json() or {}).get("results") or []
                    if res:
                        lat = float(res[0].get("latitude", lat))
                        lon = float(res[0].get("longitude", lon))

                w = client.get(
                    f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
                    "&current=temperature_2m,weather_code,is_day"
                )
                if w.status_code == 200:
                    cur = (w.json() or {}).get("current") or {}
                    temp_c = int(round(float(cur.get("temperature_2m", 0))))
                    code = int(cur.get("weather_code", 0))
                    is_day = bool(cur.get("is_day", 1))
                    cond = self._map_wmo(code, is_day)
                    with self._lock:
                        self._data = {
                            "ok": True,
                            "temp_c": temp_c,
                            "condition": cond,
                            "label": f"{temp_c}°",
                        }
                        self._last_ts = time.time()
        except Exception:
            log.debug("Weather refresh failed", exc_info=True)
        finally:
            self._fetching = False

    @staticmethod
    def _map_wmo(code: int, is_day: bool) -> str:
        if code in (0, 1):
            return "clear_day" if is_day else "clear_night"
        if code in (2, 3, 45, 48):
            return "cloudy"
        if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
            return "rain"
        if code in (71, 73, 75, 77, 85, 86):
            return "snow"
        if code in (95, 96, 99):
            return "storm"
        return "cloudy"


# ---------------------------------------------------------------------------
# 5. Synced Live Lyrics Tracker (lrclib.net — Free, Zero API Key)
# ---------------------------------------------------------------------------

class SyncedLyricsTracker:
    """Fetches timestamped LRC lyrics from lrclib.net and returns the active line for any song position."""

    _LRC_LINE_RE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)")
    _LRC_OFFSET_RE = re.compile(r"\[offset:\s*([+-]?\d+)\]", re.IGNORECASE)
    _LRC_WORD_TAG_RE = re.compile(r"<(\d+):(\d+(?:\.\d+)?)>")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, Any]] = {}
        self._fetching_key: str = ""

    def request_track(self, artist: str, title: str, duration_sec: float = 0.0) -> None:
        key = f"{artist.strip().lower()}||{title.strip().lower()}"
        if not title.strip() or key in self._cache or self._fetching_key == key:
            return
        self._fetching_key = key
        threading.Thread(
            target=self._fetch_worker,
            args=(key, artist.strip(), title.strip(), float(duration_sec)),
            daemon=True,
        ).start()

    @staticmethod
    def _norm_meta(s: str) -> str:
        s = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", (s or "").lower())
        s = re.sub(r"\b(feat|ft|featuring|remaster(?:ed)?|radio edit|explicit|clean|version)\b.*", " ", s)
        s = re.sub(r"[^a-z0-9\s]", " ", s)
        return " ".join(s.split())

    @classmethod
    def _candidate_matches(cls, item: dict[str, Any], want_artist: str, want_title: str) -> bool:
        """Strictly verify an lrclib result actually matches BOTH the song title and artist (never show wrong song!)."""
        c_title = cls._norm_meta(str(item.get("trackName") or ""))
        c_artist = cls._norm_meta(str(item.get("artistName") or ""))
        w_title = cls._norm_meta(want_title)
        w_artist = cls._norm_meta(want_artist)
        if not c_title or not w_title:
            return False
        # Title must match closely (exact, substring, or >=75% word overlap)
        t_words = set(w_title.split())
        ct_words = set(c_title.split())
        t_overlap = len(t_words & ct_words) / max(1, len(t_words))
        title_ok = (w_title == c_title) or (w_title in c_title) or (c_title in w_title) or (t_overlap >= 0.75)
        if not title_ok:
            return False
        # If artist is known, artist MUST also match (at least one primary artist token matches)
        if w_artist and c_artist:
            a_words = {w for w in w_artist.split() if len(w) >= 2}
            ca_words = {w for w in c_artist.split() if len(w) >= 2}
            artist_ok = (w_artist in c_artist) or (c_artist in w_artist) or bool(a_words & ca_words)
            if not artist_ok:
                return False
        return True

    def _fetch_worker(self, key: str, artist: str, title: str, duration_sec: float = 0.0) -> None:
        result: dict[str, Any] = {"status": "none", "synced": [], "plain": []}
        try:
            import httpx
            from urllib.parse import quote

            clean_title = re.sub(r"\s*[\(\[].*?[\)\]]", "", title).strip() or title
            primary_artist = artist.split(",")[0].strip() if "," in artist else artist
            with httpx.Client(timeout=5.0, headers={"User-Agent": "JARVIS-Notch/2.7"}) as client:
                item = None
                # 1. Exact artist + track lookup (do NOT pass strict duration first, as ±3s causes 404!)
                if primary_artist:
                    r = client.get(
                        f"https://lrclib.net/api/get?artist_name={quote(primary_artist)}&track_name={quote(clean_title)}"
                    )
                    if r.status_code == 200:
                        cand = r.json()
                        if cand and (cand.get("syncedLyrics") or cand.get("plainLyrics")) and self._candidate_matches(cand, primary_artist, clean_title):
                            item = cand

                # 2. Structured search fallback with STRICT artist + title verification
                if not item:
                    if primary_artist:
                        s_url = f"https://lrclib.net/api/search?track_name={quote(clean_title)}&artist_name={quote(primary_artist)}"
                    else:
                        s_url = f"https://lrclib.net/api/search?track_name={quote(clean_title)}"
                    r2 = client.get(s_url)
                    if r2.status_code == 200:
                        arr = r2.json() or []
                        valid = [x for x in arr if self._candidate_matches(x, primary_artist, clean_title)]
                        synced_valid = [x for x in valid if x.get("syncedLyrics")]
                        pool = synced_valid or [x for x in valid if x.get("plainLyrics")]
                        if pool:
                            if duration_sec > 10.0:
                                pool.sort(key=lambda x: abs(float(x.get("duration") or 0.0) - duration_sec))
                            item = pool[0]

                if item:
                    synced_raw = str(item.get("syncedLyrics") or "").strip()
                    plain_raw = str(item.get("plainLyrics") or "").strip()
                    synced_lines: list[tuple[float, str]] = []
                    lrc_offset_sec = 0.0
                    if synced_raw:
                        m_off = self._LRC_OFFSET_RE.search(synced_raw)
                        if m_off:
                            try:
                                # Standard LRC [offset:+ms] shifts lyrics earlier by ms/1000
                                lrc_offset_sec = int(m_off.group(1)) / 1000.0
                            except ValueError:
                                lrc_offset_sec = 0.0
                        for ln in synced_raw.splitlines():
                            m = self._LRC_LINE_RE.match(ln.strip())
                            if m:
                                ts = max(0.0, int(m.group(1)) * 60.0 + float(m.group(2)) - lrc_offset_sec)
                                # Strip any inline <mm:ss.xx> tags cleanly for display
                                txt = self._LRC_WORD_TAG_RE.sub("", m.group(3)).strip()
                                synced_lines.append((ts, txt))
                        synced_lines.sort(key=lambda x: x[0])
                    if synced_lines:
                        result = {"status": "synced", "synced": synced_lines, "plain": []}
                    elif plain_raw:
                        pl = [l.strip() for l in plain_raw.splitlines() if l.strip()]
                        result = {"status": "plain", "synced": [], "plain": pl}
        except Exception:
            log.debug("Lyrics fetch failed for %s - %s", artist, title, exc_info=True)
        finally:
            with self._lock:
                self._cache[key] = result
                if self._fetching_key == key:
                    self._fetching_key = ""

    def get_lines_at(
        self, artist: str, title: str, pos_sec: float, duration_sec: float = 0.0, offset_sec: float = 0.0
    ) -> tuple[str, str, str, float]:
        """Return (status ['loading'|'synced'|'plain'|'none'], active_line, next_line, word_progress_0_to_1)."""
        key = f"{artist.strip().lower()}||{title.strip().lower()}"
        with self._lock:
            entry = self._cache.get(key)
            fetching = self._fetching_key == key
        if entry is None:
            if not fetching:
                self.request_track(artist, title, duration_sec=duration_sec)
            return "loading", "Searching synced lyrics…", "", 0.0

        status = entry["status"]
        if status == "synced":
            lines: list[tuple[float, str]] = entry["synced"]
            # +0.55s lead-in compensates for Spotify Desktop Windows GSMTC latency so each line hits right on the beat!
            eff_pos = pos_sec + offset_sec + 0.55
            idx = -1
            for i, (ts, _txt) in enumerate(lines):
                if ts <= eff_pos:
                    idx = i
                else:
                    break
            if 0 <= idx < len(lines):
                cur_ts, cur = lines[idx]
                if idx + 1 < len(lines):
                    nxt_ts, nxt = lines[idx + 1]
                    raw_span = max(0.5, nxt_ts - cur_ts)
                    # Follow the song's REAL tempo (90% of the actual gap to the next line, only capping huge >7.5s instrumental breaks)
                    vocal_span = min(raw_span * 0.90, 7.5)
                else:
                    nxt = ""
                    vocal_span = 4.0
                prog = max(0.0, min(1.0, (eff_pos - cur_ts) / max(0.35, vocal_span)))
                return "synced", cur or "♪  •  ♪", nxt, prog
            first = lines[0][1] if lines else ""
            return "synced", "♪  •  ♪", first, 0.0
        if status == "plain":
            pl: list[str] = entry["plain"]
            return "plain", pl[0] if pl else "", pl[1] if len(pl) > 1 else "", 1.0
        return "none", "No synced lyrics found", "", 0.0


# ---------------------------------------------------------------------------
# 6. Windows Audio Output Device Switcher (CoreAudio COM via PowerShell / C#)
# ---------------------------------------------------------------------------

_PS_AUDIO_DEVICES_CS = r"""
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public static class AudioSwitchHelper {
    [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
    private class MMDeviceEnumeratorComObject {}

    [Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IMMDeviceEnumerator {
        int EnumAudioEndpoints(int dataFlow, int dwStateMask, out IMMDeviceCollection ppDevices);
        int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice ppEndpoint);
    }

    [Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IMMDeviceCollection {
        int GetCount(out uint pcDevices);
        int Item(uint nDevice, out IMMDevice ppDevice);
    }

    [Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IMMDevice {
        int Activate(ref Guid iid, int dwClsCtx, IntPtr pActivationParams, [MarshalAs(UnmanagedType.IUnknown)] out object ppInterface);
        int OpenPropertyStore(int stgmAccess, out IPropertyStore ppProperties);
        int GetId([MarshalAs(UnmanagedType.LPWStr)] out string ppstrId);
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PROPERTYKEY {
        public Guid fmtid;
        public uint pid;
    }

    [StructLayout(LayoutKind.Explicit)]
    private struct PROPVARIANT {
        [FieldOffset(0)] public ushort vt;
        [FieldOffset(8)] public IntPtr pwszVal;
    }

    [Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IPropertyStore {
        int GetCount(out uint cProps);
        int GetAt(uint iProp, out PROPERTYKEY pkey);
        int GetValue(ref PROPERTYKEY key, out PROPVARIANT pv);
    }

    [ComImport, Guid("870af99c-171d-4f9e-af0d-e63df40c2bc9")]
    private class PolicyConfigClient {}

    [Guid("f8679f50-850a-41cf-9c72-430f290290c8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IPolicyConfig {
        int GetMixFormat(string pszDeviceName, IntPtr ppFormat);
        int GetDeviceFormat(string pszDeviceName, int bDefault, IntPtr ppFormat);
        int ResetDeviceFormat(string pszDeviceName);
        int SetDeviceFormat(string pszDeviceName, IntPtr pEndpointFormat, IntPtr MixFormat);
        int GetProcessingPeriod(string pszDeviceName, int bDefault, IntPtr pmftDefaultPeriod, IntPtr pmftMinimumPeriod);
        int SetProcessingPeriod(string pszDeviceName, IntPtr pmftPeriod);
        int GetShareMode(string pszDeviceName, IntPtr pMode);
        int SetShareMode(string pszDeviceName, IntPtr mode);
        int GetPropertyValue(string pszDeviceName, ref PROPERTYKEY key, IntPtr pv);
        int SetPropertyValue(string pszDeviceName, ref PROPERTYKEY key, IntPtr pv);
        int SetDefaultEndpoint([MarshalAs(UnmanagedType.LPWStr)] string wszDeviceId, int role);
        int SetEndpointVisibility(string pszDeviceName, int bVisible);
    }

    public static string ListDevices() {
        var enumerator = (IMMDeviceEnumerator)new MMDeviceEnumeratorComObject();
        IMMDevice defDev;
        string defId = "";
        if (enumerator.GetDefaultAudioEndpoint(0, 0, out defDev) == 0 && defDev != null) {
            defDev.GetId(out defId);
        }
        IMMDeviceCollection col;
        enumerator.EnumAudioEndpoints(0, 1, out col);
        uint count;
        col.GetCount(out count);
        var pkey = new PROPERTYKEY { fmtid = new Guid("a45c254e-df1c-4efd-8020-67d146a850e0"), pid = 14 };
        var lines = new List<string>();
        for (uint i = 0; i < count; i++) {
            IMMDevice dev;
            col.Item(i, out dev);
            string id;
            dev.GetId(out id);
            IPropertyStore store;
            dev.OpenPropertyStore(0, out store);
            PROPVARIANT pv;
            store.GetValue(ref pkey, out pv);
            string name = (pv.vt == 31 && pv.pwszVal != IntPtr.Zero) ? Marshal.PtrToStringUni(pv.pwszVal) : id;
            lines.Add((id == defId ? "1" : "0") + "|" + id + "|" + name);
        }
        return string.Join("\n", lines);
    }

    public static void SetDefault(string deviceId) {
        var pc = (IPolicyConfig)new PolicyConfigClient();
        pc.SetDefaultEndpoint(deviceId, 0); // eConsole
        pc.SetDefaultEndpoint(deviceId, 1); // eMultimedia
        pc.SetDefaultEndpoint(deviceId, 2); // eCommunications
    }
}
"""


def list_audio_output_devices_sync() -> list[dict[str, Any]]:
    """List active Windows audio playback devices (Speakers, Headphones, Bluetooth) with default flag."""
    if os.name != "nt":
        return []
    import base64

    ps = f"Add-Type -TypeDefinition @'\n{_PS_AUDIO_DEVICES_CS}\n'@; [AudioSwitchHelper]::ListDevices()"
    enc = base64.b64encode(ps.encode("utf-16le")).decode("ascii")
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-EncodedCommand", enc],
            capture_output=True,
            text=True,
            timeout=6,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        devices: list[dict[str, Any]] = []
        for ln in (res.stdout or "").splitlines():
            parts = ln.strip().split("|", 2)
            if len(parts) == 3:
                devices.append({
                    "default": parts[0] == "1",
                    "id": parts[1],
                    "name": parts[2],
                })
        return devices
    except Exception:
        log.debug("Failed to list Windows audio outputs", exc_info=True)
        return []


def set_default_audio_output_sync(device_id: str) -> bool:
    """Switch Windows default audio output device immediately via IPolicyConfig."""
    if os.name != "nt" or not device_id:
        return False
    import base64

    safe_id = device_id.replace("'", "''")
    ps = f"Add-Type -TypeDefinition @'\n{_PS_AUDIO_DEVICES_CS}\n'@; [AudioSwitchHelper]::SetDefault('{safe_id}')"
    enc = base64.b64encode(ps.encode("utf-16le")).decode("ascii")
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-EncodedCommand", enc],
            capture_output=True,
            text=True,
            timeout=6,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return res.returncode == 0
    except Exception:
        log.debug("Failed to switch default audio output", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# 7. Display Brightness Reader (WMI laptop displays — non-blocking)
# ---------------------------------------------------------------------------

def get_display_brightness_sync() -> int | None:
    """Read current Windows display brightness (0..100) if supported (laptops/internal panels)."""
    if os.name != "nt":
        return None
    try:
        res = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-WindowStyle",
                "Hidden",
                "-Command",
                "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness -ErrorAction SilentlyContinue | Select-Object -First 1).CurrentBrightness",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        txt = (res.stdout or "").strip()
        if txt.isdigit():
            return max(0, min(100, int(txt)))
    except Exception:
        pass
    return None
