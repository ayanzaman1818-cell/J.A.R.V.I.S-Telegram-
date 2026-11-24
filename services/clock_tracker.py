"""Zero-dependency Windows Clock (Microsoft.WindowsAlarms) live Timer & Stopwatch tracker.

- Timers: Reads exact scheduled timer end timestamps directly from Windows
  ToastNotificationManager (`Microsoft.WindowsAlarms_8wekyb3d8bbwe!App`), so
  active countdown timers are detected with millisecond accuracy EVEN WHEN the
  Windows Clock window is minimized or closed!
- Stopwatches: Reads active running Stopwatch via Windows UI Automation when
  the Windows Clock window is open, and smoothly interpolates at 60fps.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import threading
import time
from typing import Any

log = logging.getLogger("jarvis.clock_tracker")

_PS_CLOCK_SCRIPT = r"""
# 1. Check Windows Clock Scheduled Timer Toasts (works even when Clock app is minimized or closed!)
$bestTimerDue = 0.0
try {
    [Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime] | Out-Null
    $tn = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Microsoft.WindowsAlarms_8wekyb3d8bbwe!App')
    $st = $tn.GetScheduledToastNotifications()
    $nowMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    foreach ($x in $st) {
        $xml = ""
        try { $xml = $x.Content.GetXml() } catch {}
        if ($xml -match 'type=timer|TimerDoneText') {
            $dueMs = $x.DeliveryTime.ToUnixTimeMilliseconds()
            if ($dueMs -gt $nowMs) {
                $dueSec = $dueMs / 1000.0
                if ($bestTimerDue -eq 0.0 -or $dueSec -lt $bestTimerDue) {
                    $bestTimerDue = $dueSec
                }
            }
        }
    }
} catch {}

if ($bestTimerDue -gt 0.0) {
    Write-Output ("MODE=timer|DUE=" + [math]::Round($bestTimerDue, 3))
    exit 0
}

# 2. Check Windows Clock UI Automation for an active running Stopwatch (when Clock window is open)
try {
    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, 'Clock')
    $win = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $cond)
    if ($win) {
        $all = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
        $swTime = ""
        $swRunning = $false
        foreach ($el in $all) {
            try {
                $aid = [string]$el.Current.AutomationId
                $nm = [string]$el.Current.Name
                $ct = $el.Current.ControlType.ProgrammaticName
                if (($ct -eq 'ControlType.Button') -and ($nm -match '^Pause') -and ($aid -match 'Stopwatch|PlayPause')) {
                    $swRunning = $true
                }
                if ($aid -match 'Stopwatch' -and $nm -match '(\d{1,2}:\d{2}:\d{2}(?:\.\d+)?|\d{1,2}:\d{2}\.\d+)') {
                    $swTime = $Matches[1]
                } elseif ($nm -match '(\d{1,2}:\d{2}:\d{2}\.\d{1,2})') {
                    $swTime = $Matches[1]
                }
            } catch {}
        }
        if ($swRunning -and $swTime) {
            Write-Output ("MODE=stopwatch|TIME=" + $swTime)
            exit 0
        }
    }
} catch {}

Write-Output "IDLE"
""".strip()

_ENCODED_CLOCK_PS = base64.b64encode(_PS_CLOCK_SCRIPT.encode("utf-16le")).decode("ascii")


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


def query_windows_clock_sync() -> dict[str, Any]:
    """Query Windows Clock for an active Timer (via Scheduled Toasts) or Stopwatch (via UIA)."""
    if os.name != "nt":
        return {"active": False, "mode": "", "due_ts": 0.0, "seconds": 0.0, "ts": time.time()}
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
            if mode == "timer":
                due_ts = float(parts.get("DUE", "0") or 0.0)
                if due_ts > time.time():
                    return {"active": True, "mode": "timer", "due_ts": due_ts, "seconds": 0.0, "ts": time.time()}
            elif mode == "stopwatch":
                secs = _parse_hms_to_seconds(parts.get("TIME", ""))
                return {"active": True, "mode": "stopwatch", "due_ts": 0.0, "seconds": secs, "ts": time.time()}
    except Exception:
        log.debug("Windows Clock poll failed", exc_info=True)
    return {"active": False, "mode": "", "due_ts": 0.0, "seconds": 0.0, "ts": time.time()}


class WindowsClockTracker:
    """Maintains a smooth 60fps interpolated Timer / Stopwatch state synced with Windows Clock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: bool = False
        self._mode: str = ""  # "timer" or "stopwatch"
        self._due_ts: float = 0.0
        self._base_seconds: float = 0.0
        self._updated_ts: float = 0.0
        self._last_poll_ts: float = 0.0
        self._polling: bool = False

    def trigger_poll_async(self) -> None:
        """Non-blocking background poll every ~2.0s so active timers & stopwatches appear automatically."""
        now = time.time()
        with self._lock:
            if self._polling:
                return
            if now - self._last_poll_ts < 2.0:
                return
            self._polling = True
            self._last_poll_ts = now

        def _worker() -> None:
            try:
                data = query_windows_clock_sync()
                with self._lock:
                    self._active = bool(data["active"])
                    self._mode = str(data["mode"])
                    self._due_ts = float(data["due_ts"])
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
            due_ts = self._due_ts
            base_sec = self._base_seconds
            updated = self._updated_ts

        if not active or not mode:
            return False, "", ""

        now = time.time()
        if mode == "timer":
            remaining = due_ts - now
            if remaining <= 0.0:
                return False, "", ""
            total_s = int(remaining + 0.5)
        else:
            elapsed = max(0.0, now - updated)
            total_s = int(base_sec + elapsed)

        h, rem = divmod(total_s, 3600)
        m, s = divmod(rem, 60)
        if h > 0:
            fmt = f"{h}:{m:02d}:{s:02d}"
        else:
            fmt = f"{m:02d}:{s:02d}"
        return True, mode, fmt
