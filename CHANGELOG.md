# J.A.R.V.I.S. — Update Log & Release Notes

## v2.6.0 — Dynamic Island PC Dashboard, Live Album Cover & Audio Visualizer, Local LM Studio (Qwen3 8B) & Live Hot-Reload (2026-11-22)

### 1. Dynamic Island PC Home Dashboard & Live Music Visualizer (`services/desktop_overlay.py`, `tools/pc_control.py`)
- **Idle Notch Live Album Cover & WASAPI Audio Visualizer (`212×23`):**
  - When a song plays on Spotify or YouTube Music, the pitch-black idle notch expands horizontally (`212×23px`) to show the track's **rounded Album Cover** on the left, **`JARVIS`** in the center, and a **6-bar 30fps Live Audio Visualizer** on the right.
  - **Real-Time Windows CoreAudio Sync (`_WindowsAudioPeakMeter`):** Reads live master audio output amplitude directly from Windows WASAPI (`IAudioMeterInformation::GetPeakValue`) via zero-dependency `ctypes` COM calls so the bars bounce to the actual beat of the song.
  - **Blurry Album-Cover Visualizer Palette:** Fetches the track's album cover in the background (via Deezer / iTunes API), applies a Gaussian blur + saturation boost (`ImageFilter.GaussianBlur(radius=4)`), and colors the dancing visualizer bars and their soft outer glow with the blurred background colors while keeping the notch body pure black (`#07080b`).
- **Full PC Home Dashboard (`🏠` Home Tab):**
  - Clicking the notch (or pressing `Ctrl + /`) now opens directly to the **PC Home Dashboard**, while **JARVIS AI Chat** is tucked into its own dedicated **`✨` Sparkle Icon** in the top navigation bar.
  - **Now Playing Media Card:** Live track title, album artwork thumbnail, and working **`⏮` (Rewind/Previous)**, **`⏯` (Play/Pause)**, and **`⏭` (Fast-Forward/Next)** transport buttons powered by `KEYEVENTF_EXTENDEDKEY` + direct `WM_APPCOMMAND` (`0x0319`) posting to background Spotify windows.
  - **CPU & RAM Health in Words with Icons:** Zero-overhead `<0.1ms` Win32 `GetSystemTimes` and `GlobalMemoryStatusEx` cards displaying **CPU Usage** and **RAM Usage** in plain words with color-coded icons (`🟢 Low`, `🔵 Normal`, `🟠/🔴 High`).
  - **Important Events & Schedule Card:** Combines today's Google Calendar events (refreshed in the background via Composio), upcoming reminders (`⏰`), and open/high-priority tasks (`🔥` / `📌`).

### 2. Local LM Studio Engine (Qwen3 8B) & Continuous Self-Refresh Learning (`brain/gemini.py`, `tools/student.py`, `services/scheduler.py`)
- **LM Studio (`http://127.0.0.1:1234/v1`) & Qwen3 8B Support (`lmstudio:qwen3-8b`):**
  - Added smart tool pruning (`_select_relevant_tools_for_local`, capping schemas to `<=8` relevant tools) so local models never overflow a 4,096-token context window with all 99 tool definitions.
  - Added automatic `/no_think` injection for Qwen3 models for instant ~25–35 tok/sec responses, automatic pure-chat retry on `400 Bad Request` tool-template errors (without retrying on timeouts), and persistent `.env` saving when switching models in the Desktop Notch or `/model`.
- **Automatic Live Web Grounding & Casual Reading Loop:**
  - **Pre-Flight Web Grounding (`_extract_auto_grounding_topic`):** Automatically runs a 1-second DuckDuckGo search before drafting emails or answering questions about real-world topics/games/events (such as *Grand Theft Auto VI*) so local models never rely on outdated training cutoffs.
  - **Zero-Token Background Casual Reading (`run_casual_reading_refresh`):** Every 3 hours in the background (using 0% local LLM CPU so it never contends with live chat slots), JARVIS casually browses current world events, games, tech releases, and topics the user recently discussed, saving compact `Casual Reading` cards into its Second Brain for automatic prompt recall.

### 3. Optional Voice Replies & Live Code Hot-Reload (`main.py`, `services/telegram.py`, `tools/comms.py`)
- **Text-Only Replies by Default (`VOICE_CHAT_DEFAULT=false`):**
  - Disabled unsolicited voice messages after every text reply; two-way voice chat is now purely optional via `/voicemode`.
- **Live Hot-Reload File Watcher (`_start_hot_reload_watcher` in `main.py`):**
  - Watches all project `.py` files and `.env` in a lightweight background thread, validates syntax with `py_compile`, and automatically hot-restarts JARVIS in-place whenever a feature or fix is saved (while suppressing duplicate `"Jarvis online."` Telegram startup notifications on hot-reload).

---

## v2.5.0 — Dynamic Island Desktop Notch, Native Telegram VoIP Calling, Universal Composio Bridge & Autonomous Agentic Suite (2026-11-22)

### 1. Dynamic Island / Top-Notch Desktop Companion Overlay (`services/desktop_overlay.py`)
- **Center-Locked Motionless Fixed-Canvas Architecture (`580×540`):**
  - Anchored permanently at `x = (screen_width - 580) // 2, y = 0` so the OS window never shifts horizontally or slides in from the right.
  - Sub-pixel `QSizeF` interpolation (`OutBack` hover spring, `OutQuint` expand, `InOutQuint` collapse) combined with vertical glide (`-10px → 0px`), `QGraphicsOpacityEffect` cross-fades, and dynamic click-through `QRegion` masking.
- **3 Core Modes & Dedicated Global Hotkeys:**
  - **Idle Micro-Notch (`96×7px`) → Hover Pill (`162×20px`):** Expands slightly on hover with an emerald status dot and `JARVIS` micro-label.
  - **Quick Chat Mode (`Ctrl + /` or Click):** Animated 3D green wizard mascot, styled Markdown/code output viewer (`QTextBrowser`), inline `✅ Approve Pending Action` button, and a stadium input pill with `+` / `👁` Auto-See Screen toggle and voice mic trigger.
  - **Voice Cloud Orb Mode (`Ctrl + Alt + Space`):** 60fps volumetric 3D blue/white plasma cloud sphere orb wired to native Windows microphone capture, Groq Whisper / Gemini STT, and spoken JARVIS TTS replies.
  - **Connections Tab (`🔗` Icon — Universal Composio Bridge):** Scrollable directory of 16+ external apps (**Notion, Slack, Discord, GitHub, Spotify, Linear, Trello, Microsoft Outlook, YouTube, Figma, Jira, Todoist, Dropbox, Gmail, Google Calendar, Google Drive**) with one-click **`Connect`** buttons that generate live `"Composio wants to connect <App Name>"` OAuth links (`POST /api/v3/connected_accounts/link`) without editing `.env`.
- **Smart Ergonomics:**
  - **Click-Outside Auto-Close:** Clicking anywhere outside the expanded notch automatically collapses it back to the top micro-strip (unless locked open with the rotating **`📌` Pin** button).
  - **Clean `Ctrl+C` Shutdown & Stale Instance Guard:** Automatically terminates orphaned background `main.py` instances on startup and exits cleanly back to the PowerShell prompt on `Ctrl+C`.

---

### 2. Real Native 1-on-1 Telegram VoIP Calling & Free Web Voice Line (`services/telegram_call.py`, `services/twilio_webhook.py`, `setup_telegram_call.py`)
- **Native 1-on-1 Telegram VoIP Calls (`py-tgcalls` / `ntgcalls` + `Telethon`):**
  - Ring your personal Telegram app directly (`/call` or `call_me`) or call JARVIS's Telegram account from your phone/PC for a real two-way native Telegram voice call.
  - **QR Code Login (`py setup_telegram_call.py`):** Instant QR code pairing via Telegram's `Settings → Devices → Link Desktop Device` using Telegram Desktop's official public API credentials (no `my.telegram.org` form or SMS wait required).
  - **1ms High-Precision WebRTC PCM Streamer:** Decodes TTS MP3s to 48kHz stereo 16-bit PCM in memory via `miniaudio` and streams exact 10ms (`1920`-byte) WebRTC frames paced with Windows Multimedia Timer (`winmm.timeBeginPeriod(1)`).
  - **300ms Pre-Roll VAD & `fast_voice` Routing:** Captures incoming call audio with a 300ms pre-roll buffer, triggers transcription after `0.62s` of silence, and routes call turns directly through `gemini-3.5-flash-lite` (`MINIMAL` thinking) for sub-second voice turnaround.
- **100% Free Cloudflare-Tunneled Web Voice Call UI & Two-Way Twilio Webhook (`services/twilio_webhook.py`):**
  - Auto-starts a background Cloudflare HTTPS Quick Tunnel (`cloudflared`) on port `8787`.
  - Provides both a full-screen mobile/desktop browser voice call UI (`/call`) and a two-way `<Gather input="speech">` + `<Play>` Twilio voice webhook (`/twilio/voice`).

---

### 3. Background Windows Computer-Use & Screen Vision (`tools/desktop.py`, `tools/pc_control.py`, `tools/registry.py`)
- **Off-Screen Background GUI Automation (Zero Focus Stealing):**
  - `see_screen`, `desktop_click`, `desktop_type`, `desktop_hotkey`, `desktop_scroll`, and `pc_launch_app` execute **in the background by default** (`background=True`) using Win32 `PrintWindow(..., PW_RENDERFULLCONTENT=2)`, `PostMessageW` (`WM_LBUTTONDOWN`/`UP`, `WM_CHAR`, `WM_KEYDOWN`, `WM_MOUSEWHEEL`), and `SW_SHOWNOACTIVATE` without moving the user's physical mouse cursor or stealing active window focus.
  - **Mid-Loop Multimodal Vision:** `see_screen` attaches resized JPEG bytes (`<=1600px`, `quality=80`) directly into Gemini's tool response history (`ToolContext.attach_media()`) so JARVIS can visually verify UI state mid-task.
  - **Strict Safety Guards:** Enforces `pyautogui.FAILSAFE = True` at `(0, 0)`, blocks password/credential typing (`_looks_like_credential`), and requires `pending_actions` confirmation tokens before form submissions (`press_enter=True`) or destructive hotkeys.
- **Workstation Automation Suite (`tools/pc_control.py`):**
  - Added smart window inspection (`pc_inspect_window`), window management (`pc_list_windows`, `pc_window_control`), top process manager with one-tap `🛑 Kill` buttons (`/top`, `pc_top_processes`, `pc_kill_process`), power/display/Caffeine controls (`pc_power_control`, `pc_stay_awake`), direct URL-to-PC downloader (`/download`, `pc_download_url`), Downloads auto-organizer (`pc_organize_downloads`), network/dev diagnostics (`pc_network_info`, `pc_run_diagnostic`), and physical PC notifications/speaker TTS (`pc_notify`, `pc_speak_aloud`).

---

### 4. Universal Composio Integration Bridge (`tools/integrations.py`)
- **Zero-`.env` Dynamic Account Discovery:**
  - `get_connected_toolkits_sync()` and `authorize_toolkit_sync()` query Composio v3 (`connected_accounts.list` and `connected_accounts.link`) live and cache active `ca_xxx` account IDs in SQLite preferences automatically.
  - Added `composio_list_connections`, `composio_connect_app`, and `composio_execute_action` so JARVIS can connect and run actions across 250+ external apps on demand.

---

### 5. Autonomous Student Engine & Agentic Workflows (`tools/student.py`, `tools/agentic.py`, `brain/agent.py`)
- **Autonomous Student / Self-Learning Engine (`/study`, `tools/student.py`):**
  - JARVIS studies topics on the web autonomously every 6 hours (`AUTO_STUDY_INTERVAL_HOURS=6`), running silently in the background (`AUTO_STUDY_NOTIFY=false` by default).
  - Auto-discovers new topics from user memories, open tasks, and live Hacker News stories when the syllabus is empty, progresses across 5 Mastery Levels, writes structured **Student Study Notes + Self-Test Flashcards** into the Second Brain vault, and queues its own next curiosity question (`NEXT_SUBTOPIC`).
- **Deep Research, Goal Planner & Scheduled Agent Jobs (`tools/agentic.py`):**
  - `deep_research` (`/research <topic>`): Parallel multi-angle web search and page reading with cited Second Brain report archiving.
  - `plan_goal` (`/plan <goal>`): Decomposes high-level goals into prioritized SQLite tasks and a master roadmap note.
  - `schedule_agent_job` (`/automate`): Standing background orders (`daily`, `weekdays`, `weekly`, `monthly`) that wake JARVIS up to execute live tools and deliver synthesized reports.
- **Parallel Multi-Tool ReAct & Proactive RAG Recall (`brain/agent.py`):**
  - Executes multiple independent tool calls concurrently via `asyncio.gather` and injects top-matching Second Brain notes into the system prompt on every turn.

---

### 6. Model Routing, Adaptive Thinking Mode & Reliability (`brain/gemini.py`, `brain/history.py`, `services/backup.py`)
- **Gemma 4 26B (`gemma-4-26b-a4b-it`) + Gemini 3.5 Flash-Lite (`gemini-3.5-flash-lite`) Routing:**
  - Configured `gemma-4-26b-a4b-it` as primary model with automatic `thought=True` reasoning-part filtering in `response_text()` (`brain/history.py`) so internal chain-of-thought is never spoken or printed aloud.
  - Automatic fallback to `gemini-3.5-flash-lite` on `503`/`504`, timeouts, and Gemma `429` free-tier TPM rate limits.
- **Adaptive Thinking Mode (`_thinking_config_for`):**
  - Uses `thinking_level="MINIMAL"` on `gemini-3.5-flash-lite` and `thinking_level="LOW"` on `gemini-3.8-flash` for routine turns, background health probes, and post-tool summarization steps, scaling up to `MEDIUM` only for complex reasoning queries.
- **Exponential Circuit Breaker, Secret Redaction & Online SQLite Backups:**
  - Exponential primary model cooldown (`3m → 60m`) with off-path background recovery probing (`_maybe_start_probe`), Fish Audio circuit breaker, full bot-token/API-key redaction in tracebacks, nightly online SQLite backups (`/backup`), and live `/health` diagnostics.
