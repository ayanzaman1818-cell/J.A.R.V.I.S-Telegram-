"""Zero-dependency Windows GSMTC (GlobalSystemMediaTransportControls) live media timeline & seek bridge.

Uses a persistent warm PowerShell WinRT daemon (~2ms per poll, zero cold-start lag)
and anchors Spotify's exact 100ns-tick GSMTC `Position` + `LastUpdatedTime` + `PlaybackRate`
directly to Python's monotonic `time.perf_counter()` for sub-millisecond 60fps accuracy.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import threading
import time
from typing import Any

log = logging.getLogger("jarvis.media_timeline")

_PS_DAEMON_SCRIPT = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}
[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager,Windows.Media.Control,ContentType=WindowsRuntime] | Out-Null
$mgr = Await ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()) ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])

function Read-State([string]$seekArg) {
    $sess = $mgr.GetCurrentSession()
    if (-not $sess) {
        $sessions = $mgr.GetSessions()
        if ($sessions -and $sessions.Count -gt 0) { $sess = $sessions[0] }
    }
    if (-not $sess) {
        return "NO_SESSION"
    }
    if ($seekArg) {
        $ticks = [long]([double]$seekArg * 10000000.0)
        $null = Await ($sess.TryChangePlaybackPositionAsync($ticks)) ([bool])
        Start-Sleep -Milliseconds 60
    }
    $tl = $sess.GetTimelineProperties()
    $pb = $sess.GetPlaybackInfo()
    $posSec = $tl.Position.Ticks / 10000000.0
    $endSec = $tl.EndTime.Ticks / 10000000.0
    $rate = 1.0
    if ($null -ne $pb.PlaybackRate -and [double]$pb.PlaybackRate -gt 0.0) {
        $rate = [double]$pb.PlaybackRate
    }
    $status = [string]$pb.PlaybackStatus
    $nowTicks = [DateTimeOffset]::UtcNow.Ticks
    $lastTicks = $tl.LastUpdatedTime.Ticks
    $deltaSec = 0.0
    if ($status -eq 'Playing' -and $lastTicks -gt 0) {
        $deltaSec = ($nowTicks - $lastTicks) / 10000000.0
        if ($deltaSec -lt 0.0) { $deltaSec = 0.0 }
    }
    $exactPos = $posSec + ($deltaSec * $rate)
    if ($endSec -gt 0.0 -and $exactPos -gt $endSec) { $exactPos = $endSec }
    return ("STATUS=" + $status + "|POS=" + $exactPos.ToString("F6", [Globalization.CultureInfo]::InvariantCulture) + "|END=" + $endSec.ToString("F6", [Globalization.CultureInfo]::InvariantCulture) + "|RATE=" + $rate.ToString("F3", [Globalization.CultureInfo]::InvariantCulture))
}

[Console]::Out.WriteLine("READY")
[Console]::Out.Flush()

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    $cmd = $line.Trim()
    if ($cmd -eq "QUIT") { break }
    try {
        if ($cmd.StartsWith("SEEK ")) {
            $arg = $cmd.Substring(5).Trim()
            [Console]::Out.WriteLine((Read-State $arg))
        } else {
            [Console]::Out.WriteLine((Read-State ""))
        }
    } catch {
        [Console]::Out.WriteLine("ERR")
    }
    [Console]::Out.Flush()
}
""".strip()

_ENCODED_DAEMON_PS = base64.b64encode(_PS_DAEMON_SCRIPT.encode("utf-16le")).decode("ascii")


class MediaTimelineTracker:
    """Maintains a sub-millisecond monotonic playback position synced to Spotify via a warm WinRT daemon."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._pos: float = 0.0
        self._duration: float = 0.0
        self._rate: float = 1.0
        self._perf_anchor: float = 0.0
        self._updated_ts: float = 0.0
        self._playing: bool = False
        self._last_poll_ts: float = 0.0
        self._polling: bool = False

        # Warm up the persistent WinRT daemon immediately on a background thread
        if os.name == "nt":
            threading.Thread(target=self._ensure_daemon_locked, daemon=True).start()

    def _ensure_daemon_locked(self) -> subprocess.Popen[str] | None:
        """Start (or reuse) the persistent warm PowerShell GSMTC daemon."""
        with self._io_lock:
            if self._proc is not None and self._proc.poll() is None:
                return self._proc
            try:
                proc = subprocess.Popen(
                    ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-EncodedCommand", _ENCODED_DAEMON_PS],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                ready = proc.stdout.readline().strip() if proc.stdout else ""
                if ready == "READY":
                    self._proc = proc
                    return proc
                proc.kill()
            except Exception:
                log.debug("Failed to start persistent GSMTC daemon", exc_info=True)
            self._proc = None
            return None

    def _query_daemon(self, cmd: str = "POLL") -> dict[str, Any]:
        """Send POLL or SEEK to the warm WinRT daemon (~2ms RTT) and return exact sub-ms state."""
        if os.name != "nt":
            return {"ok": False}
        proc = self._ensure_daemon_locked()
        if proc is None or proc.stdin is None or proc.stdout is None:
            return {"ok": False}
        with self._io_lock:
            try:
                t0 = time.perf_counter()
                proc.stdin.write(cmd + "\n")
                proc.stdin.flush()
                out = proc.stdout.readline().strip()
                t1 = time.perf_counter()
                mid_perf = 0.5 * (t0 + t1)
                if "STATUS=" in out:
                    parts = dict(item.split("=", 1) for item in out.split("|") if "=" in item)
                    status = parts.get("STATUS", "").strip().lower()
                    pos = max(0.0, float(parts.get("POS", "0") or 0.0))
                    dur = max(0.0, float(parts.get("END", "0") or 0.0))
                    rate = float(parts.get("RATE", "1.0") or 1.0)
                    return {
                        "ok": True,
                        "playing": status == "playing",
                        "pos": pos,
                        "duration": dur,
                        "rate": rate if rate > 0 else 1.0,
                        "perf_anchor": mid_perf,
                    }
            except Exception:
                log.debug("GSMTC daemon query failed; resetting daemon", exc_info=True)
                try:
                    proc.kill()
                except Exception:
                    pass
                self._proc = None
        return {"ok": False}

    def _apply_sample(self, data: dict[str, Any]) -> None:
        if not data.get("ok"):
            return
        with self._lock:
            self._pos = float(data["pos"])
            self._duration = float(data["duration"])
            self._rate = float(data.get("rate", 1.0))
            self._perf_anchor = float(data["perf_anchor"])
            self._updated_ts = time.time()
            self._playing = bool(data["playing"])

    def trigger_poll_async(self, force: bool = False) -> None:
        now = time.time()
        with self._lock:
            if self._polling:
                return
            # Warm daemon takes ~2ms, so polling every 0.6s keeps Spotify 100% locked with ~0% CPU
            if not force and (now - self._last_poll_ts < 0.6):
                return
            self._polling = True
            self._last_poll_ts = now

        def _worker() -> None:
            try:
                self._apply_sample(self._query_daemon("POLL"))
            finally:
                with self._lock:
                    self._polling = False

        threading.Thread(target=_worker, daemon=True).start()

    def notify_track_changed(self) -> None:
        """Immediately reset position for a newly started track and force-sync with Spotify GSMTC."""
        with self._lock:
            self._pos = 0.0
            self._duration = 0.0
            self._perf_anchor = time.perf_counter()
            self._updated_ts = time.time()
            self._last_poll_ts = 0.0

        def _sync_new_track() -> None:
            for delay in (0.04, 0.25, 0.65):
                time.sleep(delay)
                data = self._query_daemon("POLL")
                if data.get("ok"):
                    self._apply_sample(data)

        threading.Thread(target=_sync_new_track, daemon=True).start()

    def seek_async(self, target_seconds: float) -> None:
        """Seek the playing track to target_seconds via Windows GSMTC TryChangePlaybackPositionAsync."""
        target = max(0.0, float(target_seconds))
        with self._lock:
            self._pos = target
            self._perf_anchor = time.perf_counter()
            self._updated_ts = time.time()

        def _worker() -> None:
            self._apply_sample(self._query_daemon(f"SEEK {target:.3f}"))

        threading.Thread(target=_worker, daemon=True).start()

    def current_position(self) -> tuple[float, float, bool]:
        """Return (interpolated_pos_sec, duration_sec, is_playing) at 60fps with sub-millisecond precision."""
        with self._lock:
            pos = self._pos
            dur = self._duration
            rate = self._rate
            playing = self._playing
            perf_anchor = self._perf_anchor
        if playing and dur > 0 and perf_anchor > 0:
            elapsed = max(0.0, (time.perf_counter() - perf_anchor) * rate)
            pos = min(dur, pos + elapsed)
        return pos, dur, playing
