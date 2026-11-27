# The J.A.R.V.I.S Notch

A Dynamic Island–inspired **Windows Desktop Notch & AI Command Hub**, powered by our own custom fine-tuned **[JARVIS-Qwen-7B (`AyanZaman19/jarvis-qwen-7b-gguf`)](https://huggingface.co/AyanZaman19/jarvis-qwen-7b-gguf)** local model via LM Studio + **Google Gemini / Gemma** cloud fallback, with full Telegram, PC control, and Composio integrations.

Built with Python & PySide6 — featuring solid pitch-black (`#07080b`) borderless aesthetics, bundled Apple **SF Pro Display / SF Pro Text** typography, custom hand-drawn `QPainter` vector icons (zero Windows emojis), and silky spring animations.

---

## Custom Fine-Tuned Model: JARVIS-Qwen-7B

We trained and fine-tuned our own dedicated local LLM specifically for **The J.A.R.V.I.S Notch** using QLoRA + Unsloth and published both the model and dataset openly under **Apache 2.0**:

- **Model (4-bit `Q4_K_M` GGUF for LM Studio):** [`AyanZaman19/jarvis-qwen-7b-gguf`](https://huggingface.co/AyanZaman19/jarvis-qwen-7b-gguf)
- **Custom Dataset:** [`AyanZaman19/jarvis-notch-dataset`](https://huggingface.co/datasets/AyanZaman19/jarvis-notch-dataset)
- **Built-In Everywhere:** Available as the default 1-click preset (**`JARVIS Qwen 7B (Custom)`**) in both the **Notch Top-Bar Model Switcher** and the **Telegram `/model` menu**, with native tool calling, live web grounding, and dry British J.A.R.V.I.S. wit.

---

## Highlights

### Dynamic Island Idle Notch & Live Pop-Outs
- **HiDPI Squircle Album Art**: Pulls exact lossless PNG covers directly from Windows GSMTC (Spotify / Apple Music / Chrome) and pre-renders a supersampled PIL Lanczos-filtered squircle thumbnail (`|x|^{4.6} + |y|^{4.6} = 1`).
- **Live Song Marquee, Progress Hairline & Direct Controls**: Displays the active song title (smooth 60fps marquee) with an album-tinted progress hairline. Scroll over the idle notch to adjust volume, middle-click to play/pause, or right-click to skip.
- **Real-Time 60fps WASAPI Visualizer**: True decibel (`-42 dB .. 0 dB`) metering driven directly by Windows `IAudioMeterInformation`, colored with the album cover's extracted palette.
- **Live Orange Timer & Stopwatch**: Tracks both the Windows Clock app and built-in Notch Pomodoro timers with distinct 60fps rotating-hand vector icons (widens automatically alongside music).
- **Live Volume HUD, Battery Pulse, Downloads Ring & Privacy Dots**: Pops sleek animated pills for OS volume changes, AC charging / low battery, active `Downloads` progress (with emerald checkmark flash on completion), and glowing iOS-style orange/green Mic & Camera privacy dots.
- **Smart Title-Bar Dodge**: Automatically tucks into a 3px micro-line when your mouse approaches browser tabs underneath.

### Wide DockBar PC Dashboard (`Ctrl + /` or Click Notch)
- **Now Playing Card**:
  - Continuous-curvature `84×84` squircle album art + `26×26` 4×-supersampled music player icon below (Spotify, Apple Music/iTunes, YouTube Music, YouTube, SoundCloud).
  - **Click Album Cover → Synced Live Lyrics (`lrclib.net`)** or right-click to focus the music player window.
  - Hand-drawn monochrome vector Previous / Play-Pause / Next controls + interactive Windows GSMTC scrub/seek bar + 20s magnetic retraction after pausing.
- **Calendar, Clock & Live Weather**: 12-hour live clock, 7-day calendar strip, upcoming event pill, and a hand-drawn vector live weather badge (Open-Meteo, 30-min cache).
- **System Monitor**: Real-time CPU, RAM, and Disk usage bars with Low / Normal / High status indicators and custom vector icons.
- **1:1 Windows Volume Slider & Audio Output Switcher**: Vertical CoreAudio `IAudioEndpointVolume` slider; click the vector speaker icon to 1-click switch Windows playback devices (Headphones / Speakers / Bluetooth).
- **4-Tab Utility Hub (Bento Grid Icon or Drag-and-Drop to Notch)**:
  - **File Shelf**: Drag files/images into the notch to park them, drag them right back out into any app, or drop onto **Ask JARVIS** for instant AI analysis.
  - **Clipboard History**: Tracks your last 8 copied snippets for 1-click re-copy.
  - **Quick Webcam Mirror**: Horizontally-mirrored squircle live camera preview (`PySide6.QtMultimedia`), automatically turning off when closed.
  - **Quick Focus / Pomodoro Timer**: Start 5m / 10m / 15m / 25m / 45m focus timers straight from the notch.
- **Live Notification Toasts**: Smooth 5.5s Dynamic Island toast pills for reminders, timer completions, and system alerts with 1-click **Reply** and **Dismiss**.

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
# Primary LLM (Custom JARVIS-Qwen-7B via LM Studio or Google Gemini)
LLM_MODEL=lmstudio:jarvis-qwen-7b
LLM_FALLBACK_MODEL=gemini-3.5-flash-lite
LOCAL_LLM_BASE_URL=http://127.0.0.1:1234/v1

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

---

*Coded using Google Gemini as a partner.*
