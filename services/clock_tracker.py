"""Zero-dependency Windows Clock (Microsoft.WindowsAlarms) live Timer & Stopwatch tracker.

- Timers: Reads exact scheduled timer end timestamps directly from Windows
  ToastNotificationManager (`Microsoft.WindowsAlarms_8wekyb3d8bbwe!App`), aligned
  to Windows Clock's exact displayed second even when Clock is minimized or closed.
- Stopwatches: Reads active running Stopwatch via Windows UI Automation (supports
  both numeric `HH:MM:SS.cc` and Win11 spoken accessibility formats + Lap-button
  running detection) and continues smooth 60fps ticking even if Clock is minimized.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import subprocess
import threading
import time
from typing import Any

log = logging.getLogger("jarvis.clock_tracker")

_PS_CLOCK_SCRIPT = r"""
$timerDue = 0.0
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
                if ($timerDue -eq 0.0 -or $dueSec -lt $timerDue) {
                    $timerDue = $dueSec
                }
            }
        }
    }
} catch {}

$swRunning = $false
$swTime = ""
$clockWinFound = $false
$timeProcAlive = [bool](Get-Process -Name 'Time' -ErrorAction SilentlyContinue)

try {
    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
    $win = $null
    foreach ($w in $wins) {
        try {
            $wn = [string]$w.Current.Name
            $wc = [string]$w.Current.ClassName
            if ($wn -eq 'Clock' -or ($wc -eq 'ApplicationFrameWindow' -and $wn -match 'Clock')) {
                $win = $w
                break
            }
        } catch {}
    }
    if ($win) {
        $clockWinFound = $true
        $all = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
        $lapEnabled = $false
        $swPauseBtn = $false
        foreach ($el in $all) {
            try {
                $aid = [string]$el.Current.AutomationId
                $nm = [string]$el.Current.Name
                $ct = $el.Current.ControlType.ProgrammaticName

                # ONLY Stopwatch-exclusive controls (NEVER match the sidebar nav item 'Stopwatch'!)
                if ($ct -eq 'ControlType.Button') {
                    if (($nm -match '^Lap$' -or $aid -match 'Stopwatch.*Lap|LapButton') -and $el.Current.IsEnabled) {
                        $lapEnabled = $true
                    }
                    if ($aid -match 'Stopwatch.*PlayPause|Stopwatch.*Pause' -and $nm -match 'Pause') {
                        $swPauseBtn = $true
                    }
                }
                if ($aid -match 'StopwatchTimer|StopwatchTime') {
                    if ($nm) { $swTime = $nm }
                } elseif (-not $swTime -and $nm -match '(\d{1,2}:\d{2}:\d{2}\.\d{1,2}|\d{1,2}:\d{2}\.\d{1,2})') {
                    $swTime = $Matches[1]
                }
            } catch {}
        }
        if (($lapEnabled -or $swPauseBtn) -and $swTime) {
            $swRunning = $true
        }
    }
} catch {}

if ($swRunning -and $swTime) {
    Write-Output ("MODE=stopwatch|TIME=" + $swTime + "|WIN=1|ALIVE=" + [int]$timeProcAlive)
} elseif ($timerDue -gt 0.0) {
    Write-Output ("MODE=timer|DUE=" + [math]::Round($timerDue, 3) + "|WIN=" + [int]$clockWinFound + "|ALIVE=" + [int]$timeProcAlive)
} else {
    Write-Output ("MODE=idle|WIN=" + [int]$clockWinFound + "|ALIVE=" + [int]$timeProcAlive)
}
""".strip()

_ENCODED_CLOCK_PS = base64.b64encode(_PS_CLOCK_SCRIPT.encode("utf-16le")).decode("ascii")


def _parse_clock_time_to_seconds(text: str) -> float:
    """Parse numeric ('00:05.23', '01:02:03.45') OR Win11 spoken ('0 hours, 1 minute, 23 seconds') strings."""
    clean = (text or "").strip()
    if not clean:
        return 0.0

    # 1. Numeric HH:MM:SS(.cc) or MM:SS(.cc)
    m_num = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.(\d+))?", clean)
    if m_num:
        a = int(m_num.group(1))
        b = int(m_num.group(2))
        c = int(m_num.group(3)) if m_num.group(3) is not None else None
        frac = float("0." + m_num.group(4)) if m_num.group(4) else 0.0
        if c is not None:
            return a * 3600.0 + b * 60.0 + c + frac
        return a * 60.0 + b + frac

    # 2. Spoken Win11 UIA format: e.g. "0 hours, 1 minute, 23 seconds" or "12.4 seconds"
    total = 0.0
    matched = False
    for pat, mult in (
        (r"(\d+(?:\.\d+)?)\s*hour", 3600.0),
        (r"(\d+(?:\.\d+)?)\s*minute", 60.0),
        (r"(\d+(?:\.\d+)?)\s*second", 1.0),
    ):
        mh = re.search(pat, clean, re.IGNORECASE)
        if mh:
            total += float(mh.group(1)) * mult
            matched = True
    return total if matched else 0.0


def query_windows_clock_sync() -> dict[str, Any]:
    """Query Windows Clock for an active Timer (Scheduled Toasts) or Stopwatch (UIA)."""
    if os.name != "nt":
        return {"active": False, "mode": "", "due_ts": 0.0, "seconds": 0.0, "win": False, "alive": False, "ts": time.time()}
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
            win_open = parts.get("WIN", "0").strip() == "1"
            alive = parts.get("ALIVE", "0").strip() == "1"
            now = time.time()
            if mode == "timer":
                due_ts = float(parts.get("DUE", "0") or 0.0)
                if due_ts > now:
                    return {
                        "active": True,
                        "mode": "timer",
                        "due_ts": due_ts,
                        "seconds": 0.0,
                        "win": win_open,
                        "alive": alive,
                        "ts": now,
                    }
            elif mode == "stopwatch":
                secs = _parse_clock_time_to_seconds(parts.get("TIME", ""))
                return {
                    "active": True,
                    "mode": "stopwatch",
                    "due_ts": 0.0,
                    "seconds": secs,
                    "win": True,
                    "alive": alive,
                    "ts": now,
                }
            return {"active": False, "mode": "", "due_ts": 0.0, "seconds": 0.0, "win": win_open, "alive": alive, "ts": now}
    except Exception:
        log.debug("Windows Clock poll failed", exc_info=True)
    return {"active": False, "mode": "", "due_ts": 0.0, "seconds": 0.0, "win": False, "alive": False, "ts": time.time()}


class WindowsClockTracker:
    """Maintains a smooth 60fps interpolated Timer / Stopwatch state synced with Windows Clock."""

    # Exact alignment with Windows Clock's floor-displayed countdown (no artificial offset)
    TIMER_TOAST_OFFSET_SEC = 0.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: bool = False
        self._mode: str = ""  # "timer" or "stopwatch"
        self._due_ts: float = 0.0
        self._timer_total_sec: float = 60.0
        self._base_seconds: float = 0.0
        self._updated_ts: float = 0.0
        self._last_poll_ts: float = 0.0
        self._polling: bool = False

    def trigger_poll_async(self) -> None:
        """Non-blocking background poll every ~1.8s so active timers & stopwatches stay synced."""
        now = time.time()
        with self._lock:
            if self._polling:
                return
            if now - self._last_poll_ts < 1.8:
                return
            self._polling = True
            self._last_poll_ts = now

        def _worker() -> None:
            try:
                data = query_windows_clock_sync()
                with self._lock:
                    if data["active"]:
                        new_mode = str(data["mode"])
                        new_due = float(data["due_ts"])
                        if new_mode == "timer":
                            rem_now = max(1.0, new_due - time.time())
                            # Reset total duration if a brand-new timer was started (due_ts shifted by >2s)
                            if (not self._active) or self._mode != "timer" or abs(new_due - self._due_ts) > 2.0:
                                self._timer_total_sec = rem_now
                            else:
                                self._timer_total_sec = max(self._timer_total_sec, rem_now)
                        self._active = True
                        self._mode = new_mode
                        self._due_ts = new_due
                        self._base_seconds = float(data["seconds"])
                        self._updated_ts = float(data["ts"])
                    else:
                        # If a Stopwatch was running and the user simply minimized Clock (win=False, Time.exe still alive),
                        # keep ticking smoothly until Clock is closed (alive=False) or reopened & paused (win=True)!
                        if self._active and self._mode == "stopwatch" and (not data["win"]) and data["alive"]:
                            pass
                        else:
                            self._active = False
                            self._mode = ""
                            self._due_ts = 0.0
            finally:
                with self._lock:
                    self._polling = False

        threading.Thread(target=_worker, daemon=True).start()

    def current_state(self) -> tuple[bool, str, str, float, float]:
        """Return (is_active, mode, formatted_time, exact_seconds, timer_fraction_0_to_1) at 60fps."""
        with self._lock:
            active = self._active
            mode = self._mode
            due_ts = self._due_ts
            total_dur = self._timer_total_sec
            base_sec = self._base_seconds
            updated = self._updated_ts

        if not active or not mode:
            return False, "", "", 0.0, 0.0

        now = time.time()
        if mode == "timer":
            remaining = (due_ts - self.TIMER_TOAST_OFFSET_SEC) - now
            if remaining <= 0.0:
                return False, "", "", 0.0, 0.0
            exact_sec = remaining
            frac = max(0.0, min(1.0, remaining / max(1.0, total_dur)))
            total_s = int(remaining)
        else:
            elapsed = max(0.0, now - updated)
            exact_sec = base_sec + elapsed
            frac = (exact_sec % 60.0) / 60.0
            total_s = int(exact_sec)

        h, rem = divmod(total_s, 3600)
        m, s = divmod(rem, 60)
        if h > 0:
            fmt = f"{h}:{m:02d}:{s:02d}"
        else:
            fmt = f"{m:02d}:{s:02d}"
        return True, mode, fmt, exact_sec, frac
