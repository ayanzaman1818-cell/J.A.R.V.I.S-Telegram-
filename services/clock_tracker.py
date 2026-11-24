"""Zero-dependency Windows Clock (Microsoft.WindowsAlarms) live Timer & Stopwatch tracker.

Uses Windows UI Automation (UIAutomationClient) in a non-blocking background thread
when the Windows Clock app is open, and smoothly interpolates countdown / elapsed
seconds at 60fps on the UI thread.
"""

from __future__ import annotations

import base64
import ctypes
import logging
import os
import re
import subprocess
import threading
import time
from typing import Any

log = logging.getLogger("jarvis.clock_tracker")

_PS_CLOCK_UIA_SCRIPT = r"""
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, 'Clock')
$win = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $cond)
if (-not $win) {
    Write-Output "NONE"
    exit 0
}
$all = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
$timerTime = ""
$timerRunning = $false
$swTime = ""
$swRunning = $false
$fallbackTimes = New-Object System.Collections.Generic.List[string]

foreach ($el in $all) {
    try {
        $aid = [string]$el.Current.AutomationId
        $nm = [string]$el.Current.Name
        $ct = $el.Current.ControlType.ProgrammaticName

        # Detect active Timer pause button or running state
        if (($aid -match 'Timer.*Pause|Pause.*Timer|PlayPauseButton') -and ($nm -match 'Pause')) {
            $timerRunning = $true
        }
        # Detect active Stopwatch pause button
        if (($aid -match 'Stopwatch.*Pause|StopwatchPlayPause') -and ($nm -match 'Pause')) {
            $swRunning = $true
        }
        # Generic Pause button fallback (disambiguated by page text below)
        if ($ct -eq 'ControlType.Button' -and $nm -match '^Pause') {
            if ($aid -match 'Stopwatch') {
                $swRunning = $true
            } elseif ($aid -match 'Timer') {
                $timerRunning = $true
            } else {
                $hasGenericPause = $true
            }
        }

        # Extract Stopwatch time (usually HH:MM:SS.cc or MM:SS.cc or StopwatchTimerText)
        if ($aid -match 'Stopwatch' -and $nm -match '(\d{1,2}:\d{2}:\d{2}(?:\.\d+)?|\d{1,2}:\d{2}\.\d+)') {
            $swTime = $Matches[1]
        } elseif ($nm -match '(\d{1,2}:\d{2}:\d{2}\.\d{1,2})') {
            $swTime = $Matches[1]
        }

        # Extract Timer remaining time (HH:MM:SS or MM:SS)
        if ($aid -match 'Timer' -and $nm -match '(\d{1,2}:\d{2}:\d{2}|\d{1,2}:\d{2})') {
            if (-not $timerTime) { $timerTime = $Matches[1] }
        } elseif ($nm -match '(\d{1,2}:\d{2}:\d{2})' -and $aid -notmatch 'Clock|World|Stopwatch') {
            $fallbackTimes.Add($Matches[1])
        }
    } catch {}
}

# Disambiguate if a generic Pause button is visible on the active tab
if ($hasGenericPause -and -not $timerRunning -and -not $swRunning) {
    if ($swTime) {
        $swRunning = $true
    } elseif ($timerTime -or $fallbackTimes.Count -gt 0) {
        $timerRunning = $true
    }
}

if (-not $timerTime -and $fallbackTimes.Count -gt 0) {
    $timerTime = $fallbackTimes[0]
}

if ($timerRunning -and $timerTime) {
    Write-Output ("MODE=timer|RUNNING=1|TIME=" + $timerTime)
} elseif ($swRunning -and $swTime) {
    Write-Output ("MODE=stopwatch|RUNNING=1|TIME=" + $swTime)
} else {
    Write-Output "IDLE"
}
""".strip()

_ENCODED_CLOCK_PS = base64.b64encode(_PS_CLOCK_UIA_SCRIPT.encode("utf-16le")).decode("ascii")


def _parse_hms_to_seconds(text: str) -> float:
    """Parse 'HH:MM:SS', 'MM:SS', or 'HH:MM:SS.cc' into seconds."""
    clean = (text or "").strip()
    if not clean:
        return 0.0
    parts = clean.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600.0 + int(parts[1]) * 60.0 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60.0 + float(parts[1])
    except ValueError:
        pass
    return 0.0


def _is_windows_clock_window_open() -> bool:
    """Ultra-fast (<0.1ms) Win32 check to see if the Windows Clock app window exists."""
    if os.name != "nt":
        return False
    try:
        hwnd = ctypes.windll.user32.FindWindowW("ApplicationFrameWindow", "Clock")
        if hwnd:
            return True
    except Exception:
        pass
    return False


def query_windows_clock_sync() -> dict[str, Any]:
    """Query the Windows Clock app via UIAutomation for any active running Timer or Stopwatch."""
    if os.name != "nt" or not _is_windows_clock_window_open():
        return {"active": False, "mode": "", "seconds": 0.0, "ts": time.time()}
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-EncodedCommand", _ENCODED_CLOCK_PS],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (res.stdout or "").strip()
        if "MODE=" in out:
            line = out.splitlines()[-1]
            parts = dict(item.split("=", 1) for item in line.split("|") if "=" in item)
            mode = parts.get("MODE", "").strip().lower()
            running = parts.get("RUNNING", "0").strip() == "1"
            secs = _parse_hms_to_seconds(parts.get("TIME", ""))
            if running and mode in ("timer", "stopwatch") and (secs > 0.0 or mode == "stopwatch"):
                return {"active": True, "mode": mode, "seconds": secs, "ts": time.time()}
    except Exception:
        log.debug("Windows Clock UIA poll failed", exc_info=True)
    return {"active": False, "mode": "", "seconds": 0.0, "ts": time.time()}


class WindowsClockTracker:
    """Maintains a smooth 60fps interpolated Timer / Stopwatch state synced with the Windows Clock app."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: bool = False
        self._mode: str = ""  # "timer" or "stopwatch"
        self._base_seconds: float = 0.0
        self._updated_ts: float = 0.0
        self._last_poll_ts: float = 0.0
        self._polling: bool = False

    def trigger_poll_async(self) -> None:
        """Non-blocking background poll (checks window presence in <0.1ms; queries UIA every ~1.8s when open)."""
        now = time.time()
        with self._lock:
            if self._polling:
                return
            interval = 1.8 if (self._active or _is_windows_clock_window_open()) else 2.5
            if now - self._last_poll_ts < interval:
                return
            self._polling = True
            self._last_poll_ts = now

        def _worker() -> None:
            try:
                data = query_windows_clock_sync()
                with self._lock:
                    self._active = bool(data["active"])
                    self._mode = str(data["mode"])
                    self._base_seconds = float(data["seconds"])
                    self._updated_ts = float(data["ts"])
            finally:
                with self._lock:
                    self._polling = False

        threading.Thread(target=_worker, daemon=True).start()

    def current_state(self) -> tuple[bool, str, str]:
        """Return (is_active, mode ['timer'|'stopwatch'|''], formatted_time) at 60fps without blocking."""
        with self._lock:
            active = self._active
            mode = self._mode
            base_sec = self._base_seconds
            updated = self._updated_ts

        if not active or not mode:
            return False, "", ""

        elapsed = max(0.0, time.time() - updated)
        if mode == "timer":
            cur = max(0.0, base_sec - elapsed)
            total_s = int(round(cur))
        else:
            cur = base_sec + elapsed
            total_s = int(cur)

        h, rem = divmod(total_s, 3600)
        m, s = divmod(rem, 60)
        if h > 0:
            fmt = f"{h}:{m:02d}:{s:02d}"
        else:
            fmt = f"{m:02d}:{s:02d}"
        return True, mode, fmt
