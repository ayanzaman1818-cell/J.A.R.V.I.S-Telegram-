"""Voice capabilities: Edge-TTS, Twilio phone calls, and Groq/Gemini audio transcription."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import shutil
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import config
from database.repository import db

log = logging.getLogger("jarvis.voice")

OPENPEON_DIR: Path = config.BASE_DIR / "assets" / "openpeon_jarvis"

# Fish Audio circuit breaker: skip the API for a while after account-level failures
# (no credit, bad key, rate limit) so every voice note doesn't wait on a doomed call.
_fish_disabled_until: float = 0.0
_fish_last_error: str = ""


def _fish_cooldown_for(status: int | None) -> int:
    if status in (401, 402, 403):
        return 1800
    if status == 429:
        return 300
    return 120


def fish_status() -> str:
    import time

    remaining = int(_fish_disabled_until - time.time())
    if not (config.FISH_API_KEY and config.FISH_MODEL_ID):
        return "not configured"
    if remaining > 0:
        return f"paused {remaining}s ({_fish_last_error})"
    return "ready"


@lru_cache(maxsize=1)
def load_openpeon_manifest() -> dict[str, list[dict[str, str]]]:
    """Load the bundled FlynnCruse/openpeon-jarvis Marvel JARVIS sound pack manifest."""
    manifest_path = OPENPEON_DIR / "openpeon.json"
    if not manifest_path.exists():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        categories: dict[str, list[dict[str, str]]] = {}
        for cat_name, cat_obj in (data.get("categories") or {}).items():
            sounds = []
            for s in cat_obj.get("sounds") or []:
                rel_file = s.get("file", "")
                full_path = OPENPEON_DIR / rel_file
                if rel_file and full_path.exists():
                    sounds.append(
                        {
                            "path": str(full_path),
                            "label": str(s.get("label", "")).strip(),
                            "category": cat_name,
                        }
                    )
            if sounds:
                categories[cat_name] = sounds
        return categories
    except Exception:
        log.warning("Failed to load openpeon.json sound pack", exc_info=True)
        return {}


def pick_openpeon_clip(
    category: str = "session.start", query: str = ""
) -> tuple[str, str] | None:
    """Return (mp3_path, transcript_label) from the bundled openpeon-jarvis pack."""
    cats = load_openpeon_manifest()
    if not cats:
        return None
    if query.strip():
        q_low = query.strip().lower()
        all_sounds = [s for group in cats.values() for s in group]
        matches = [s for s in all_sounds if q_low in s["label"].lower()]
        if matches:
            chosen = random.choice(matches)
            return chosen["path"], chosen["label"]
    pool = cats.get(category) or [s for group in cats.values() for s in group]
    if not pool:
        return None
    chosen = random.choice(pool)
    return chosen["path"], chosen["label"]


def _norm_quote(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def match_openpeon_clip(text: str) -> str | None:
    """If text matches one of the bundled openpeon-jarvis canon lines, return its MP3 path."""
    norm = _norm_quote(text)
    if not norm:
        return None
    cats = load_openpeon_manifest()
    for group in cats.values():
        for s in group:
            if _norm_quote(s["label"]) == norm:
                return s["path"]
    return None

VOICE_PRESETS: dict[str, str] = {
    "jarvis": "en-GB-RyanNeural",
    "marvel": "en-GB-RyanNeural",
    "bettany": "en-GB-RyanNeural",
    "ryan": "en-GB-RyanNeural",
    "thomas": "en-GB-ThomasNeural",
    "sonia": "en-GB-SoniaNeural",
    "christopher": "en-US-ChristopherNeural",
    "guy": "en-US-ChristopherNeural",
    "jenny": "en-US-JennyNeural",
    "aria": "en-US-AriaNeural",
    "natasha": "en-AU-NatashaNeural",
}


def resolve_voice_name(name: str | None) -> str:
    if not name or not name.strip():
        return config.TTS_VOICE
    cleaned = name.strip()
    return VOICE_PRESETS.get(cleaned.lower(), cleaned)


def tts_engine_status(user_id: int | None = None) -> str:
    """Return a summary of the active TTS voice engine."""
    cats = load_openpeon_manifest()
    pack_count = sum(len(v) for v in cats.values())
    pack_info = f"openpeon-jarvis ({pack_count} MCU clips) + " if pack_count else ""
    if config.FISH_API_KEY and config.FISH_MODEL_ID:
        return f"{pack_info}Fish Audio Custom Clone (model_id={config.FISH_MODEL_ID[:8]}...)"
    if config.ELEVENLABS_API_KEY and config.ELEVENLABS_VOICE_ID:
        return f"{pack_info}ElevenLabs Marvel Clone (voice_id={config.ELEVENLABS_VOICE_ID[:8]}...)"
    v = get_user_tts_voice(user_id)
    return f"{pack_info}Edge-TTS Marvel JARVIS prosody ({v}, rate={config.TTS_RATE}, pitch={config.TTS_PITCH})"


def is_voice_mode_enabled(user_id: int | None = None) -> bool:
    """Return True if continuous two-way Telegram voice conversation mode is active for the user."""
    prefs = db.list_preferences(user_id=user_id)
    val = prefs.get("voice_chat_mode")
    if val is None:
        return config.VOICE_CHAT_DEFAULT
    return val.lower() in ("1", "true", "yes", "on")


def set_voice_mode(enabled: bool, user_id: int | None = None) -> bool:
    """Enable or disable two-way Telegram voice conversation mode for the user."""
    db.set_preference("voice_chat_mode", "true" if enabled else "false", user_id=user_id)
    return enabled


def get_user_tts_voice(user_id: int | None = None) -> str:
    """Return the user's preferred Edge-TTS voice or the default config.TTS_VOICE."""
    prefs = db.list_preferences(user_id=user_id)
    return resolve_voice_name(prefs.get("tts_voice"))


def set_user_tts_voice(voice_name: str, user_id: int | None = None) -> str:
    """Save the user's preferred Edge-TTS voice."""
    resolved = resolve_voice_name(voice_name)
    db.set_preference("tts_voice", resolved, user_id=user_id)
    return resolved


def _clean_for_speech(text: str) -> str:
    """Strip URLs and markdown so speech sounds natural."""
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_`#>]", "", text)
    return re.sub(r"\s+", " ", text).strip()


async def _tts_with_fish_audio(text: str, out_path: str) -> None:
    """Synthesize speech via Fish Audio (fish.audio) REST API using the user's custom JARVIS voice model."""
    import httpx

    url = "https://api.fish.audio/v1/tts"
    headers = {
        "Authorization": f"Bearer {config.FISH_API_KEY.strip()}",
        "Content-Type": "application/json",
        "model": config.FISH_MODEL or "s2.1-pro-free",
    }
    payload = {
        "text": text,
        "reference_id": config.FISH_MODEL_ID.strip(),
        "format": "mp3",
        "normalize": True,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        with open(out_path, "wb") as f:
            f.write(resp.content)


async def _tts_with_elevenlabs(text: str, out_path: str) -> None:
    """Synthesize speech via ElevenLabs REST API using the configured Marvel JARVIS voice ID."""
    import httpx

    voice_id = config.ELEVENLABS_VOICE_ID.strip()
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": config.ELEVENLABS_API_KEY,
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": config.ELEVENLABS_MODEL or "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.72,
            "similarity_boost": 0.86,
            "style": 0.15,
            "use_speaker_boost": True,
        },
    }
    async with httpx.AsyncClient(timeout=25.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        with open(out_path, "wb") as f:
            f.write(resp.content)


async def tts_file(
    text: str,
    *,
    voice_name: str | None = None,
    user_id: int | None = None,
) -> str:
    """Generate an MP3 voice note via ElevenLabs (if configured) or tuned Microsoft Edge TTS. Caller must unlink."""
    import edge_tts

    selected_voice = resolve_voice_name(voice_name) if voice_name else get_user_tts_voice(user_id)
    fd, path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    try:
        cleaned = _clean_for_speech(text)[:3000] or "Done."
        matched_clip = match_openpeon_clip(cleaned)
        if matched_clip and os.path.exists(matched_clip):
            shutil.copyfile(matched_clip, path)
            return path

        if config.FISH_API_KEY and config.FISH_MODEL_ID and not voice_name:
            import time

            global _fish_disabled_until, _fish_last_error
            if time.time() >= _fish_disabled_until:
                try:
                    await _tts_with_fish_audio(cleaned, path)
                    return path
                except Exception as exc:
                    response = getattr(exc, "response", None)
                    status = getattr(response, "status_code", None)
                    err_detail = (getattr(response, "text", "") or "")[:160]
                    cooldown = _fish_cooldown_for(status)
                    _fish_disabled_until = time.time() + cooldown
                    _fish_last_error = f"HTTP {status}" if status else type(exc).__name__
                    log.warning(
                        "Fish Audio TTS failed (%s%s); skipping it for %ss and falling back",
                        _fish_last_error,
                        f": {err_detail}" if err_detail else "",
                        cooldown,
                    )

        if config.ELEVENLABS_API_KEY and config.ELEVENLABS_VOICE_ID and not voice_name:
            try:
                await _tts_with_elevenlabs(cleaned, path)
                return path
            except Exception as exc:
                log.warning(
                    "ElevenLabs TTS failed, falling back to Edge-TTS Marvel prosody: %s",
                    type(exc).__name__,
                )

        # Apply Marvel JARVIS prosody tuning (crisp British pace + deeper resonance) when using Ryan/Thomas
        rate = config.TTS_RATE if selected_voice in ("en-GB-RyanNeural", "en-GB-ThomasNeural") else "+0%"
        pitch = config.TTS_PITCH if selected_voice in ("en-GB-RyanNeural", "en-GB-ThomasNeural") else "+0Hz"
        await asyncio.wait_for(
            edge_tts.Communicate(
                cleaned, selected_voice, rate=rate, pitch=pitch
            ).save(path),
            timeout=25.0,
        )
    except Exception:
        if os.path.exists(path):
            os.unlink(path)
        raise
    return path


# In-memory store for custom TTS audio clips served to Twilio <Play> during phone calls
_TWILIO_AUDIO_CACHE: dict[str, tuple[float, bytes]] = {}


def cache_twilio_audio(audio_bytes: bytes) -> str:
    """Store an MP3 clip in memory for 10 minutes and return its token ID for Twilio <Play>."""
    import secrets
    import time

    now = time.time()
    expired = [k for k, (ts, _) in _TWILIO_AUDIO_CACHE.items() if now - ts > 600]
    for k in expired:
        _TWILIO_AUDIO_CACHE.pop(k, None)

    token = secrets.token_urlsafe(12)
    _TWILIO_AUDIO_CACHE[token] = (now, audio_bytes)
    return token


def get_twilio_audio(token: str) -> bytes | None:
    """Retrieve a cached MP3 clip for Twilio <Play>."""
    import time

    item = _TWILIO_AUDIO_CACHE.get(token)
    if not item:
        return None
    ts, data = item
    if time.time() - ts > 600:
        _TWILIO_AUDIO_CACHE.pop(token, None)
        return None
    return data


def calls_enabled() -> bool:
    return all(
        [config.TWILIO_SID, config.TWILIO_TOKEN, config.TWILIO_NUMBER, config.MY_PHONE]
    )


def resolve_twilio_webhook_url(base_url: str | None = None) -> str:
    """Ensure the Twilio webhook URL points to the /twilio/voice endpoint."""
    raw = (base_url or config.TWILIO_WEBHOOK_URL or "").strip()
    if not raw:
        return ""
    raw = raw.rstrip("/")
    if not raw.endswith("/twilio/voice"):
        if raw.endswith("/twilio"):
            return f"{raw}/voice"
        return f"{raw}/twilio/voice"
    return raw


def build_twiml(
    text: str,
    interactive: bool = True,
    *,
    webhook_url: str | None = None,
    audio_url: str | None = None,
    hangup: bool = False,
) -> str:
    """Build TwiML for a phone call, supporting continuous two-way <Gather input='speech'> conversation loops."""
    spoken = escape(_clean_for_speech(text)[:1500] or "At your service, sir.")
    voice_attr = escape(config.TWILIO_VOICE or "Polly.Brian")
    if audio_url:
        prompt_xml = f"<Play>{escape(audio_url)}</Play>"
    else:
        prompt_xml = f'<Say voice="{voice_attr}" language="en-GB">{spoken}</Say>'

    if hangup:
        return f"<Response>{prompt_xml}<Hangup/></Response>"

    target_webhook = resolve_twilio_webhook_url(webhook_url)
    if interactive and target_webhook:
        escaped_hook = escape(target_webhook)
        reprompt = escape(f"I am still on the line, {config.USER_NAME}. Go ahead whenever you are ready.")
        return (
            "<Response>"
            f'<Gather input="speech" action="{escaped_hook}" method="POST" '
            'speechTimeout="auto" speechModel="experimental_conversations" enhanced="true" '
            'actionOnEmptyResult="true" timeout="8" language="en-US">'
            f"{prompt_xml}"
            "</Gather>"
            f'<Gather input="speech" action="{escaped_hook}" method="POST" '
            'speechTimeout="auto" actionOnEmptyResult="true" timeout="8" language="en-US">'
            f'<Say voice="{voice_attr}" language="en-GB">{reprompt}</Say>'
            "</Gather>"
            "</Response>"
        )
    return f'<Response><Pause length="1"/>{prompt_xml}</Response>'


async def prepare_twilio_prompt_audio(text: str, base_url: str | None = None) -> str | None:
    """Synthesize JARVIS's custom TTS voice (Fish Audio / ElevenLabs / Edge-TTS / MCU pack) and return a public URL for Twilio <Play>."""
    if not config.TWILIO_USE_CUSTOM_TTS:
        return None
    webhook = resolve_twilio_webhook_url(base_url)
    if not webhook:
        return None
    root = webhook[: -len("/twilio/voice")] if webhook.endswith("/twilio/voice") else webhook.rsplit("/", 1)[0]
    try:
        mp3_path = await tts_file(text, user_id=config.OWNER_ID or None)
        try:
            audio_bytes = Path(mp3_path).read_bytes()
        finally:
            if os.path.exists(mp3_path):
                os.unlink(mp3_path)
        if not audio_bytes:
            return None
        token = cache_twilio_audio(audio_bytes)
        return f"{root}/twilio/audio/{token}.mp3"
    except Exception:
        log.debug("Falling back to Twilio <Say> for call prompt", exc_info=True)
        return None


def _call_sync(text: str, audio_url: str | None = None) -> str:
    from twilio.rest import Client

    twiml = build_twiml(
        text,
        interactive=bool(config.TWILIO_WEBHOOK_URL),
        audio_url=audio_url,
    )
    call = Client(config.TWILIO_SID, config.TWILIO_TOKEN).calls.create(
        to=config.MY_PHONE, from_=config.TWILIO_NUMBER, twiml=twiml
    )
    return call.sid


async def phone_call(text: str) -> str:
    """Ring the owner's phone, speak the message, and listen for two-way spoken responses when TWILIO_WEBHOOK_URL is set."""
    if not calls_enabled():
        raise RuntimeError(
            "Phone calls aren't set up. Add TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, "
            "TWILIO_NUMBER, and MY_PHONE to .env."
        )
    audio_url = None
    if config.TWILIO_WEBHOOK_URL and config.TWILIO_USE_CUSTOM_TTS:
        audio_url = await prepare_twilio_prompt_audio(text)
    return await asyncio.wait_for(
        asyncio.to_thread(_call_sync, text, audio_url), timeout=25.0
    )


@lru_cache(maxsize=1)
def _groq_client():
    from openai import AsyncOpenAI

    return AsyncOpenAI(
        api_key=config.GROQ_API_KEY,
        base_url="https://api.groq.com/openai/v1",
        timeout=25.0,
    )


async def _transcribe_with_groq(audio_bytes: bytes, filename: str) -> str:
    heard = await _groq_client().audio.transcriptions.create(
        model="whisper-large-v3-turbo",
        file=(filename, audio_bytes),
    )
    return (heard.text or "").strip()


async def _transcribe_with_gemini(audio_bytes: bytes, mime_type: str) -> str:
    """Fallback STT using Gemini's multimodal audio understanding."""
    from google.genai import types
    from brain.gemini import get_client

    client = get_client()
    if client is None:
        raise RuntimeError("GEMINI_API_KEY is missing.")
    model = config.LLM_FALLBACK_MODEL or config.LLM_FAST_MODEL
    part = types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)
    response = await asyncio.wait_for(
        client.aio.models.generate_content(
            model=model,
            contents=[
                "Transcribe this voice message verbatim. Output only the spoken words, nothing else.",
                part,
            ],
        ),
        timeout=float(config.LLM_TIMEOUT_SECONDS),
    )
    return (response.text or "").strip()


async def transcribe_audio(
    audio_bytes: bytes,
    filename: str = "voice.ogg",
    mime_type: str = "audio/ogg",
) -> str:
    """Transcribe audio via Groq Whisper first, falling back to Gemini."""
    if config.GROQ_API_KEY:
        try:
            text = await _transcribe_with_groq(audio_bytes, filename)
            if text:
                return text
        except Exception as exc:
            log.warning("Groq STT failed, falling back to Gemini: %s", type(exc).__name__)

    return await _transcribe_with_gemini(audio_bytes, mime_type)
