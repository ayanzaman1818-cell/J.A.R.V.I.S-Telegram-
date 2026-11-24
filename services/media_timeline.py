"""Zero-dependency Windows GSMTC (GlobalSystemMediaTransportControls) live media timeline & seek bridge."""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import threading
import time
from typing import Any

log = logging.getLogger("jarvis.media_timeline")

_PS_TIMELINE_SCRIPT = r"""
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
$sess = $mgr.GetCurrentSession()
if (-not $sess) {
    $sessions = $mgr.GetSessions()
    if ($sessions -and $sessions.Count -gt 0) { $sess = $sessions[0] }
}
if ($sess) {
    if ($env:JARVIS_SEEK_SEC) {
        $ticks = [long]([double]$env:JARVIS_SEEK_SEC * 10000000)
        $null = Await ($sess.TryChangePlaybackPositionAsync($ticks)) ([bool])
        Start-Sleep -Milliseconds 120
    }
    $tl = $sess.GetTimelineProperties()
    $pb = $sess.GetPlaybackInfo()
    $pos = [math]::Round($tl.Position.TotalSeconds, 3)
    $end = [math]::Round($tl.EndTime.TotalSeconds, 3)
    $last = $tl.LastUpdatedTime.ToUnixTimeMilliseconds() / 1000.0
    Write-Output ("STATUS=$($pb.PlaybackStatus)|POS=$pos|END=$end|LAST=$last")
} else {
    Write-Output "NO_SESSION"
}
""".strip()

_ENCODED_PS = base64.b64encode(_PS_TIMELINE_SCRIPT.encode("utf-16le")).decode("ascii")


def query_media_timeline_sync(seek_seconds: float | None = None) -> dict[str, Any]:
    """Query (and optionally seek) the active Windows GSMTC media session (Spotify, Chrome, etc.)."""
    if os.name != "nt":
        return {"ok": False, "playing": False, "pos": 0.0, "duration": 0.0, "updated_ts": 0.0}
    try:
        env = os.environ.copy()
        if seek_seconds is not None:
            env["JARVIS_SEEK_SEC"] = f"{max(0.0, float(seek_seconds)):.3f}"
        else:
            env.pop("JARVIS_SEEK_SEC", None)
        res = subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-EncodedCommand", _ENCODED_PS],
            env=env,
            capture_output=True,
            text=True,
            timeout=6,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (res.stdout or "").strip()
        if "STATUS=" in out:
            parts = dict(item.split("=", 1) for item in out.splitlines()[-1].split("|") if "=" in item)
            status = parts.get("STATUS", "").strip().lower()
            pos = float(parts.get("POS", "0") or 0.0)
            dur = float(parts.get("END", "0") or 0.0)
            last_ts = float(parts.get("LAST", "0") or 0.0)
            return {
                "ok": True,
                "playing": status == "playing",
                "pos": max(0.0, pos),
                "duration": max(0.0, dur),
                "updated_ts": last_ts if last_ts > 0 else time.time(),
            }
    except Exception:
        log.debug("Windows GSMTC timeline query failed", exc_info=True)
    return {"ok": False, "playing": False, "pos": 0.0, "duration": 0.0, "updated_ts": time.time()}


class MediaTimelineTracker:
    """Maintains a smooth, second-accurate interpolated playback position synced with Windows GSMTC."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pos: float = 0.0
        self._duration: float = 0.0
        self._updated_ts: float = 0.0
        self._playing: bool = False
        self._last_poll_ts: float = 0.0
        self._polling: bool = False

    def trigger_poll_async(self, force: bool = False) -> None:
        now = time.time()
        with self._lock:
            if self._polling:
                return
            if not force and (now - self._last_poll_ts < 4.0):
                return
            self._polling = True
            self._last_poll_ts = now

        def _worker() -> None:
            try:
                data = query_media_timeline_sync()
                with self._lock:
                    if data.get("ok"):
                        self._pos = float(data["pos"])
                        self._duration = float(data["duration"])
                        self._updated_ts = float(data["updated_ts"])
                        self._playing = bool(data["playing"])
            finally:
                with self._lock:
                    self._polling = False

        threading.Thread(target=_worker, daemon=True).start()

    def notify_track_changed(self) -> None:
        """Immediately reset position for a newly started track and force-sync with Windows GSMTC."""
        with self._lock:
            self._pos = 0.0
            self._duration = 0.0
            self._updated_ts = time.time()
            self._last_poll_ts = 0.0

        def _sync_new_track() -> None:
            # Poll once immediately, and again ~650ms later in case Spotify's GSMTC timeline lags behind title update
            for delay in (0.15, 0.65):
                time.sleep(delay)
                data = query_media_timeline_sync()
                with self._lock:
                    if data.get("ok"):
                        self._pos = float(data["pos"])
                        self._duration = float(data["duration"])
                        self._updated_ts = float(data["updated_ts"])
                        self._playing = bool(data["playing"])
                        self._last_poll_ts = time.time()

        threading.Thread(target=_sync_new_track, daemon=True).start()

    def seek_async(self, target_seconds: float) -> None:
        """Seek the playing track to target_seconds via Windows GSMTC TryChangePlaybackPositionAsync."""
        target = max(0.0, float(target_seconds))
        with self._lock:
            self._pos = target
            self._updated_ts = time.time()

        def _worker() -> None:
            data = query_media_timeline_sync(seek_seconds=target)
            with self._lock:
                if data.get("ok"):
                    self._pos = float(data["pos"])
                    self._duration = float(data["duration"])
                    self._updated_ts = float(data["updated_ts"])
                    self._playing = bool(data["playing"])

        threading.Thread(target=_worker, daemon=True).start()

    def current_position(self) -> tuple[float, float, bool]:
        """Return (interpolated_pos_sec, duration_sec, is_playing) at 60fps without blocking."""
        with self._lock:
            pos = self._pos
            dur = self._duration
            playing = self._playing
            updated = self._updated_ts
        if playing and dur > 0 and updated > 0:
            elapsed = max(0.0, time.time() - updated)
            pos = min(dur, pos + elapsed)
        return pos, dur, playing
