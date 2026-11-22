# JARVIS

A personal AI assistant for Telegram, built with Python and powered by
Google Gemini. JARVIS is designed for natural-language conversations,
tool execution, persistent memory, reminders, tasks, and external
integrations.

> This README describes the Gemini-based setup discussed for this
> project. Your local `config.py` and integration modules are
> authoritative: if your current code uses different
> environment-variable names, update `.env` and this README to match.

## Requirements

-   Windows 10/11, Linux, or macOS
-   Python 3.12 recommended
-   A Telegram account
-   A Google account and Gemini API key
-   Internet access

Some optional integrations require separate accounts and credentials.

## Installation

### 1. Get the project

Clone the repository, or download and extract its ZIP:

``` bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git
cd YOUR_REPOSITORY
```

### 2. Create a virtual environment

**Windows PowerShell:**

``` powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, use the environment's Python directly:

``` powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**Linux/macOS:**

``` bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

``` bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If the project uses Playwright for browser automation, install Chromium:

``` bash
playwright install chromium
```

### 4. Create `.env`

**Windows PowerShell:**

``` powershell
Copy-Item .env.example .env
```

**Linux/macOS:**

``` bash
cp .env.example .env
```

Open `.env` and add your own credentials. Never commit `.env` to Git.

## Configuration

Example Gemini-based `.env`:

``` dotenv
# Required: Telegram
TELEGRAM_BOT_TOKEN=
OWNER_ID=

# Required: Google Gemini
GEMINI_API_KEY=

# Gemini models
LLM_MODEL=gemini-3.8-flash
LLM_FAST_MODEL=gemini-3.8-flash
LLM_DEEP_MODEL=gemini-3.8-flash
LLM_FALLBACK_MODEL=gemini-3.5-flash-lite

# Gemini reliability settings
LLM_TIMEOUT_SECONDS=45
GEMINI_RETRY_ATTEMPTS=3
GEMINI_FALLBACK_RETRIES=2

# Assistant identity and locale
USER_NAME=Sir
LOCATION=Dhaka
TIMEZONE=Asia/Dhaka
BRIEFING_TIME=06:40
EVENING_BRIEFING_TIME=21:30

# Proactive background health alerts
PROACTIVE_ALERTS=true
BATTERY_ALERT_THRESHOLD=20

# Optional: Groq Whisper voice transcription
GROQ_API_KEY=

# Voice Engine: Fish Audio (Free custom Paul Bettany clone)
FISH_API_KEY=
FISH_MODEL_ID=
FISH_MODEL=s2.1-pro-free

# Voice Engine: ElevenLabs (Optional custom/community clone)
ELEVENLABS_API_KEY=
ELEVENLABS_VOICE_ID=onwK4e9ZLuTAKqWW03F9
ELEVENLABS_MODEL=eleven_multilingual_v2

# Voice Engine: Microsoft Edge-TTS (Free unlimited fallback with Marvel prosody)
TTS_VOICE=en-GB-RyanNeural
TTS_RATE=+5%
TTS_PITCH=-4Hz
VOICE_CHAT_DEFAULT=false

# Optional: Composio integrations (Gmail, Drive, Calendar)
COMPOSIO_API_KEY=
COMPOSIO_USER_ID=
COMPOSIO_GMAIL_ACCOUNT=
COMPOSIO_GOOGLEDRIVE_ACCOUNT=
COMPOSIO_GOOGLECALENDAR_ACCOUNT=

# Optional: Twilio phone calls
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_NUMBER=
MY_PHONE=
TWILIO_VOICE=alice
TWILIO_WEBHOOK_URL=

# Optional: Briefing delivery options
BRIEFING_VOICE=true
BRIEFING_CALL=false

# Quiet-hours window & local storage paths
QUIET_HOURS_START=22:00
QUIET_HOURS_END=07:00
INBOX_DIR=inbox
KNOWLEDGE_LOCAL_DIR=notes
KNOWLEDGE_DRIVE_FOLDER=
```

This is a template, not a guarantee that every variable is enabled in
every revision. Check `config.py`, `main.py`, `services/voice.py`, and
tool modules in the version you are running. Remove unused
settings and add any additional variables required by your
implementation.

### Environment variable reference

  -----------------------------------------------------------------------------------
  Variable                            Required?               Purpose
  ----------------------------------- ----------------------- -----------------------
  `TELEGRAM_BOT_TOKEN`                Yes                     Authenticates the
                                                              Telegram bot

  `OWNER_ID`                          Yes for                 Numeric Telegram user
                                      owner-restricted use    ID authorised for
                                                              owner-only functions

  `GEMINI_API_KEY`                    Yes                     Authenticates Google
                                                              Gemini requests

  `LLM_MODEL`                         Depends on config       Default model setting

  `LLM_FAST_MODEL`                    Depends on config       Model for
                                                              routine/faster requests

  `LLM_DEEP_MODEL`                    Depends on config       Model for more involved
                                                              requests, if supported

  `LLM_FALLBACK_MODEL`                Recommended             Backup model for
                                                              eligible primary-model
                                                              failures

  `LLM_TIMEOUT_SECONDS`               Optional                Timeout for an
                                                              individual Gemini
                                                              request

  `GEMINI_RETRY_ATTEMPTS`             Optional                Primary-model retry
                                                              count

  `GEMINI_FALLBACK_RETRIES`           Optional                Fallback-model retry
                                                              count

  `USER_NAME`                         Optional                Name/title used when
                                                              addressing the owner

  `LOCATION`                          Optional                Default location for
                                                              weather and local
                                                              context

  `TIMEZONE`                          Optional                Time zone for
                                                              scheduling and
                                                              time-aware features

  `BRIEFING_TIME`                     Optional                Local time for morning
                                                              briefing (HH:MM)

  `EVENING_BRIEFING_TIME`             Optional                Local time for evening
                                                              reflection (HH:MM)

  `PROACTIVE_ALERTS`                  Optional                Enable PC battery, RAM,
                                                              and disk space alerts

  `BATTERY_ALERT_THRESHOLD`           Optional                Battery % to alert on
                                                              when unplugged (def 20)

  `GROQ_API_KEY`                      Optional                Groq API key for
                                                              Whisper transcription

  `FISH_API_KEY`                      Optional                Fish Audio API key for
                                                              cloned JARVIS voice

  `FISH_MODEL_ID`                     Optional                Fish Audio voice model
                                                              ID for cloned voice

  `FISH_MODEL`                        Optional                Fish Audio model routing
                                                              (default `s2.1-pro-free`)

  `ELEVENLABS_API_KEY`                Optional                ElevenLabs API key for
                                                              voice clone synthesis

  `ELEVENLABS_VOICE_ID`               Optional                ElevenLabs voice ID

  `TTS_VOICE`                         Optional                Edge-TTS voice name
                                                              (default `en-GB-RyanNeural`)

  `TTS_RATE` / `TTS_PITCH`            Optional                Edge-TTS Marvel prosody
                                                              tuning (`+5%`, `-4Hz`)

  `VOICE_CHAT_DEFAULT`                Optional                Default two-way voice
                                                              mode (`true`/`false`)

  `COMPOSIO_API_KEY`                  Optional                Composio project API
                                                              key

  `COMPOSIO_USER_ID`                  Integration-dependent   Composio app-user ID,
                                                              if required

  `COMPOSIO_GMAIL_ACCOUNT`            Optional                Connected Gmail account
                                                              identifier

  `COMPOSIO_GOOGLEDRIVE_ACCOUNT`      Optional                Connected Drive account
                                                              identifier

  `COMPOSIO_GOOGLECALENDAR_ACCOUNT`   Optional                Connected Calendar
                                                              account identifier

  `TWILIO_ACCOUNT_SID`                Optional                Twilio account
                                                              identifier

  `TWILIO_AUTH_TOKEN`                 Optional                Twilio authentication
                                                              secret

  `TWILIO_NUMBER`                     Optional                Twilio phone number

  `MY_PHONE`                          Optional                Verified destination
                                                              phone number

  `TWILIO_VOICE`                      Optional                Twilio voice selection

  `TWILIO_WEBHOOK_URL`                Optional                Twilio webhook for
                                                              two-way speech gather

  `BRIEFING_VOICE`                    Optional                Enables voice note
                                                              briefings

  `BRIEFING_CALL`                     Optional                Enables phone-call
                                                              briefings

  `QUIET_HOURS_START` /               Optional                Quiet-hours window
  `QUIET_HOURS_END`                                           (e.g. 22:00 to 07:00)

  `INBOX_DIR`                         Optional                PC folder for `/save`
                                                              file downloads

  `KNOWLEDGE_LOCAL_DIR`               Optional                Folder for Second Brain
                                                              local markdown notes
  -----------------------------------------------------------------------------------

Use `true` or `false` for boolean values. Follow the parsing rules in
your local `config.py`.

## Where to get API keys

### Telegram bot token

1.  Open [@BotFather](https://t.me/BotFather) in Telegram.
2.  Send `/newbot` and follow the prompts.
3.  Copy the token BotFather provides.
4.  Put it in `TELEGRAM_BOT_TOKEN`.

Keep this token private. Anyone with it may be able to control your bot.

### Telegram owner ID

1.  Open [@userinfobot](https://t.me/userinfobot).
2.  Press Start.
3.  Copy the numeric Telegram user ID it returns.
4.  Put it in `OWNER_ID`.

The owner ID is not the bot token. It identifies the Telegram account
authorised by Jarvis.

### Google Gemini API key

1.  Open [Google AI Studio](https://aistudio.google.com/).
2.  Sign in with your Google account.
3.  Open the API key section and create a key.
4.  Put it in `GEMINI_API_KEY`.

Check Google's current model availability, quotas, and billing
requirements. Model IDs may change. Use IDs supported by your account
and current implementation. A fallback model cannot guarantee
availability during account-wide quota restrictions or provider outages.

### Groq API key (optional: voice transcription)

1.  Open [Groq Console](https://console.groq.com/).
2.  Sign in and create an API key.
3.  Put it in `GROQ_API_KEY`.

In this setup, Groq is for Whisper speech-to-text if the installed voice
handler uses it; it is not the primary chat provider.

### Composio API key (optional: Gmail, Drive, Calendar)

1.  Open [Composio](https://app.composio.dev/).
2.  Create or select a project.
3.  Open the project's API key settings.
4.  Create the Project API Key type expected by the installed SDK (the
    earlier integration used an `ak_` project key).
5.  Connect the Google services you want Jarvis to access.
6.  Put the connected account identifiers in the matching
    `COMPOSIO_*_ACCOUNT` variables.
7.  Set `COMPOSIO_USER_ID` only if your Composio project requires it.

Use the key type expected by your installed Composio integration. A
Connect/MCP consumer key may not work with code that expects a project
API key. Grant only the permissions required.

### Twilio credentials (optional: phone calls)

1.  Open [Twilio](https://www.twilio.com/) and create an account.
2.  Obtain a Twilio phone number.
3.  Find the Account SID and Auth Token in the Twilio Console.
4.  Configure the Twilio number and any verified destination number.
5.  Add the values to `TWILIO_*` and `MY_PHONE`.

Trial accounts have restrictions, including verified destination numbers
and trial announcements. Check current terms and regional availability.

## Run JARVIS

From the project root, with the virtual environment activated:

``` powershell
py main.py
```

If `py` is unavailable:

``` bash
python main.py
```

Keep the process running while you want the bot and scheduled jobs to
operate. Closing the terminal or shutting down the computer stops a
locally hosted bot.

Open Telegram, find the bot created with BotFather, and send `/start`
(or the startup command supported by your version).

Press `Ctrl+C` in the terminal to stop Jarvis.

## Features & Commands

### Core & Advanced Capabilities

- **Marvel JARVIS Voice & Sound Pack:** 
  - **Fish Audio Custom Clone (`fish.audio`):** Full integration with Fish Audio using the free `s2.1-pro-free` model for custom cloned MCU JARVIS voice synthesis.
  - **Bundled `openpeon-jarvis` Sound Pack:** 66 authentic 44.1kHz studio MP3 voice clips from the MCU. Plays the genuine Paul Bettany *"Welcome home, sir."* greeting upon startup, automatically matches canon quotes (*"Congratulations, sir. You have created a new element."*, *"As always, sir, a great pleasure watching you work."*), and provides an interactive Telegram soundboard.
  - **Tuned Edge-TTS Fallback:** Free, unlimited Microsoft `en-GB-RyanNeural` calibrated with custom prosody (`rate=+5%`, `pitch=-4Hz`) for a crisp British AI butler cadence.
  - **ElevenLabs Ready:** Includes master concatenated audio samples in `assets/elevenlabs_samples/` and a 1-command cloning helper script (`py clone_jarvis_voice.py`).
- **Two-Way Telegram Voice Chat Mode:** Toggle continuous two-way voice chat (`/voicemode`) where JARVIS replies with spoken voice notes aloud for every message, with voice presets (`marvel`, `sonia`, `christopher`, `aria`).
- **Visual Charts & Financial Intelligence:**
  - **Expense Analytics Charts:** Generate dark-mode PNG category breakdown and daily spending trend charts directly in Telegram.
  - **Habit Streak Heatmaps:** Generate visual 14-day consistency heatmaps and active streak badges.
  - **Monthly Category Budgets:** Set spending caps per category (`set_budget`) with automatic `80%` warning and `100%` exceeded threshold alerts.
- **Proactive PC Health & Background Monitor:** Background APScheduler watchdog checks PC vitals every 10 minutes and sends proactive Telegram alerts if your laptop battery drops below 20% while unplugged, or if RAM/disk exceeds 93%.
- **Daily Morning & Evening Debriefings:**
  - **Morning Briefing (`/briefing`):** Weather forecast, appointments, unread VIP emails, pending reminders, 7-day finances, and top tech news.
  - **Evening Wrap-Up (`/evening`):** End-of-day reflection at 9:30 PM summarizing checked-off tasks, today's logged expenses & habits, and tomorrow's upcoming events.
- **Desktop Companion Floating Overlay & Computer-Use ([services/desktop_overlay.py](file:///g:/jarvis/services/desktop_overlay.py), [tools/desktop.py](file:///g:/jarvis/tools/desktop.py)):**
  - **Global Hotkey Translucent Command Bar (`DESKTOP_OVERLAY_ENABLED=true`):** Toggle an always-on-top floating command bar from anywhere on your PC with `Ctrl+Alt+Space` or `Ctrl+/`. Shares the exact same Gemini Brain, SQLite memory, tasks, and tools as your Telegram chat (`OWNER_ID`).
  - **Auto-See Screen & Push-to-Talk Voice:** Includes a single-line prompt (`Enter` to submit, `Esc` to hide), an **👁 Auto-See Screen** switch that attaches a live monitor screenshot automatically, a **🎙 Push-to-Talk** microphone button wired to Groq Whisper STT + TTS playback, and an inline `✅ Approve Pending Action` button.
  - **Real-Time Screen Vision (`see_screen`):** Captures the primary monitor or active window via `mss` and `Pillow` as a resized JPEG (max 1600px width, quality 80) and feeds the raw image bytes directly into Gemini's multimodal pipeline mid-task to verify UI state.
  - **Native GUI Computer-Use Automation (`desktop_list_windows`, `desktop_focus_window`, `desktop_click`, `desktop_type`, `desktop_hotkey`, `desktop_scroll`):** Built on `pyautogui` (`FAILSAFE=True` at `(0, 0)`, `PAUSE=0.15`) and `pygetwindow`, with strict credential/password blocking and `pending_actions` confirmation tokens before form submissions, destructive hotkeys, or system settings changes.
- **PC Desktop Remote Control & Workstation Automation ([tools/pc_control.py](file:///g:/jarvis/tools/pc_control.py)):**
  - **Smart Window Capture & AI Screen Inspector (`pc_inspect_window`, `pc_screenshot`):** Automatically minimizes Telegram for 300ms when capturing on the same PC (or targets a specific background window by title like `"Visual Studio Code"`) and pipes the screenshot directly into Gemini Vision to debug errors on your screen.
  - **Window & Process Manager (`/top`, `pc_list_windows`, `pc_window_control`, `pc_top_processes`, `pc_kill_process`):** List or focus/minimize/maximize/close open desktop windows, view top RAM-heavy processes, and terminate frozen processes with one-tap `🛑 Kill` buttons.
  - **Power, Display & Keep-Awake Automation (`pc_power_control`, `pc_stay_awake`):** Turn off monitors (`display_off`), sleep, hibernate, schedule/cancel shutdown or restart, and toggle Windows Caffeine / Keep-Awake mode so your PC never sleeps during long renders or downloads.
  - **Direct URL-to-PC Downloader & Downloads Organizer (`/download`, `pc_download_url`, `pc_organize_downloads`):** Download any URL directly into your PC's `Downloads` folder and auto-sort cluttered files into `Images/`, `Documents/`, `Archives/`, `Installers/`, `Code/`, and `Media/`.
  - **Workstation Diagnostics, Network Info & Physical PC Alerts (`pc_network_info`, `pc_run_diagnostic`, `pc_notify`, `pc_speak_aloud`):** Inspect LAN IP & Wi-Fi SSID/signal, run allowlisted dev diagnostics (`git_status`, `docker_ps`, `gpu_nvidia_smi`, `netstat_listening`), pop native desktop notifications, or speak aloud through your PC's physical speakers.
  - **Two-Way Clipboard Sync:** Read PC clipboard text into Telegram or copy text/URLs directly to your PC clipboard (`/clip <text>`).
  - **Focus & Environment Modes:** Activate desktop presets (`/pc` or natural language): `work` (opens VS Code + Spotify), `night` (mutes media + locks PC), `chill` (Spotify playback), and `meeting` (instant mute).
  - **Remote File Transfers & Inbox:** List files in Downloads/Desktop/Documents, send PC files to Telegram, or send any photo/document to Telegram with caption `/save` to store it directly in `inbox/`.
  - **Workstation Lock & Live Screenshots:** Capture multi-monitor desktop screenshots or lock Windows workstation screen immediately (`/lock`).
- **Second Brain Knowledge Vault (RAG & Read-Later):**
  - **Semantic + Keyword Hybrid Search:** Search saved notes, code snippets, and local markdown files in `notes/` using Gemini vector embeddings and SQLite.
  - **"Read Later" Web Article Archiver:** Forward any article URL (`archive_article`) to extract clean readable text, generate an executive summary, and save it in your Second Brain vault.
- **Autonomous Agentic Workflows ([tools/agentic.py](file:///g:/jarvis/tools/agentic.py), [tools/student.py](file:///g:/jarvis/tools/student.py), [brain/agent.py](file:///g:/jarvis/brain/agent.py)):**
  - **Autonomous Student / Self-Learning Engine (`/study`, `enroll_study_topic`, `study_next_lesson`):** JARVIS continuously learns on the web like a student. In background study sessions (`AUTO_STUDY_INTERVAL_HOURS=6`), JARVIS either progresses through enrolled subjects across 5 Mastery Levels (`Level 1: Foundations` → `Level 5: Frontier Research`) or **autonomously discovers new topics** from your interests, tasks, and live Hacker News frontiers. Each lesson searches the web, reads full articles, writes structured **Student Study Notes + Self-Test Flashcards** into your Second Brain vault, and queues its own next curiosity question (`NEXT_SUBTOPIC`).
  - **Deep Research Agent (`/research <topic>`, `deep_research`):** Fans out across multiple search angles concurrently, reads authoritative web pages in parallel, synthesizes a cited executive research report, and archives it to your Second Brain vault.
  - **Goal & Project Decomposition Planner (`/plan <goal>`, `plan_goal`):** Decomposes high-level goals into prioritized milestones with due dates, populates your SQLite task inbox automatically, and saves a master roadmap note.
  - **Scheduled Autonomous Background Jobs / Standing Orders (`/automate`, `schedule_agent_job`):** Schedule future or recurring (`daily`, `weekdays`, `weekly`, `monthly`) prompts where JARVIS wakes up autonomously, executes live tools (web search, PC status, Hacker News, tasks, weather), and messages you the synthesized report.
  - **Parallel Multi-Tool ReAct Execution & Proactive RAG Recall:** Executes multiple independent tool calls concurrently via `asyncio.gather` and automatically injects top-matching Second Brain notes into the system prompt on every turn.
- **Automated Price-Drop Watchdog:** Monitor product URLs (`watch_price`, `/watch`) and receive instant Telegram deal alerts when prices drop to or below your target threshold.
- **Pomodoro & Deep Focus Timers (`/focus`):** Start quick 25m Pomodoro, 50m Deep Work, or 5m Break countdown sessions (`start_focus_timer`) with persistent SQLite reminder delivery.
- **One-Tap CSV & Markdown Data Export (`/export`):** Export your expenses, tasks, habits, or Second Brain knowledge vault notes directly in Telegram as downloadable `.csv` or `.md` files (`export_data`).
- **Rich Recurring Reminders:** Schedule persistent reminders repeating `daily`, `weekdays` (Monday–Friday), `weekly`, or `monthly` (with automatic month-end clamping).
- **Automated Online SQLite Backups & Live Health Diagnostics (`/backup`, `/health`):** Nightly online SQLite database backups (`03:30` local time) with `PRAGMA integrity_check` verification and rolling retention, plus `/health` live diagnostics for model latency, adaptive circuit-breaker state, voice status, and network metrics.
- **Vision & Document Analysis:** Send photos, receipts, or PDF/text files in Telegram with an optional caption to analyze bills, debug code screenshots, or summarize research papers.
- **YouTube Video Summarizer:** Paste any YouTube link (including Shorts) to extract timestamped transcripts and generate comprehensive summaries.
- **Spotify & Desktop Media Control:** Inspect currently playing track, toggle play/pause, skip tracks, adjust volume, or search and launch tracks in Spotify.
- **Interactive Inline Buttons:** One-tap inline Telegram buttons for task completion, reminder snooze (`+10m`), action approval, PC controls, voice presets, and visual chart generation.

### Telegram Slash Commands

| Command | Description |
| :--- | :--- |
| `/start`, `/help` | Display welcome guide and available commands |
| `/voicemode`, `/voice` | Toggle two-way voice chat mode, test voices, or play MCU sound clips |
| `/status` | Check system health, active voice engine, models, and database counts |
| `/health`, `/diag` | Live diagnostics: uptime, model circuit breaker & latency, Fish Audio, backups |
| `/backup [send]` | Run an online SQLite backup now (`send` delivers the `.db` file to Telegram) |
| `/model` | Inspect active Gemini models and circuit-breaker status |
| `/briefing` | Generate and deliver your on-demand morning briefing |
| `/evening` | Generate and deliver your 9:30 PM evening reflection & wrap-up |
| `/tasks` | List open tasks with one-tap `✅ Done` inline buttons |
| `/plan <goal>` | Autonomously decompose a goal into tasks and save a Second Brain roadmap |
| `/research <topic>` | Run autonomous multi-query web research and archive the cited report |
| `/study [now\|quiz\|<subject>]` | Autonomous Student Mode: self-study on the web, syllabus, & flashcard review (`/learn`) |
| `/automate [when \| prompt]` | Schedule or list standing autonomous background agent jobs (`/jobs`) |
| `/focus [mins] [task]` | Start a Pomodoro / deep focus countdown timer (25m, 50m, 5m presets) |
| `/reminders` | List pending reminders with one-tap `❌ Cancel` inline buttons |
| `/expenses` | View 30-day expense breakdown with `📊 Generate Expense Chart` button |
| `/habits`, `/stats` | View habit streaks and 7-day stats with `🔥 Habit Streak Chart` button |
| `/notes [query]`, `/brain` | Search or list notes and articles in your Second Brain knowledge vault |
| `/export [dataset]` | Export `expenses`, `tasks`, `habits`, or `notes` as a CSV or Markdown file |
| `/watch` | List and manage active product price-drop watches |
| `/pc`, `/pcstatus` | Check live PC CPU, RAM, disk, battery, and quick remote control buttons |
| `/top` | List top memory-consuming PC processes with one-tap `🛑 Kill` inline buttons |
| `/download <url>` | Download any public file URL directly to your PC's `Downloads` folder |
| `/clip [text]` | Read PC clipboard, or copy `<text>` to your PC clipboard |
| `/lock` | Immediately lock your Windows workstation (owner only) |
| `/memory` | Inspect stored long-term memories and user preferences |py 
| `/call <message>` | Ring your phone with a spoken message via Twilio (owner only) |
| `/reset`, `/clear` | Clear current short-term conversation context |
| `/myid` | Display your numeric Telegram user ID |

## Voice Engine Setup

JARVIS features a multi-tiered voice architecture:

### 1. Fish Audio (`fish.audio` — Recommended for Cloned Voice)
Fish Audio offers free custom voice cloning and free API inference via their `s2.1-pro-free` model.
1. Create an account at [fish.audio](https://fish.audio/) and create a voice clone of JARVIS.
2. In your profile, create an **API Key**.
3. Copy your voice **Model ID** from the model URL (e.g. `https://fish.audio/m/<model_id>/`).
4. Add to `.env`:
   ```dotenv
   FISH_API_KEY=your_fish_audio_api_key
   FISH_MODEL_ID=your_fish_model_id
   FISH_MODEL=s2.1-pro-free
   ```

### 2. Built-in `openpeon-jarvis` Sound Pack (100% Free, Zero Config)
Bundled in `assets/openpeon_jarvis/`. Contains 66 high-quality 44.1kHz MCU Paul Bettany clips. Automatically plays on startup and matches canon phrases with zero latency.

### 3. Microsoft Edge-TTS (100% Free, Unlimited Fallback)
If no cloud API keys are provided, JARVIS automatically uses Microsoft Edge Neural TTS (`en-GB-RyanNeural`) tuned with Paul Bettany prosody (`rate=+5%`, `pitch=-4Hz`). No keys or accounts required.

### 4. ElevenLabs (Optional)
If you have an ElevenLabs key, add `ELEVENLABS_API_KEY` and `ELEVENLABS_VOICE_ID` to `.env`. Master training samples are provided in `assets/elevenlabs_samples/` or run `py clone_jarvis_voice.py`.

## Optional integrations

### Gmail, Google Drive, and Calendar

Configure Composio and connect the relevant Google accounts. Available
actions depend on the tools implemented in the current `tools/` suite.

Keep read-only operations read-only. Actions that send messages, create
events, delete data, or share files require explicit confirmation (`/confirm`).

### Voice

Set `GROQ_API_KEY` for Whisper voice transcription, or use Gemini multimodal audio understanding as an automatic fallback.

### Scheduled briefings and reminders

Set `TIMEZONE`, `BRIEFING_TIME`, and `EVENING_BRIEFING_TIME`. The application must
remain running for in-process APScheduler jobs to execute. Use an
always-on computer or managed server for continuous operation.

## Data and storage

The SQLite storage layer may store reminders, tasks, preferences,
memories, and audit data.

-   Back up the database before major upgrades.
-   Do not delete it unless you intend to remove its stored data.
-   Do not commit private database files.
-   Check `storage.py` and the current configuration for the actual
    database filename and location.

## Troubleshooting

### `GEMINI_API_KEY` is missing

Check that `.env` exists in the project root, the key is set,
`config.py` loads `.env`, and Jarvis was restarted after the change.

### Gemini returns HTTP 503

A 503 means the service is temporarily unavailable. It is not
automatically an invalid-key error. Retries and fallback may help, but
cannot guarantee uninterrupted service.

### Gemini request times out

Check your internet connection and provider status. Review the timeout
and retry logs. Avoid excessively long timeouts that leave Telegram
requests waiting.

### HTTP 429

A 429 indicates a rate limit or quota restriction. Check the Gemini
project's quota and billing status. Repeated retries usually will not
resolve an exhausted quota.

### Telegram bot does not respond

Check the bot token, confirm the process is running, verify `OWNER_ID`
if owner restrictions are enabled, and ensure another process is not
polling with the same token.

### Missing Python module

Activate the correct virtual environment and run:

``` powershell
python -m pip install -r requirements.txt
```

### Playwright browser missing

``` powershell
playwright install chromium
```

## Security

-   Never commit `.env`, API keys, bot tokens, authentication files, or
    private databases.
-   Keep `.env.example` limited to blank values or obvious placeholders.
-   Rotate credentials immediately if they appear in logs, screenshots,
    Git history, or public repositories.
-   Do not log full Telegram Bot API URLs: bot tokens are embedded in
    those URLs.
-   Restrict owner-only commands and sensitive integrations.
-   Require confirmation before consequential actions.
-   Treat webpage content and external documents as untrusted input.

## License

Add the license selected for this repository. Until a license is
included, do not assume others have permission to reuse, modify, or
distribute the code.
