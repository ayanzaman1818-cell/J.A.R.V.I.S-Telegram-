"""Native 1-on-1 Telegram VoIP Calling for JARVIS using Telethon + PyTgCalls (ntgcalls) + miniaudio PCM streaming."""

from __future__ import annotations

import array
import asyncio
import io
import logging
import math
import os
import re
import time
import wave
from pathlib import Path
from typing import Any

import config
from services import voice
from tools.base import ToolContext

log = logging.getLogger("jarvis.telegram_call")

_GOODBYE_RE = re.compile(
    r"\b(goodbye|bye|hang\s*up|end\s+(?:the\s+)?call|that(?:'s|\s+is)\s+all|nothing\s+else|disconnect)\b",
    re.IGNORECASE,
)

# 48 kHz stereo 16-bit PCM (AudioQuality.HIGH in PyTgCalls = (48000, 2))
SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_SAMPLE = 2
# WebRTC / ntgcalls audio sink consumes exactly 10ms frames (480 samples * 2 channels * 2 bytes = 1920 bytes).
# Note: Windows asyncio.sleep(0.01) has a 15.6ms timer quantum, so sleeping per-frame starves the WebRTC ring buffer.
# Streaming the decoded PCM buffer directly into ntgcalls's internal paced FIFO ring buffer eliminates all jitter/chop!
FRAME_MS = 10
FRAME_BYTES = (SAMPLE_RATE // 100) * CHANNELS * BYTES_PER_SAMPLE  # 1920 bytes per 10ms WebRTC frame

_mtproto_client: Any = None
_tgcalls_client: Any = None
_active_bot: Any = None
_active_job_queue: Any = None
_call_states: dict[int, dict[str, Any]] = {}


def is_native_call_configured() -> bool:
    """Return True if a logged-in Telethon userbot session file exists on disk."""
    sess = config.TELEGRAM_CALL_SESSION
    if sess.exists():
        return True
    if not str(sess).endswith(".session") and Path(f"{sess}.session").exists():
        return True
    return False


def _pcm_rms(pcm_bytes: bytes) -> float:
    """Compute RMS amplitude of 16-bit signed little-endian PCM bytes."""
    if len(pcm_bytes) < 4:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm_bytes[: len(pcm_bytes) - (len(pcm_bytes) % 2)])
    if not samples:
        return 0.0
    # Subsample every 4th value for fast real-time VAD
    step = 4
    total = 0.0
    count = 0
    for i in range(0, len(samples), step):
        val = samples[i]
        total += val * val
        count += 1
    return math.sqrt(total / max(1, count))


def _pcm_to_wav_bytes(pcm_bytes: bytes, sample_rate: int = SAMPLE_RATE, channels: int = CHANNELS) -> bytes:
    """Wrap raw 16-bit signed little-endian PCM into an in-memory WAV container for Groq Whisper / Gemini STT."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(BYTES_PER_SAMPLE)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def _decode_mp3_to_pcm(mp3_path: str) -> bytes:
    """Decode an MP3 file into 48kHz stereo 16-bit signed PCM bytes using miniaudio (no external ffmpeg required)."""
    import miniaudio

    decoded = miniaudio.decode_file(
        mp3_path,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=CHANNELS,
        sample_rate=SAMPLE_RATE,
    )
    return bytes(decoded.samples)


def _get_or_init_call_state(chat_id: int) -> dict[str, Any]:
    state = _call_states.get(chat_id)
    if state is None:
        state = {
            "active": True,
            "is_speaking": False,
            "is_processing": False,
            "speech_buf": bytearray(),
            "speaking_detected": False,
            "speech_started_at": 0.0,
            "last_voice_at": 0.0,
            "pending_greeting": "",
            "play_lock": asyncio.Lock(),
        }
        _call_states[chat_id] = state
    return state


async def _speak_on_telegram_call(chat_id: int, text: str) -> None:
    """Synthesize JARVIS TTS audio and stream 48kHz stereo PCM into the active native Telegram call."""
    if _tgcalls_client is None:
        return
    state = _get_or_init_call_state(chat_id)
    async with state["play_lock"]:
        state["is_speaking"] = True
        state["speech_buf"].clear()
        state["speaking_detected"] = False
        try:
            mp3_path = await voice.tts_file(text, user_id=config.OWNER_ID or chat_id)
            try:
                pcm_data = await asyncio.to_thread(_decode_mp3_to_pcm, mp3_path)
            finally:
                if os.path.exists(mp3_path):
                    os.unlink(mp3_path)

            if not pcm_data or not state.get("active", False):
                return

            # Pad to exact 10ms WebRTC frame boundary (1920 bytes)
            rem = len(pcm_data) % FRAME_BYTES
            if rem:
                pcm_data = pcm_data + (b"\x00" * (FRAME_BYTES - rem))

            bytes_per_sec = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE  # 192,000 bytes/sec
            total_duration = len(pcm_data) / float(bytes_per_sec)

            from pytgcalls.types import Device

            # Begin Windows 1ms multimedia timer resolution (winmm.timeBeginPeriod(1))
            # so asyncio.sleep(0.010) wakes up at exact 10ms WebRTC frame intervals instead of 15.6ms jumps.
            winmm = None
            try:
                import ctypes

                winmm = ctypes.windll.winmm
                winmm.timeBeginPeriod(1)
            except Exception:
                winmm = None

            try:
                # Stream exact 10ms frames (1920 bytes = 480 stereo samples at 48kHz) paced to wall-clock time
                t0 = time.perf_counter()
                frame_idx = 0
                for offset in range(0, len(pcm_data), FRAME_BYTES):
                    if not state.get("active", False):
                        break
                    frame = pcm_data[offset : offset + FRAME_BYTES]
                    await _tgcalls_client.send_frame(
                        chat_id,
                        Device.MICROPHONE,
                        frame,
                    )
                    frame_idx += 1
                    target_time = frame_idx * 0.010  # 10ms per frame
                    sleep_dur = target_time - (time.perf_counter() - t0)
                    if sleep_dur > 0.001:
                        await asyncio.sleep(sleep_dur)
                await asyncio.sleep(0.08)
            finally:
                if winmm is not None:
                    try:
                        winmm.timeEndPeriod(1)
                    except Exception:
                        pass
        except Exception:
            log.warning("Failed streaming TTS audio into native Telegram call", exc_info=True)
        finally:
            state["is_speaking"] = False
            state["speech_buf"].clear()
            state["speaking_detected"] = False


async def _process_captured_utterance(chat_id: int, pcm_bytes: bytes) -> None:
    """Transcribe captured caller speech, run JARVIS brain + tools, and speak the reply on the Telegram call."""
    from brain.agent import respond as brain_respond

    state = _get_or_init_call_state(chat_id)
    if state["is_processing"] or not state["active"]:
        return
    state["is_processing"] = True
    try:
        wav_bytes = await asyncio.to_thread(_pcm_to_wav_bytes, pcm_bytes)
        heard = (
            await voice.transcribe_audio(
                wav_bytes, filename="tg_call.wav", mime_type="audio/wav"
            )
        ).strip()
        if not heard or len(heard) < 2:
            return

        log.info("Native Telegram Call heard (chat_id=%s): %s", chat_id, heard[:160])

        # Check for hangup phrase
        if len(heard) <= 75 and _GOODBYE_RE.search(heard):
            farewell = f"Very good, {config.USER_NAME}. Always a pleasure. Hanging up now."
            await _speak_on_telegram_call(chat_id, farewell)
            await hangup_native_telegram_call(chat_id)
            return

        owner_id = config.OWNER_ID or chat_id
        ctx = ToolContext(
            bot=_active_bot,
            chat_id=owner_id,
            job_queue=_active_job_queue,
            user_id=owner_id,
        )
        prompt = (
            f"[Live Native Telegram Voice Call — respond concisely in 1 to 2 natural spoken sentences without markdown or bullet lists]: "
            f"{heard}"
        )
        try:
            reply = await asyncio.wait_for(
                brain_respond(ctx, prompt, fast_voice=True), timeout=18.0
            )
        except asyncio.TimeoutError:
            reply = f"I am still working on that request, {config.USER_NAME}. What else can I do for you?"
        except Exception:
            log.exception("Native Telegram call brain_respond failed")
            reply = f"Pardon me, {config.USER_NAME}, I ran into a momentary issue. Please go ahead again."

        reply = (reply or "").strip() or f"Done, {config.USER_NAME}."
        log.info("Native Telegram Call JARVIS reply: %s", reply[:160])

        # Mirror call transcript to Telegram text chat
        if _active_bot and owner_id:
            try:
                asyncio.create_task(
                    _active_bot.send_message(
                        owner_id,
                        f"📞 **Telegram Call**\n🎙 You: {heard}\n🎩 JARVIS: {reply}",
                    )
                )
            except Exception:
                pass

        await _speak_on_telegram_call(chat_id, reply)
    finally:
        state["is_processing"] = False


async def _connect_bidirectional_stream(chat_id: int, greeting: str = "") -> None:
    """Attach external PCM playback and microphone capture streams to a 1-on-1 Telegram call."""
    if _tgcalls_client is None:
        return
    from pytgcalls.types import AudioQuality, Direction, ExternalMedia, MediaStream, RecordStream

    state = _get_or_init_call_state(chat_id)
    state["active"] = True
    if greeting:
        state["pending_greeting"] = greeting

    await _tgcalls_client.play(
        chat_id,
        MediaStream(
            ExternalMedia.AUDIO,
            audio_parameters=AudioQuality.HIGH,
        ),
    )
    await _tgcalls_client.record(
        chat_id,
        RecordStream(
            audio=True,
            audio_parameters=AudioQuality.HIGH,
        ),
    )
    spoken_greeting = (
        state.pop("pending_greeting", "")
        or greeting
        or f"At your service, {config.USER_NAME}. I am on the line — go ahead whenever you are ready."
    )
    asyncio.create_task(_speak_on_telegram_call(chat_id, spoken_greeting))


async def start_native_telegram_call_service(
    *,
    bot: Any = None,
    job_queue: Any = None,
) -> bool:
    """Initialize and start the Telethon + PyTgCalls userbot service if a session file is present."""
    global _mtproto_client, _tgcalls_client, _active_bot, _active_job_queue
    _active_bot = bot
    _active_job_queue = job_queue

    if _tgcalls_client is not None:
        return True
    if not is_native_call_configured():
        return False

    client = None
    try:
        import sqlite3
        from pytgcalls import PyTgCalls, filters
        from pytgcalls.types import ChatUpdate, Device, Direction, StreamFrames
        from telethon import TelegramClient

        session_path = str(config.TELEGRAM_CALL_SESSION)
        if session_path.endswith(".session"):
            session_path = session_path[: -len(".session")]

        # Enable WAL mode and a 10s busy_timeout on Telethon's session SQLite file so it never throws 'database is locked'
        db_file = f"{session_path}.session"
        if os.path.exists(db_file):
            try:
                with sqlite3.connect(db_file, timeout=10.0) as conn:
                    conn.execute("PRAGMA journal_mode=WAL;")
                    conn.execute("PRAGMA busy_timeout=10000;")
            except Exception:
                pass

        client = TelegramClient(
            session_path,
            config.TELEGRAM_API_ID,
            config.TELEGRAM_API_HASH,
        )
        if getattr(client.session, "_conn", None) is not None:
            try:
                client.session._conn.execute("PRAGMA busy_timeout=10000;")
            except Exception:
                pass
        await client.connect()
        if not await client.is_user_authorized():
            log.warning(
                "JARVIS Telegram Call session exists but is not authorized. Run: py setup_telegram_call.py"
            )
            await client.disconnect()
            return False

        me = await client.get_me()
        tgcalls = PyTgCalls(client)

        @tgcalls.on_update(filters.chat_update(ChatUpdate.Status.INCOMING_CALL))
        async def _on_incoming_call(_: PyTgCalls, update: ChatUpdate) -> None:
            caller_id = update.chat_id
            if config.OWNER_ID and caller_id != config.OWNER_ID and caller_id not in config.ALLOWED_USER_IDS:
                log.warning("Ignoring incoming Telegram call from unauthorized user_id=%s", caller_id)
                return
            log.info("Auto-answering incoming native Telegram call from user_id=%s", caller_id)
            await _connect_bidirectional_stream(
                caller_id,
                greeting=f"At your service, {config.USER_NAME}. How may I assist you?",
            )

        @tgcalls.on_update(
            filters.chat_update(
                ChatUpdate.Status.LEFT_CALL | ChatUpdate.Status.DISCARDED_CALL
            )
        )
        async def _on_call_ended(_: PyTgCalls, update: ChatUpdate) -> None:
            st = _call_states.pop(update.chat_id, None)
            if st:
                st["active"] = False
            log.info("Native Telegram call ended for chat_id=%s", update.chat_id)

        @tgcalls.on_update(
            filters.stream_frame(Direction.INCOMING, Device.MICROPHONE)
        )
        async def _on_incoming_audio(_: PyTgCalls, update: StreamFrames) -> None:
            chat_id = update.chat_id
            state = _call_states.get(chat_id)
            if not state or not state.get("active") or state.get("is_speaking") or state.get("is_processing"):
                return
            if not update.frames:
                return
            chunk = b"".join(f.frame for f in update.frames if f.frame)
            if not chunk:
                return

            rms = _pcm_rms(chunk)
            now = time.monotonic()
            # Keep a rolling 300ms pre-roll buffer so the beginning consonant of a word is never clipped
            preroll: bytearray = state.setdefault("preroll_buf", bytearray())
            max_preroll = int(SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE * 0.30)

            # Voice activity detection threshold (~350 on 16-bit scale -32768..32767)
            if rms > 350:
                if not state["speaking_detected"]:
                    state["speaking_detected"] = True
                    state["speech_started_at"] = now
                    if preroll:
                        state["speech_buf"].extend(preroll)
                        preroll.clear()
                state["last_voice_at"] = now
                state["speech_buf"].extend(chunk)
            elif state["speaking_detected"]:
                state["speech_buf"].extend(chunk)
                silence_dur = now - state["last_voice_at"]
                utterance_dur = now - state["speech_started_at"]
                if silence_dur >= 0.62 or utterance_dur >= 12.0:
                    captured = bytes(state["speech_buf"])
                    state["speech_buf"].clear()
                    state["speaking_detected"] = False
                    # Only transcribe if at least ~0.30s of audio
                    if len(captured) >= SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE * 0.30:
                        asyncio.create_task(_process_captured_utterance(chat_id, captured))
            else:
                preroll.extend(chunk)
                if len(preroll) > max_preroll:
                    del preroll[: len(preroll) - max_preroll]

        await tgcalls.start()
        _mtproto_client = client
        _tgcalls_client = tgcalls
        log.info(
            "Native Telegram VoIP Calling userbot online as '%s' (id=%s)",
            getattr(me, "first_name", "JARVIS"),
            getattr(me, "id", "?"),
        )
        return True
    except Exception:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        log.warning("Could not start Native Telegram VoIP Calling userbot", exc_info=True)
        return False


async def place_native_telegram_call(
    target_user_id: int | None = None,
    greeting: str = "",
) -> str:
    """Ring the owner's Telegram app with a real native 1-on-1 Telegram VoIP Call and start 2-way voice."""
    if _tgcalls_client is None:
        started = await start_native_telegram_call_service(
            bot=_active_bot, job_queue=_active_job_queue
        )
        if not started or _tgcalls_client is None:
            raise RuntimeError(
                "Native Telegram calling is not logged in yet. Run `py setup_telegram_call.py` once to log in JARVIS's Telegram account."
            )

    uid = target_user_id or config.OWNER_ID
    if not uid:
        raise RuntimeError("OWNER_ID is not set in .env.")

    # Ensure Telethon has cached the owner's peer entity so it can dial them
    if _mtproto_client is not None:
        try:
            await _mtproto_client.get_entity(uid)
        except Exception:
            # Refresh dialogs cache so the userbot discovers the owner if they messaged it
            try:
                await _mtproto_client.get_dialogs(limit=30)
            except Exception:
                pass

    await _connect_bidirectional_stream(
        uid,
        greeting=greeting or f"At your service, {config.USER_NAME}. How may I assist you?",
    )
    return f"Ringing your Telegram account (user_id={uid}) with a real native Telegram VoIP Call."


async def hangup_native_telegram_call(chat_id: int | None = None) -> None:
    """End an active native Telegram VoIP call."""
    uid = chat_id or config.OWNER_ID
    if not uid:
        return
    st = _call_states.pop(uid, None)
    if st:
        st["active"] = False
    if _tgcalls_client is not None:
        try:
            await _tgcalls_client.leave_call(uid)
        except Exception:
            pass


async def stop_native_telegram_call_service() -> None:
    """Disconnect the Telethon userbot on shutdown."""
    global _mtproto_client, _tgcalls_client
    for uid in list(_call_states.keys()):
        await hangup_native_telegram_call(uid)
    if _mtproto_client is not None:
        try:
            await _mtproto_client.disconnect()
        except Exception:
            pass
    _mtproto_client = None
    _tgcalls_client = None
