# The J.A.R.V.I.S Notch

A Dynamic Island–inspired **Windows Desktop Notch & AI Command Hub**, powered by a hybrid **Local LM Studio (Qwen3 8B) + Google Gemini / Gemma** brain with full Telegram, PC control, and Composio integrations.

Built with Python & PySide6 — featuring solid pitch-black (`#07080b`) borderless aesthetics, bundled Apple **SF Pro Display / SF Pro Text** typography, custom hand-drawn `QPainter` vector icons (zero Windows emojis), and silky spring animations.

---

## Highlights

### Dynamic Island Music Notch (Idle)
- **HiDPI Squircle Album Art**: Fetches high-resolution (`500×500`–`1000×1000`) album covers and pre-renders a supersampled PIL Lanczos-filtered squircle thumbnail (`|x|^{4.6} + |y|^{4.6} = 1`).
- **Live Song Marquee & Progress Hairline**: Displays the active song title (smooth 60fps marquee for long titles) with a sleek album-tinted live progress bar synced to Windows GSMTC.
- **Real-Time 60fps WASAPI Visualizer**: True decibel (`-42 dB .. 0 dB`) metering driven directly by Windows `IAudioMeterInformation` — zero fake boosting, colored with the album cover's extracted palette, and works even when Spotify is minimized to the system tray.
- **Smart Title-Bar Dodge**: Automatically tucks into a 3px micro-line when your mouse approaches browser tabs underneath, and pops open when you push your cursor to the top pixel edge.

### Wide DockBar PC Dashboard (`Ctrl + /` or Click Notch)
- **Now Playing Card**:
  - Continuous-curvature `78×78` squircle album art with custom vector music-note fallback.
  - Hand-drawn monochrome vector Previous / Play-Pause / Next controls.
  - Interactive live **Windows GSMTC progress & scrub/seek bar** that auto-syncs whenever the track changes.
  - **Auto-Hiding & 20s Magnetic Retraction**: Stays compact (`548×142`) when idle, expands (`790×146`) when audio plays, and keeps the Music Tab open for a 20-second grace window after pausing before smoothly pulling it back into the notch.
- **Calendar & Clock**: 12-hour live clock, 7-day calendar strip (with today & weekend highlights), and upcoming Calendar / Reminder pill.
- **System Monitor**: Real-time CPU, RAM, and Disk usage bars with Low / Normal / High status indicators and custom vector icons.
- **1:1 Windows Volume Slider**: Vertical volume bar bound directly to Windows CoreAudio `IAudioEndpointVolume` with a hand-drawn vector speaker glyph.

### Borderless Quick Chat, Voice Orb & Hybrid AI
- **Borderless Quick Chat**: Clean `#07080b` chat interface with a one-click top-bar model switcher (`LM Studio: Qwen3 8B`, `Gemma 4 26B`, `Gemini 3.5 Lite`, `Gemini 3.8`, etc.).
- **Voice Cloud Orb (`Ctrl + Alt + Space`)**: Two-way voice mode (optional; text replies are silent by default via `VOICE_CHAT_DEFAULT=false`).
- **Composio Universal Connections Hub**: One-click OAuth connections for Gmail, Google Calendar, Google Drive, and more directly from the notch.
- **Live Hot-Reload**: `main.py` watches `.py` and `.env` files, syntax-checks changes with `py_compile`, and hot-restarts JARVIS in place automatically.

---

## Requirements

- **Windows 10 / 11** (for WASAPI, CoreAudio `IAudioEndpointVolume`, and GSMTC media timeline)
- **Python 3.12+**
- **Optional**: [LM Studio](https://lmstudio.ai/) running locally (e.g. `qwen3-8b` on `http://127.0.0.1:1234/v1`) and/or a **Google Gemini API key**
- **Optional**: Telegram Bot token (for mobile companion control)

---

## Quick Start

### 1. Clone the repository

```powershell
git clone https://github.com/ayanzaman1818-cell/The-J.A.R.V.I.S-Notch.git
cd The-J.A.R.V.I.S-Notch
```

### 2. Create a virtual environment & install dependencies

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Configure `.env`

```powershell
Copy-Item .env.example .env
```

Edit `.env` with your preferred provider and optional integrations (never commit `.env`):

```dotenv
# Primary LLM (Local LM Studio or Google Gemini)
LLM_MODEL=lmstudio:qwen3-8b
LLM_FALLBACK_MODEL=gemini-3.5-flash-lite
LMSTUDIO_BASE_URL=http://127.0.0.1:1234/v1

# Google Gemini (Cloud / Fallback)
GEMINI_API_KEY=

# Telegram Companion Bot (Optional)
TELEGRAM_BOT_TOKEN=
OWNER_ID=

# Assistant Identity & Locale
USER_NAME=Sir
LOCATION=Dhaka
TIMEZONE=Asia/Dhaka

# Voice (Silent text replies by default)
VOICE_CHAT_DEFAULT=false
TTS_VOICE=en-GB-RyanNeural

# Optional Integrations
GROQ_API_KEY=
COMPOSIO_API_KEY=
```

### 4. Run JARVIS

```powershell
py main.py
```

To launch silently in the background on Windows login, use [start_jarvis_hidden.vbs](start_jarvis_hidden.vbs).

---

## Keyboard Shortcuts

| Shortcut | Action |
| :--- | :--- |
| `Ctrl + /` | Toggle the PC Dashboard Notch |
| `Ctrl + Alt + Space` | Open / Close Voice Cloud Orb Mode |
| `Esc` | Collapse the Notch back to Idle |

---

## Project Structure

- `main.py` — Entrypoint, Telegram bot runner, scheduler, and Live Hot-Reload file watcher
- `services/desktop_overlay.py` — Dynamic Island Notch UI, PC Dashboard, 60fps WASAPI visualizer, CoreAudio volume slider, and spring animations
- `services/media_timeline.py` — Zero-dependency Windows GSMTC live media timeline & seek bridge
- `services/llm.py` — Hybrid Local LM Studio + Google Gemini / Gemma router with smart tool pruning
- `tools/` — PC control, media transport, web search, memory, reminders, tasks, and Composio tools
- `assets/fonts/` — Bundled `SF Pro Display` and `SF Pro Text` fonts
