"""Voice capabilities: Edge-TTS, Twilio phone calls, and Groq/Gemini audio transcription."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from functools import lru_cache
from xml.sax.saxutils import escape

import config

log = logging.getLogger("jarvis.voice")


def _clean_for_speech(text: str) -> str:
    """Strip URLs and markdown so speech sounds natural."""
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_`#>]", "", text)
    return re.sub(r"\s+", " ", text).strip()


async def tts_file(text: str) -> str:
    """Generate an MP3 voice note via Microsoft Edge TTS. Caller must unlink."""
    import edge_tts

    fd, path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    try:
        cleaned = _clean_for_speech(text)[:3000] or "Done."
        await asyncio.wait_for(
            edge_tts.Communicate(cleaned, config.TTS_VOICE).save(path),
            timeout=25.0,
        )
    except Exception:
        if os.path.exists(path):
            os.unlink(path)
        raise
    return path


def calls_enabled() -> bool:
    return all(
        [config.TWILIO_SID, config.TWILIO_TOKEN, config.TWILIO_NUMBER, config.MY_PHONE]
    )


def _call_sync(text: str) -> str:
    from twilio.rest import Client

    twiml = (
        '<Response><Pause length="1"/>'
        f'<Say voice="{config.TWILIO_VOICE}" language="en-GB">{escape(_clean_for_speech(text)[:1500])}</Say>'
        "</Response>"
    )
    call = Client(config.TWILIO_SID, config.TWILIO_TOKEN).calls.create(
        to=config.MY_PHONE, from_=config.TWILIO_NUMBER, twiml=twiml
    )
    return call.sid


async def phone_call(text: str) -> str:
    """Ring the owner's phone and read the message aloud (one-way)."""
    if not calls_enabled():
        raise RuntimeError(
            "Phone calls aren't set up. Add TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, "
            "TWILIO_NUMBER, and MY_PHONE to .env."
        )
    return await asyncio.wait_for(asyncio.to_thread(_call_sync, text), timeout=20.0)


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
