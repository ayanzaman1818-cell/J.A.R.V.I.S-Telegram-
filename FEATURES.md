# The J.A.R.V.I.S Notch — Complete Feature Guide & Engineering Log

This document explains every feature inside **The J.A.R.V.I.S Notch** and summarizes everything we designed, built, and refined across the project.

---

## 1. Design Philosophy & Visual Identity

- **Solid Pitch-Black Borderless Aesthetic**: Pure `#07080b` Dynamic Island silhouette with zero borders and no glowing outlines.
- **Bundled Apple Typography**: Uses bundled **SF Pro Display** and **SF Pro Text** (`assets/fonts/`) across every pill, timer, dashboard card, and lyrics view.
- **100% Custom Vector Graphics (Zero Windows Emojis)**: Every icon (music controls, player badges, weather, timer, stopwatch, speaker, clipboard, camera, downloads, battery, system stats) is hand-drawn with `QPainter` paths and HiDPI supersampling.
- **Silky Physics & Spring Animations**: Carefully paced Qt curves (`OutBack`, `OutQuart`, `InOutCubic`) for opening, closing, morphing, title-bar dodging, and lyrics transitions — engineered to avoid Windows Qt `QGraphicsOpacityEffect` nesting and mask-clipping glitches.

---

## 2. Dynamic Island Idle Notch & Live Pop-Outs

### Idle Music Pill & Interactive Hover Controls
- **Lossless GSMTC Album Art**: Pulls the exact album cover directly from Windows Global System Media Transport Controls (`TryGetMediaPropertiesAsync().Thumbnail` via WinRT `AsStreamForRead`), eliminating mismatched third-party cover searches.
- **True Apple Squircle Masking**: Pre-renders both the `18×18` idle thumbnail and the `84×84` dashboard cover using continuous-curvature superellipse math ($|x|^{4.6} + |y|^{4.6} = 1$) with PIL Lanczos downsampling and subtle UnsharpMask sharpening.
- **60fps Song Title Marquee & Progress Hairline**: Replaces static idle text with a smooth 60fps scrolling song title and a live album-color-tinted progress hairline along the bottom edge.
- **Hover Mini-Controls**: Hovering over the idle music pill smoothly widens it (`hover_music`) to reveal clickable monochrome vector **Previous / Play-Pause / Next** buttons.
- **Direct Mouse Shortcuts on the Idle Notch**:
  - **Scroll Wheel**: Adjusts system volume and pops the Volume HUD.
  - **Middle-Click**: Toggles Play/Pause immediately.
  - **Right-Click**: Skips to the next track.
- **Real-Time 60fps WASAPI Audio Visualizer**: Driven directly by Windows WASAPI `IAudioMeterInformation` using true unboosted decibel metering (`-42 dB .. 0 dB`) on a `16ms PreciseTimer`, tinted with the active album cover's extracted color palette.

### Live System Activity Pills
- **Volume HUD Pill**: Pops an animated volume bar pill whenever Windows volume changes (via keyboard keys, mouse scroll, or OS controls).
- **Battery Pulse Pill**: Pops an animated charging / low-battery status pill when AC power is plugged in or battery drops low.
- **Active Downloads Progress Ring**: Watches `~/Downloads` for active `.crdownload` / `.part` / `.tmp` downloads, rendering a live circular progress ring and flashing an emerald checkmark on completion.
- **Hardware-Level Mic & Camera Privacy Dots**:
  - Clean, single glowing iOS-style **orange (Mic)** or **green (Camera)** privacy dot in a dedicated `20px` far-right slot so it never collides with the visualizer or timer, remaining visible across all notch states.
  - **Instant Microphone Detection**: Uses Windows CoreAudio `IAudioSessionManager2` (`eCapture`) to detect active mic capture in `< 0.5ms`.
  - **Instant Camera Detection (`< 0.5s` turn-off)**: Combines `QueryServiceStatusEx` on Windows `FrameServer` with `ntdll.NtQuerySystemInformation` live CPU-tick delta so the green dot turns off within ~0.5 seconds of closing the camera (bypassing Windows's 30-second `FrameServer` idle shutdown cooldown).
- **Dynamic Island Notification Toasts**: Displays 5.5-second animated toast pills for scheduled reminders, timer completions, and system alerts with 1-click **Reply** and **Dismiss** actions.

### Smart Title-Bar Dodge & Fullscreen Auto-Hide
- **Smart Title-Bar Dodge**: Detects when your cursor approaches browser tabs or window title bars directly beneath the notch and smoothly tucks into a 3px micro-line (`460ms OutQuart` tuck, `580ms OutBack` bouncy spring return).
- **Automatic Fullscreen Hide**: Polls every `0.35s` using `SHQueryUserNotificationState` (`QUNS_RUNNING_D3D_FULL_SCREEN` / `QUNS_PRESENTATION_MODE`, deliberately ignoring `QUNS_BUSY` so Focus Assist / Do Not Disturb never falsely hides the notch) plus foreground window rect comparison against the primary monitor's `rcMonitor` bounds. Completely hides the notch (`self.hide()`, closing any open panel/mirror and suppressing toasts/mouse interception) during fullscreen YouTube, Netflix, VLC, Chrome `F11`, presentations, or games — and restores it automatically on exit.

---

## 3. Wide PC Dashboard (`Ctrl + /` or Click Notch)

### Now Playing Card
- **`84×84` Squircle Album Cover** with a hover **"Lyrics"** pill overlay.
- **Clickable `26×26` Music Player Badge** directly below the cover: 4×-supersampled custom vector icons for **Spotify**, **Apple Music / iTunes**, **YouTube Music**, **YouTube**, and **SoundCloud**. Clicking reliably focuses or restores the music player from any app (even when minimized to tray) via Win32 `AttachThreadInput` + protocol URI fallback (`spotify:`, `music:`).
- **Instant 0ms Optimistic Playback Controls**: Play/Pause icon flips and timeline freezes/resumes instantaneously on click without waiting for GSMTC round-trip latency.
- **Interactive Seek / Scrub Bar & Remaining-Time Toggle**: Click or drag anywhere on the progress bar to seek via Windows GSMTC; click the elapsed/remaining timestamp to toggle between total duration and remaining time (`-mm:ss`).
- **20-Second Magnetic Retraction**: Automatically collapses music state cleanly after 20 seconds of being paused.

### Synced Live Lyrics View
- **Open**: Click the album cover (or the dedicated lyrics icon button) to switch into the live lyrics view, complete with built-in **Prev / Play-Pause / Next** playback controls.
- **Strict Song Verification**: Queries `lrclib.net` via exact `/api/get` and structured `/api/search` with strict normalized artist + title validation so unrelated songs are never shown.
- **Exact Spotify GSMTC Clock Lock**: Locks directly to Windows GSMTC's live playback position + LRC `[offset:ms]` metadata, with `−0.25s` / `+0.25s` manual sync nudge buttons.
- **True Enhanced-LRC & Clean Line Sync**: Uses real `<mm:ss.xx>` word-level timestamps when present in enhanced LRC tracks, and clean line-by-line sync with smooth `OutCubic` slide-up line transitions otherwise.

### Clock, Calendar, Weather & Quick Actions
- **Top-Right 12-Hour Live Clock** paired with a **1-Click Quick Bar** (`5m`, `15m`, `25m` Pomodoro timers, **Mirror**, and **Clipboard**).
- **7-Day Calendar Strip & Upcoming Event Pill**: Displays the current week and upcoming scheduled events.
- **Live Weather Vector Badge**: Fetches live weather from Open-Meteo (30-minute cache) and renders custom vector sun/cloud/rain/storm graphics.

### System Monitor, Volume Slider & Audio Output Switcher
- **System Resource Bars**: Live CPU, RAM, and Disk usage bars with Low / Normal / High status indicators and custom vector icons.
- **1:1 Windows Volume Slider**: Vertical CoreAudio `IAudioEndpointVolume` slider synced bidirectionally with Windows master volume.
- **1-Click Audio Output Switcher**: Click the vector speaker icon beneath the volume slider to list and immediately switch Windows default playback devices (Speakers, Headphones, Bluetooth) via COM `IPolicyConfig`.

---

## 4. Windows Clock Timer, Stopwatch & Built-in Pomodoro

- **Windows Clock App Integration** (`services/clock_tracker.py`):
  - **Timers**: Reads the exact `DeliveryTime` directly from Windows `ToastNotificationManager` (`Microsoft.WindowsAlarms_8wekyb3d8bbwe!App`), so active timers remain synced even when the Windows Clock app is minimized or closed.
  - **Stopwatch**: Detects active stopwatches via targeted Windows UI Automation (restricted strictly to stopwatch-exclusive controls so Timers are never misclassified as Stopwatches).
- **Built-in Notch Pomodoro Timers**: Launch `5m / 10m / 15m / 25m / 45m` focus timers straight from the Dashboard quick bar or the Utility Hub.
- **Animated Vector Icons**: Distinct hand-drawn orange (`#ff9f0a`) Timer (with live countdown arc) and Stopwatch (with 60fps rotating hand) icons. Automatically widens the idle notch (`idle_music_clock`) when both music and a timer/stopwatch are running.

---

## 5. 4-Tab Utility Hub (`tools` View — Bento Grid Icon or Drag-and-Drop)

1. **File Shelf & Ask JARVIS Drop Zone**: Drag any file or image onto the notch to park it in the shelf, drag parked items right back out into any app, or drop them onto **Ask JARVIS** for immediate AI analysis.
2. **Clipboard History**: Automatically tracks your last 8 copied text snippets for 1-click re-copy.
3. **Full-Notch Live Webcam Mirror**: Horizontally mirrored, aspect-fill live camera preview (`PySide6.QtMultimedia`) filling the notch; releases the camera immediately on close.
4. **Quick Focus / Pomodoro Creator**: Start, monitor, or cancel built-in focus timers in one click.

---

## 6. Custom Fine-Tuned Model, Borderless Quick Chat, Voice Orb & Hybrid AI

- **Custom Fine-Tuned `JARVIS-Qwen-7B` Model**: Trained specifically for **The J.A.R.V.I.S Notch** via QLoRA + Unsloth (`Q4_K_M` GGUF) and published under Apache 2.0 at [`AyanZaman19/jarvis-qwen-7b-gguf`](https://huggingface.co/AyanZaman19/jarvis-qwen-7b-gguf) alongside its custom dataset [`AyanZaman19/jarvis-notch-dataset`](https://huggingface.co/datasets/AyanZaman19/jarvis-notch-dataset). Wired directly into both the Notch top-bar model switcher and Telegram `/model` menu.
- **Borderless Quick Chat**: Clean `#07080b` chat interface and message input pill inside the notch with a top-bar 1-click model switcher (`JARVIS Qwen 7B (Custom)`, `LM Studio: Qwen3 8B`, `Gemma 4 26B`, `Gemini 3.5 Lite`, etc.).
- **Voice Cloud Orb (`Ctrl + Alt + Space`)**: Two-way voice assistant mode (text replies stay silent by default via `VOICE_CHAT_DEFAULT=false`).
- **Hybrid LLM Router (`services/llm.py`)**: Routes seamlessly between local LM Studio and Google Gemini / Gemma with automatic fallback and smart tool pruning.
- **Composio Universal Connections Hub**: 1-click OAuth connections for Gmail, Google Calendar, Google Drive, and more directly from the notch.
- **Telegram Companion Bot & Scheduler**: Remote chat/control via Telegram (`main.py`, `services/scheduler.py`) with reminders piped straight into Dynamic Island Notification Toasts.
- **Live Hot-Reload Watcher (`main.py`)**: Watches `.py` and `.env` files, validates syntax with `py_compile`, and hot-restarts JARVIS in-place ~2 seconds after saving.

---

## 7. Key Engineering Challenges We Solved

| Area | Problem | Solution Implemented |
| :--- | :--- | :--- |
| **GSMTC Speed & Sync** | Spawning per-poll WinRT processes was slow and laggy. | Built a persistent ~2ms warm WinRT GSMTC daemon with direct lossless thumbnail extraction (`AsStreamForRead`). |
| **Audio Visualizer** | Linear WASAPI peak values (`0.07–0.12`) fell under the bar floor. | Switched to true decibel (`-42 dB .. 0 dB`) metering at 60fps on a `16ms PreciseTimer`. |
| **Privacy Indicators** | Windows 11 `ConsentStore` didn't log `NonPackaged` apps, and `FrameServer` stayed alive 30s after closing the webcam. | Used CoreAudio `IAudioSessionManager2` (`eCapture`) for `<0.5ms` mic detection and `QueryServiceStatusEx` + `ntdll.NtQuerySystemInformation` live CPU-tick delta for `<0.5s` camera turn-off, with explicit 64-bit `ctypes` `argtypes`/`restype`. |
| **Qt Windows Fade Glitches** | Nested `QGraphicsOpacityEffect`s teleported widgets, and `setMask` clipping caused black blanking on close. | Removed inner opacity effects, kept `setMask` outside `_content_root` during close fades, and gated `enterEvent`/dodge with explicit `_fading_out` state. |
| **Windows Clock Detection** | Clock sidebar's `"Stopwatch"` nav item caused Timers to be misread as Stopwatches. | Separated Timer tracking (`ToastNotificationManager` `DeliveryTime`) from Stopwatch tracking (restricted to Stopwatch-exclusive UIA controls). |
| **Player Focusing** | Focusing Spotify/Apple Music from another app was blocked by Windows foreground lock or tray minimization. | Combined `AttachThreadInput` foreground activation with `os.startfile("spotify:")` / `"music:"` fallback. |
| **Lyrics Accuracy & Sync** | Passing strict duration to `lrclib` caused 404s; loose search returned wrong songs; fake word interpolation drifted. | Switched to exact `/api/get` + structured `/api/search` with strict normalized artist+title matching, locked to exact GSMTC playback clock, and used real enhanced-LRC word timestamps when present / clean line-sync otherwise. |
| **Fullscreen False Positives** | Windows 11 returns `QUNS_BUSY (2)` during Focus Assist / Do Not Disturb even on normal desktop. | Restricted shell state check to `QUNS_RUNNING_D3D_FULL_SCREEN (3)` & `QUNS_PRESENTATION_MODE (4)` and paired it with primary-monitor `rcMonitor` foreground rect checks (with 64-bit-safe Win32 `argtypes`). |

---

## 8. Keyboard & Mouse Shortcuts

| Input | Action |
| :--- | :--- |
| `Ctrl + /` | Toggle the Wide PC Dashboard |
| `Ctrl + Alt + Space` | Toggle Voice Cloud Orb Mode |
| `Esc` | Collapse Notch back to Idle |
| **Hover Idle Music Notch** | Reveal mini **Prev / Play-Pause / Next** controls |
| **Scroll on Idle Notch** | Adjust system volume + show Volume HUD |
| **Middle-Click Idle Notch** | Play / Pause |
| **Right-Click Idle Notch** | Next track |
| **Click Album Cover** | Open Synced Live Lyrics |
| **Click Player Icon Badge** | Focus / restore music player (Spotify, Apple Music, etc.) |
| **Click Speaker Icon** | Open 1-click Windows Audio Output Device Switcher |
| **Drag File onto Notch** | Open File Shelf / **Ask JARVIS** drop zone |

---

## 9. File Map

- [`main.py`](main.py) — Entrypoint, Telegram bot, scheduler startup, and Live Hot-Reload file watcher.
- [`services/desktop_overlay.py`](services/desktop_overlay.py) — Core Dynamic Island Notch UI, PC Dashboard, 60fps WASAPI visualizer, hover controls, lyrics view, title-bar dodge, fullscreen auto-hide, and animations.
- [`services/notch_extras.py`](services/notch_extras.py) — Hardware Mic/Camera privacy detection, Downloads watcher, Volume/Battery monitors, `lrclib.net` lyrics fetcher, Open-Meteo weather, CoreAudio `IPolicyConfig` audio device switcher, and Win32 fullscreen detector.
- [`services/notch_widgets.py`](services/notch_widgets.py) — Custom `QPainter` widgets (music player badge, lyrics widget, 4-tab Utility Hub, webcam mirror, file shelf, clipboard history, privacy dot overlay, notification toast, Pomodoro UI).
- [`services/clock_tracker.py`](services/clock_tracker.py) — Windows Clock Timer (`ToastNotificationManager`), Stopwatch (UIA), and built-in Notch Pomodoro tracker.
- [`services/media_timeline.py`](services/media_timeline.py) — Persistent warm WinRT GSMTC bridge for live playback position, seeking, and lossless album cover thumbnails.
- [`services/llm.py`](services/llm.py) — Hybrid Local LM Studio + Google Gemini / Gemma router.
- [`services/scheduler.py`](services/scheduler.py) — Reminders & scheduled jobs wired into Dynamic Island toasts.
- [`tools/`](tools/) — PC control, media transport, web search, memory, reminders, tasks, and Composio integrations.
- [`assets/fonts/`](assets/fonts/) — Bundled Apple SF Pro Display and SF Pro Text fonts.

---

*Coded using Google Gemini as a partner.*
