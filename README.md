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

# Optional: Groq Whisper voice transcription
GROQ_API_KEY=

# Optional: Composio integrations
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

# Optional: briefing and text-to-speech settings
BRIEFING_VOICE=true
BRIEFING_CALL=false
TTS_VOICE=en-GB-RyanNeural

# Optional: policy / knowledge-base settings
QUIET_HOURS_START=22:00
QUIET_HOURS_END=07:00
KNOWLEDGE_DRIVE_FOLDER=
```

This is a template, not a guarantee that every variable is enabled in
every revision. Check `config.py`, `main.py`, `voice.py`, and
`composio_tools.py` in the version you are running. Remove unused
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

  `BRIEFING_TIME`                     Optional                Local time for
                                                              scheduled briefings

  `GROQ_API_KEY`                      Optional                Groq API key for
                                                              Whisper transcription

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

  `BRIEFING_VOICE`                    Optional                Enables voice briefing,
                                                              if implemented

  `BRIEFING_CALL`                     Optional                Enables phone-call
                                                              briefings, if
                                                              implemented

  `TTS_VOICE`                         Optional                Edge TTS voice name

  `QUIET_HOURS_START` /               Optional                Quiet-hours window, if
  `QUIET_HOURS_END`                                           implemented

  `KNOWLEDGE_DRIVE_FOLDER`            Optional                Drive knowledge-base
                                                              folder, if implemented
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

## Optional integrations

### Gmail, Google Drive, and Calendar

Configure Composio and connect the relevant Google accounts. Available
actions depend on the tools implemented in the current `tools.py` and
`composio_tools.py`.

Keep read-only operations read-only. Actions that send messages, create
events, delete data, or share files should require explicit
confirmation.

### Voice

Set `GROQ_API_KEY` if the installed voice handler uses Groq Whisper.
Voice output may use Edge TTS and `TTS_VOICE`.

### Scheduled briefings and reminders

Set `TIMEZONE` and, if supported, `BRIEFING_TIME`. The application must
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
