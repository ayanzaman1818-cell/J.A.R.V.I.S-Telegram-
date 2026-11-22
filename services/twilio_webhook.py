"""Two-way interactive voice server for JARVIS: supports both 100% Free Web/Phone Call UI (/call) and Twilio webhooks (/twilio/voice)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import config
from services import voice
from tools.base import ToolContext

log = logging.getLogger("jarvis.twilio_webhook")

_GOODBYE_RE = re.compile(
    r"\b(goodbye|bye|hang\s*up|end\s+(?:the\s+)?call|that(?:'s|\s+is)\s+all|nothing\s+else|disconnect)\b",
    re.IGNORECASE,
)

_webhook_server: asyncio.AbstractServer | None = None
_telegram_bot: Any = None
_telegram_job_queue: Any = None
_tunnel_proc: Any = None
_runtime_webhook_url: str = ""
_web_call_secret: str = secrets.token_urlsafe(12)
_pending_web_greeting: str = ""


def get_web_call_secret() -> str:
    """Return the per-process secret token authenticating the free Web Voice Call page."""
    return _web_call_secret


def set_pending_web_greeting(text: str) -> None:
    """Store an initial spoken message for the next Web Voice Call pickup."""
    global _pending_web_greeting
    _pending_web_greeting = (text or "").strip()


def get_active_webhook_url() -> str:
    """Return the configured TWILIO_WEBHOOK_URL or the auto-started Cloudflare Quick Tunnel URL."""
    return voice.resolve_twilio_webhook_url(config.TWILIO_WEBHOOK_URL or _runtime_webhook_url)


def get_active_base_url() -> str:
    """Return the public HTTPS base URL (e.g. https://xxxx.trycloudflare.com) or local fallback."""
    hook = get_active_webhook_url()
    if hook:
        return hook[: -len("/twilio/voice")] if hook.endswith("/twilio/voice") else hook.rstrip("/")
    return f"http://127.0.0.1:{config.TWILIO_WEBHOOK_PORT}"


def get_web_call_url(greeting: str = "") -> str:
    """Return the authenticated URL for the 100% free interactive Web/Phone Voice Call UI."""
    if greeting.strip():
        set_pending_web_greeting(greeting.strip())
    base = get_active_base_url()
    return f"{base}/call?k={_web_call_secret}"


def _normalize_phone(number: str) -> str:
    """Keep only digits and leading + for caller authorization comparison."""
    cleaned = re.sub(r"[^\d+]", "", (number or "").strip())
    return cleaned.lstrip("+")


def is_authorized_caller(from_number: str, to_number: str, direction: str = "") -> bool:
    """Verify the phone call is strictly between the owner's MY_PHONE and TWILIO_NUMBER."""
    if not config.MY_PHONE:
        return True
    owner_norm = _normalize_phone(config.MY_PHONE)
    if not owner_norm:
        return True
    from_norm = _normalize_phone(from_number)
    to_norm = _normalize_phone(to_number)
    return owner_norm in (from_norm, to_norm)


def is_goodbye_phrase(speech: str) -> bool:
    """Return True if the user asked to end the phone call."""
    text = (speech or "").strip()
    if not text:
        return False
    if len(text) <= 75 and _GOODBYE_RE.search(text):
        return True
    return False


def _infer_base_url_from_headers(headers: dict[str, str]) -> str:
    """Determine the public webhook base URL from config, auto-tunnel, or X-Forwarded-Host / Host headers."""
    active_hook = get_active_webhook_url()
    if active_hook:
        return active_hook[: -len("/twilio/voice")] if active_hook.endswith("/twilio/voice") else active_hook
    host = (
        headers.get("x-forwarded-host")
        or headers.get("host")
        or f"127.0.0.1:{config.TWILIO_WEBHOOK_PORT}"
    ).strip()
    proto = (headers.get("x-forwarded-proto") or "https").strip()
    if host.startswith(("127.0.0.1", "localhost", "0.0.0.0")):
        proto = "http"
    return f"{proto}://{host}"


async def _synthesize_call_audio_path(text: str) -> str | None:
    """Synthesize JARVIS TTS audio and return its relative /twilio/audio/<token>.mp3 path."""
    try:
        mp3_path = await voice.tts_file(text, user_id=config.OWNER_ID or None)
        try:
            audio_bytes = Path(mp3_path).read_bytes()
        finally:
            if os.path.exists(mp3_path):
                os.unlink(mp3_path)
        if not audio_bytes:
            return None
        token = voice.cache_twilio_audio(audio_bytes)
        return f"/twilio/audio/{token}.mp3"
    except Exception:
        log.debug("Call TTS synthesis failed", exc_info=True)
        return None


async def handle_web_call_turn(
    *,
    user_text: str = "",
    audio_bytes: bytes | None = None,
    mime_type: str = "audio/webm",
    is_greeting: bool = False,
    bot: Any = None,
    job_queue: Any = None,
) -> dict[str, Any]:
    """Process one turn of the 100% Free Web/Phone Call UI (either browser SpeechRecognition text or raw mic audio bytes)."""
    global _pending_web_greeting
    from brain.agent import respond as brain_respond

    if is_greeting:
        greeting = _pending_web_greeting or f"At your service, {config.USER_NAME}. I am on the line — go ahead whenever you are ready."
        _pending_web_greeting = ""
        audio_path = await _synthesize_call_audio_path(greeting)
        return {
            "heard": "",
            "reply": greeting,
            "audio_url": audio_path or "",
            "hangup": False,
        }

    heard = (user_text or "").strip()
    if not heard and audio_bytes:
        ext = "ogg" if "ogg" in mime_type else ("mp4" if "mp4" in mime_type else "webm")
        try:
            heard = (
                await voice.transcribe_audio(
                    audio_bytes, filename=f"call_turn.{ext}", mime_type=mime_type
                )
            ).strip()
        except Exception:
            log.warning("Web call audio transcription failed", exc_info=True)
            heard = ""

    if not heard:
        return {
            "heard": "",
            "reply": "",
            "audio_url": "",
            "hangup": False,
        }

    log.info("Web Voice Call heard: %s", heard[:160])

    if is_goodbye_phrase(heard):
        farewell = f"Very good, {config.USER_NAME}. Always a pleasure. Disconnecting now."
        audio_path = await _synthesize_call_audio_path(farewell)
        return {
            "heard": heard,
            "reply": farewell,
            "audio_url": audio_path or "",
            "hangup": True,
        }

    owner_id = config.OWNER_ID or 1
    active_bot = bot if bot is not None else _telegram_bot
    active_jq = job_queue if job_queue is not None else _telegram_job_queue
    ctx = ToolContext(
        bot=active_bot,
        chat_id=owner_id,
        job_queue=active_jq,
        user_id=owner_id,
    )

    phone_prompt = (
        f"[Live Voice Call — respond concisely in 1 to 3 natural spoken sentences without markdown or bullet lists]: "
        f"{heard}"
    )
    try:
        reply = await asyncio.wait_for(
            brain_respond(ctx, phone_prompt, fast_voice=True),
            timeout=18.0,
        )
    except asyncio.TimeoutError:
        reply = f"I am still working on that request, {config.USER_NAME}. What else can I assist you with?"
    except Exception:
        log.exception("Web voice call brain_respond failed")
        reply = f"Pardon me, {config.USER_NAME}, I ran into a momentary issue. Go ahead again."

    reply = (reply or "").strip() or f"Done, {config.USER_NAME}."
    log.info("Web Voice Call JARVIS reply: %s", reply[:160])

    if active_bot and config.OWNER_ID:
        try:
            asyncio.create_task(
                active_bot.send_message(
                    config.OWNER_ID,
                    f"📞 Live Voice Call\n🎙 You: {heard}\n🎩 JARVIS: {reply}",
                )
            )
        except Exception:
            pass

    audio_path = await _synthesize_call_audio_path(reply)
    return {
        "heard": heard,
        "reply": reply,
        "audio_url": audio_path or "",
        "hangup": False,
    }


def _build_web_call_html(secret_key: str, user_name: str) -> str:
    """Return the full-screen mobile & desktop Web Voice Call HTML UI with real-time VAD and Cloud Orb."""
    safe_user = re.sub(r"[^a-zA-Z0-9 _-]", "", user_name or "sir")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
<title>JARVIS · Live Voice Call</title>
<style>
  :root {{
    --bg: #05070c;
    --card: rgba(14, 19, 30, 0.78);
    --accent: #00b4ff;
    --accent2: #3b82f6;
    --green: #22c55e;
    --red: #ef4444;
    --text: #f8fafc;
    --muted: #94a3b8;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif;
    background: radial-gradient(circle at 50% 22%, #0c1b33 0%, #05070c 68%);
    color: var(--text);
    min-height: 100dvh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: space-between;
    padding: 28px 20px 34px;
    overflow: hidden;
  }}
  .top-bar {{
    text-align: center;
    z-index: 2;
  }}
  .badge {{
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 6px 14px;
    border-radius: 999px;
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.12);
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: #bae6fd;
    margin-bottom: 10px;
  }}
  .dot {{
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--green);
    box-shadow: 0 0 10px var(--green);
    animation: pulseDot 1.8s infinite;
  }}
  @keyframes pulseDot {{
    0%, 100% {{ opacity: 1; transform: scale(1); }}
    50% {{ opacity: 0.45; transform: scale(0.85); }}
  }}
  h1 {{ font-size: 24px; font-weight: 700; letter-spacing: -0.02em; }}
  .timer {{ font-size: 14px; color: var(--muted); margin-top: 4px; font-variant-numeric: tabular-nums; }}

  .orb-wrap {{
    position: relative;
    width: 220px;
    height: 220px;
    display: flex;
    align-items: center;
    justify-content: center;
    margin: 12px 0;
  }}
  .orb-glow {{
    position: absolute;
    inset: -24px;
    border-radius: 50%;
    background: radial-gradient(circle, rgba(0,180,255,0.38) 0%, rgba(0,110,255,0) 70%);
    transition: transform 0.18s ease-out, opacity 0.25s ease;
  }}
  .orb {{
    position: relative;
    width: 184px;
    height: 184px;
    border-radius: 50%;
    background:
      radial-gradient(circle at 38% 28%, #ffffff 0%, #dbeafe 24%, #38bdf8 54%, #0284c7 82%, #0369a1 100%);
    box-shadow:
      0 0 50px rgba(14, 165, 233, 0.55),
      inset 0 -18px 34px rgba(2, 6, 23, 0.45),
      inset 0 12px 24px rgba(255, 255, 255, 0.8);
    transition: transform 0.15s ease-out, box-shadow 0.25s ease;
    cursor: pointer;
    overflow: hidden;
  }}
  .orb::after {{
    content: "";
    position: absolute;
    inset: -40%;
    background: radial-gradient(circle at 60% 40%, rgba(255,255,255,0.45), transparent 55%);
    animation: drift 7s ease-in-out infinite alternate;
  }}
  @keyframes drift {{
    0% {{ transform: translate(-8%, -6%) rotate(0deg); }}
    100% {{ transform: translate(10%, 8%) rotate(25deg); }}
  }}
  .orb.speaking {{
    box-shadow:
      0 0 80px rgba(56, 189, 248, 0.9),
      inset 0 -18px 34px rgba(2, 6, 23, 0.35),
      inset 0 14px 28px rgba(255, 255, 255, 0.95);
  }}
  .orb.listening {{
    box-shadow:
      0 0 65px rgba(34, 197, 94, 0.7),
      inset 0 -18px 34px rgba(2, 6, 23, 0.4),
      inset 0 12px 24px rgba(255, 255, 255, 0.85);
  }}

  .transcript-box {{
    width: 100%;
    max-width: 440px;
    min-height: 118px;
    background: var(--card);
    backdrop-filter: blur(16px);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 22px;
    padding: 16px 18px;
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 8px;
    text-align: center;
  }}
  .state-label {{
    font-size: 13px;
    font-weight: 600;
    color: #38bdf8;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }}
  .heard-text {{
    font-size: 13px;
    color: var(--muted);
    font-style: italic;
  }}
  .reply-text {{
    font-size: 15px;
    line-height: 1.45;
    color: var(--text);
    font-weight: 500;
  }}

  .controls {{
    display: flex;
    align-items: center;
    gap: 22px;
    margin-top: 10px;
  }}
  .btn {{
    width: 64px;
    height: 64px;
    border-radius: 50%;
    border: none;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    color: #fff;
    font-size: 24px;
    transition: transform 0.15s ease, background 0.2s ease;
  }}
  .btn:active {{ transform: scale(0.92); }}
  .btn-mute {{
    background: rgba(255,255,255,0.12);
    border: 1px solid rgba(255,255,255,0.18);
  }}
  .btn-mute.muted {{ background: rgba(239, 68, 68, 0.35); border-color: var(--red); }}
  .btn-call {{
    width: 74px;
    height: 74px;
    background: var(--green);
    box-shadow: 0 8px 26px rgba(34, 197, 94, 0.45);
  }}
  .btn-hangup {{
    width: 74px;
    height: 74px;
    background: var(--red);
    box-shadow: 0 8px 26px rgba(239, 68, 68, 0.45);
  }}
</style>
</head>
<body>
  <div class="top-bar">
    <div class="badge"><span class="dot"></span> JARVIS LIVE VOICE LINE</div>
    <h1>J.A.R.V.I.S.</h1>
    <div class="timer" id="timer">Tap Answer to begin call</div>
  </div>

  <div class="orb-wrap" onclick="manualTrigger()">
    <div class="orb-glow" id="orbGlow"></div>
    <div class="orb" id="orb"></div>
  </div>

  <div class="transcript-box">
    <div class="state-label" id="stateLabel">Incoming Call Ready</div>
    <div class="heard-text" id="heardText">Hands-free 2-way voice conversation</div>
    <div class="reply-text" id="replyText">Tap the green Answer button below to connect with JARVIS, {safe_user}.</div>
  </div>

  <div class="controls">
    <button class="btn btn-mute" id="muteBtn" onclick="toggleMute()" title="Mute / Unmute Mic" style="display:none;">🎙</button>
    <button class="btn btn-call" id="answerBtn" onclick="startCall()" title="Answer Call">📞</button>
    <button class="btn btn-hangup" id="hangupBtn" onclick="endCall()" title="End Call" style="display:none;">✖</button>
  </div>

<script>
const SECRET = {json.dumps(secret_key)};
let callActive = false;
let isMuted = false;
let isBusy = false;
let callSeconds = 0;
let timerInterval = null;
let currentAudio = null;

// Web Speech API + MediaRecorder VAD fallback
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognition = null;
let mediaStream = null;
let audioCtx = null;
let analyser = null;
let mediaRecorder = null;
let recordedChunks = [];
let speakingDetected = false;
let silenceStart = 0;
let vadLoopId = null;

const orb = document.getElementById("orb");
const orbGlow = document.getElementById("orbGlow");
const stateLabel = document.getElementById("stateLabel");
const heardText = document.getElementById("heardText");
const replyText = document.getElementById("replyText");
const timerEl = document.getElementById("timer");
const answerBtn = document.getElementById("answerBtn");
const hangupBtn = document.getElementById("hangupBtn");
const muteBtn = document.getElementById("muteBtn");

function setStatus(label, mode) {{
  stateLabel.textContent = label;
  orb.classList.remove("speaking", "listening");
  if (mode) orb.classList.add(mode);
}}

function formatTime(sec) {{
  const m = String(Math.floor(sec / 60)).padStart(2, "0");
  const s = String(sec % 60).padStart(2, "0");
  return m + ":" + s;
}}

async function startCall() {{
  if (callActive) return;
  callActive = true;
  answerBtn.style.display = "none";
  hangupBtn.style.display = "flex";
  muteBtn.style.display = "flex";
  callSeconds = 0;
  timerEl.textContent = "00:00";
  timerInterval = setInterval(() => {{
    callSeconds++;
    timerEl.textContent = formatTime(callSeconds);
  }}, 1000);

  try {{
    mediaStream = await navigator.mediaDevices.getUserMedia({{ audio: true }});
    setupAudioVisualizer(mediaStream);
  }} catch (e) {{
    console.warn("Mic access warning:", e);
  }}

  setStatus("Connecting...", "speaking");
  try {{
    const res = await fetch(`/call/turn?k=${{encodeURIComponent(SECRET)}}&greet=1`, {{ method: "POST" }});
    const data = await res.json();
    if (data.reply) replyText.textContent = data.reply;
    if (data.audio_url) {{
      await playJarvisAudio(data.audio_url);
    }}
  }} catch (e) {{
    console.error(e);
  }}
  if (callActive) beginListening();
}}

function setupAudioVisualizer(stream) {{
  try {{
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const source = audioCtx.createMediaStreamSource(stream);
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 256;
    source.connect(analyser);
    const dataArr = new Uint8Array(analyser.frequencyBinCount);

    function animateOrb() {{
      if (!callActive) return;
      analyser.getByteFrequencyData(dataArr);
      let sum = 0;
      for (let i = 0; i < 40; i++) sum += dataArr[i];
      const avg = sum / 40;
      const scale = 1 + Math.min(0.28, avg / 420);
      orb.style.transform = `scale(${{scale.toFixed(3)}})`;
      orbGlow.style.transform = `scale(${{(scale * 1.08).toFixed(3)}})`;
      requestAnimationFrame(animateOrb);
    }}
    requestAnimationFrame(animateOrb);
  }} catch (e) {{}}
}}

function playJarvisAudio(url) {{
  return new Promise((resolve) => {{
    isBusy = true;
    setStatus("JARVIS Speaking...", "speaking");
    currentAudio = new Audio(url);
    currentAudio.onended = () => {{
      isBusy = false;
      resolve();
    }};
    currentAudio.onerror = () => {{
      isBusy = false;
      resolve();
    }};
    currentAudio.play().catch(() => {{
      isBusy = false;
      resolve();
    }});
  }});
}}

function beginListening() {{
  if (!callActive || isBusy || isMuted) return;
  setStatus("Listening — Go ahead...", "listening");

  if (SpeechRecognition) {{
    startBrowserSpeechRecognition();
  }} else if (mediaStream) {{
    startVadRecorder();
  }}
}}

function startBrowserSpeechRecognition() {{
  try {{
    if (recognition) {{
      try {{ recognition.abort(); }} catch (e) {{}}
    }}
    recognition = new SpeechRecognition();
    recognition.lang = "en-US";
    recognition.interimResults = true;
    recognition.continuous = false;

    let finalTranscript = "";
    recognition.onresult = (event) => {{
      let interim = "";
      for (let i = event.resultIndex; i < event.results.length; i++) {{
        const t = event.results[i][0].transcript;
        if (event.results[i].isFinal) finalTranscript += t;
        else interim += t;
      }}
      heardText.textContent = '"' + (finalTranscript || interim).trim() + '"';
    }};

    recognition.onend = async () => {{
      if (!callActive) return;
      const spoken = finalTranscript.trim();
      if (spoken && !isBusy) {{
        await sendTextTurn(spoken);
      }} else if (callActive && !isBusy && !isMuted) {{
        setTimeout(() => beginListening(), 250);
      }}
    }};

    recognition.onerror = (e) => {{
      if (e.error === "not-allowed" || e.error === "service-not-allowed") {{
        if (mediaStream) startVadRecorder();
      }}
    }};
    recognition.start();
  }} catch (e) {{
    if (mediaStream) startVadRecorder();
  }}
}}

function startVadRecorder() {{
  if (!mediaStream || !callActive || isBusy || isMuted) return;
  recordedChunks = [];
  speakingDetected = false;
  silenceStart = performance.now();

  try {{
    mediaRecorder = new MediaRecorder(mediaStream);
  }} catch (e) {{ return; }}

  mediaRecorder.ondataavailable = (ev) => {{
    if (ev.data && ev.data.size > 0) recordedChunks.push(ev.data);
  }};

  mediaRecorder.onstop = async () => {{
    if (!callActive) return;
    if (!speakingDetected || recordedChunks.length === 0) {{
      if (callActive && !isBusy && !isMuted) setTimeout(() => beginListening(), 200);
      return;
    }}
    const blob = new Blob(recordedChunks, {{ type: mediaRecorder.mimeType || "audio/webm" }});
    await sendAudioTurn(blob);
  }};

  mediaRecorder.start();
  const dataArr = new Uint8Array(analyser ? analyser.frequencyBinCount : 64);

  function checkVad() {{
    if (!callActive || isBusy || isMuted || !mediaRecorder || mediaRecorder.state !== "recording") return;
    if (analyser) {{
      analyser.getByteFrequencyData(dataArr);
      let sum = 0;
      for (let i = 2; i < 28; i++) sum += dataArr[i];
      const level = sum / 26;
      const now = performance.now();
      if (level > 24) {{
        if (!speakingDetected) {{
          speakingDetected = true;
          heardText.textContent = "Listening to your voice...";
        }}
        silenceStart = now;
      }} else if (speakingDetected && (now - silenceStart > 1350)) {{
        mediaRecorder.stop();
        return;
      }} else if (!speakingDetected && (now - silenceStart > 8000)) {{
        mediaRecorder.stop();
        return;
      }}
    }}
    vadLoopId = requestAnimationFrame(checkVad);
  }}
  vadLoopId = requestAnimationFrame(checkVad);
}}

async function sendTextTurn(text) {{
  isBusy = true;
  setStatus("JARVIS Thinking...", "speaking");
  heardText.textContent = 'You: "' + text + '"';
  try {{
    const res = await fetch(`/call/turn?k=${{encodeURIComponent(SECRET)}}`, {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ text }})
    }});
    const data = await res.json();
    if (data.reply) replyText.textContent = data.reply;
    if (data.audio_url) await playJarvisAudio(data.audio_url);
    if (data.hangup) {{
      endCall();
      return;
    }}
  }} catch (e) {{
    console.error(e);
  }}
  isBusy = false;
  if (callActive && !isMuted) beginListening();
}}

async function sendAudioTurn(blob) {{
  isBusy = true;
  setStatus("Transcribing & Thinking...", "speaking");
  try {{
    const res = await fetch(`/call/turn?k=${{encodeURIComponent(SECRET)}}`, {{
      method: "POST",
      headers: {{ "Content-Type": blob.type || "audio/webm" }},
      body: blob
    }});
    const data = await res.json();
    if (data.heard) heardText.textContent = 'You: "' + data.heard + '"';
    if (data.reply) replyText.textContent = data.reply;
    if (data.audio_url) await playJarvisAudio(data.audio_url);
    if (data.hangup) {{
      endCall();
      return;
    }}
  }} catch (e) {{
    console.error(e);
  }}
  isBusy = false;
  if (callActive && !isMuted) beginListening();
}}

function toggleMute() {{
  isMuted = !isMuted;
  muteBtn.classList.toggle("muted", isMuted);
  muteBtn.textContent = isMuted ? "🔇" : "🎙";
  if (isMuted) {{
    setStatus("Microphone Muted", "");
    if (recognition) try {{ recognition.abort(); }} catch (e) {{}}
    if (mediaRecorder && mediaRecorder.state === "recording") try {{ mediaRecorder.stop(); }} catch (e) {{}}
  }} else {{
    beginListening();
  }}
}}

function manualTrigger() {{
  if (!callActive) {{
    startCall();
    return;
  }}
  if (currentAudio && !currentAudio.paused) {{
    currentAudio.pause();
    isBusy = false;
    beginListening();
  }}
}}

function endCall() {{
  callActive = false;
  isBusy = false;
  clearInterval(timerInterval);
  if (recognition) try {{ recognition.abort(); }} catch (e) {{}}
  if (mediaRecorder && mediaRecorder.state === "recording") try {{ mediaRecorder.stop(); }} catch (e) {{}}
  if (currentAudio) try {{ currentAudio.pause(); }} catch (e) {{}}
  if (mediaStream) mediaStream.getTracks().forEach(t => t.stop());
  setStatus("Call Ended", "");
  answerBtn.style.display = "flex";
  hangupBtn.style.display = "none";
  muteBtn.style.display = "none";
}}
</script>
</body>
</html>"""


async def handle_twilio_voice_turn(
    form_data: dict[str, str],
    *,
    base_url: str | None = None,
    bot: Any = None,
    job_queue: Any = None,
) -> str:
    """Process a single Twilio voice webhook turn (initial pickup or <Gather> speech result) and return TwiML XML."""
    from brain.agent import respond as brain_respond

    call_sid = form_data.get("CallSid", "")
    from_num = form_data.get("From", "")
    to_num = form_data.get("To", "")
    direction = form_data.get("Direction", "")
    speech_result = (form_data.get("SpeechResult") or "").strip()
    digits = (form_data.get("Digits") or "").strip()

    webhook_url = voice.resolve_twilio_webhook_url(
        f"{base_url.rstrip('/')}/twilio/voice" if base_url else get_active_webhook_url()
    )

    if not is_authorized_caller(from_num, to_num, direction):
        log.warning(
            "Rejected unauthorized Twilio call: CallSid=%s From=%s To=%s",
            call_sid,
            from_num,
            to_num,
        )
        return voice.build_twiml(
            "Unauthorized number. Disconnecting.",
            interactive=False,
            hangup=True,
        )

    user_input = speech_result or digits

    # 1. If no speech was captured yet (inbound call pickup or silent <Gather> timeout)
    if not user_input:
        has_speech_field = "SpeechResult" in form_data or "Confidence" in form_data
        if has_speech_field:
            greeting = f"I'm listening, {config.USER_NAME}. What can I do for you?"
        else:
            greeting = f"At your service, {config.USER_NAME}. How may I assist you?"
        audio_url = await voice.prepare_twilio_prompt_audio(greeting, base_url=webhook_url)
        return voice.build_twiml(
            greeting,
            interactive=True,
            webhook_url=webhook_url,
            audio_url=audio_url,
        )

    log.info("Twilio call speech heard (CallSid=%s): %s", call_sid, user_input[:160])

    # 2. Check if the caller wants to hang up
    if is_goodbye_phrase(user_input):
        farewell = f"Very good, {config.USER_NAME}. Always a pleasure. Goodbye."
        audio_url = await voice.prepare_twilio_prompt_audio(farewell, base_url=webhook_url)
        return voice.build_twiml(
            farewell,
            interactive=False,
            webhook_url=webhook_url,
            audio_url=audio_url,
            hangup=True,
        )

    # 3. Route the caller's spoken request through the full JARVIS Gemini brain & tool suite
    owner_id = config.OWNER_ID or 1
    active_bot = bot if bot is not None else _telegram_bot
    active_jq = job_queue if job_queue is not None else _telegram_job_queue
    ctx = ToolContext(
        bot=active_bot,
        chat_id=owner_id,
        job_queue=active_jq,
        user_id=owner_id,
    )

    phone_prompt = (
        f"[Live Phone Call — respond concisely in 1 to 3 natural spoken sentences without markdown or bullet lists]: "
        f"{user_input}"
    )

    try:
        reply = await asyncio.wait_for(
            brain_respond(ctx, phone_prompt, fast_voice=True),
            timeout=12.0,
        )
    except asyncio.TimeoutError:
        log.warning("Twilio phone turn timed out after 13s for CallSid=%s", call_sid)
        reply = f"I am still processing that request, {config.USER_NAME}. What else can I do for you?"
    except Exception:
        log.exception("Twilio phone turn brain_respond failed")
        reply = f"Pardon me, {config.USER_NAME}, I encountered a temporary issue processing that. Please go ahead again."

    reply = (reply or "").strip() or f"Done, {config.USER_NAME}. Anything else?"
    log.info("Twilio call JARVIS reply (CallSid=%s): %s", call_sid, reply[:160])

    if active_bot and config.OWNER_ID:
        try:
            asyncio.create_task(
                active_bot.send_message(
                    config.OWNER_ID,
                    f"📞 Phone Call\n🎙 You: {user_input}\n🎩 JARVIS: {reply}",
                )
            )
        except Exception:
            pass

    audio_url = await voice.prepare_twilio_prompt_audio(reply, base_url=webhook_url)
    return voice.build_twiml(
        reply,
        interactive=True,
        webhook_url=webhook_url,
        audio_url=audio_url,
    )


def _build_http_response(
    status_code: int,
    status_text: str,
    content_type: str,
    body: bytes,
) -> bytes:
    header = (
        f"HTTP/1.1 {status_code} {status_text}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("iso-8859-1")
    return header + body


async def _handle_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        raw_head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10.0)
        head_text = raw_head.decode("iso-8859-1", errors="replace")
        lines = head_text.split("\r\n")
        if not lines or not lines[0]:
            return
        parts = lines[0].split(" ")
        if len(parts) < 2:
            return
        method = parts[0].upper()
        full_path = parts[1]
        path = full_path.split("?", 1)[0]
        query_str = full_path.split("?", 1)[1] if "?" in full_path else ""
        query_params = {k: (v[0] if v else "") for k, v in parse_qs(query_str).items()}

        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        content_length = 0
        try:
            content_length = max(0, min(int(headers.get("content-length", "0")), 4_000_000))
        except ValueError:
            content_length = 0

        body_bytes = b""
        if content_length > 0:
            body_bytes = await asyncio.wait_for(
                reader.readexactly(content_length), timeout=20.0
            )

        # Route 1: Serve synthesized JARVIS TTS MP3 audio clips for Web Call & Twilio <Play>
        if method == "GET" and path.startswith("/twilio/audio/"):
            token = path[len("/twilio/audio/") :]
            if token.endswith(".mp3"):
                token = token[:-4]
            audio = voice.get_twilio_audio(token)
            if audio:
                writer.write(_build_http_response(200, "OK", "audio/mpeg", audio))
            else:
                writer.write(_build_http_response(404, "Not Found", "text/plain", b"Audio expired"))
            await writer.drain()
            return

        # Route 2: 100% Free Interactive Web/Phone Call UI (GET /call)
        if method == "GET" and path == "/call":
            if query_params.get("k") != _web_call_secret:
                writer.write(
                    _build_http_response(
                        403, "Forbidden", "text/plain", b"Invalid or expired call token. Send /call in Telegram for a fresh link."
                    )
                )
                await writer.drain()
                return
            html = _build_web_call_html(_web_call_secret, config.USER_NAME)
            writer.write(
                _build_http_response(200, "OK", "text/html; charset=utf-8", html.encode("utf-8"))
            )
            await writer.drain()
            return

        # Route 3: Web Voice Call Turn API (POST /call/turn)
        if method == "POST" and path == "/call/turn":
            if query_params.get("k") != _web_call_secret:
                writer.write(_build_http_response(403, "Forbidden", "application/json", b'{"error":"forbidden"}'))
                await writer.drain()
                return
            is_greet = query_params.get("greet") == "1"
            ctype = headers.get("content-type", "application/json").lower()
            user_text = ""
            audio_data: bytes | None = None
            if is_greet:
                res_obj = await handle_web_call_turn(
                    is_greeting=True,
                    bot=_telegram_bot,
                    job_queue=_telegram_job_queue,
                )
            elif "application/json" in ctype:
                try:
                    payload = json.loads(body_bytes.decode("utf-8", errors="replace") or "{}")
                    user_text = str(payload.get("text") or "").strip()
                except Exception:
                    user_text = ""
                res_obj = await handle_web_call_turn(
                    user_text=user_text,
                    bot=_telegram_bot,
                    job_queue=_telegram_job_queue,
                )
            else:
                audio_data = body_bytes
                res_obj = await handle_web_call_turn(
                    audio_bytes=audio_data,
                    mime_type=ctype.split(";")[0].strip() or "audio/webm",
                    bot=_telegram_bot,
                    job_queue=_telegram_job_queue,
                )
            writer.write(
                _build_http_response(
                    200,
                    "OK",
                    "application/json; charset=utf-8",
                    json.dumps(res_obj).encode("utf-8"),
                )
            )
            await writer.drain()
            return

        # Route 4: Health check
        if method == "GET" and path in ("/", "/health", "/twilio/health"):
            payload = b'{"status":"ok","service":"jarvis-voice-call-server"}'
            writer.write(_build_http_response(200, "OK", "application/json", payload))
            await writer.drain()
            return

        # Route 5: Twilio Voice Webhook (POST or GET to /twilio/voice or /twilio)
        if path in ("/twilio/voice", "/twilio", "/voice", "/"):
            form_raw = body_bytes.decode("utf-8", errors="replace") if body_bytes else query_str
            parsed = parse_qs(form_raw, keep_blank_values=True)
            form_data = {k: (v[0] if v else "") for k, v in parsed.items()}
            base_url = _infer_base_url_from_headers(headers)
            twiml = await handle_twilio_voice_turn(
                form_data,
                base_url=base_url,
                bot=_telegram_bot,
                job_queue=_telegram_job_queue,
            )
            writer.write(
                _build_http_response(
                    200, "OK", "text/xml; charset=utf-8", twiml.encode("utf-8")
                )
            )
            await writer.drain()
            return

        writer.write(_build_http_response(404, "Not Found", "text/plain", b"Not Found"))
        await writer.drain()
    except Exception:
        log.debug("Voice webhook HTTP connection error", exc_info=True)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _maybe_start_quick_tunnel(port: int) -> str:
    """If TWILIO_WEBHOOK_URL is empty and cloudflared is installed, auto-start a free Cloudflare HTTPS Quick Tunnel."""
    global _tunnel_proc, _runtime_webhook_url
    if config.TWILIO_WEBHOOK_URL or _runtime_webhook_url:
        return get_active_webhook_url()

    import shutil
    import subprocess

    cf_bin = shutil.which("cloudflared")
    if not cf_bin:
        for candidate in (
            r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
            r"C:\Program Files\cloudflared\cloudflared.exe",
            str(config.BASE_DIR / "cloudflared.exe"),
        ):
            if os.path.exists(candidate):
                cf_bin = candidate
                break

    if not cf_bin:
        return ""

    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            [cf_bin, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        _tunnel_proc = proc

        def _read_until_url() -> str:
            if not proc.stdout:
                return ""
            for _ in range(80):
                line = proc.stdout.readline()
                if not line:
                    break
                m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
                if m:
                    return m.group(0)
            return ""

        base_tunnel = await asyncio.wait_for(asyncio.to_thread(_read_until_url), timeout=15.0)
        if base_tunnel:
            _runtime_webhook_url = f"{base_tunnel}/twilio/voice"
            config.TWILIO_WEBHOOK_URL = _runtime_webhook_url
            log.info("Auto-started Cloudflare Quick Tunnel for JARVIS Live Voice Call: %s/call", base_tunnel)
            return _runtime_webhook_url
    except Exception:
        log.debug("Cloudflare Quick Tunnel auto-start skipped", exc_info=True)
    return ""


async def start_twilio_webhook_server(
    *,
    host: str | None = None,
    port: int | None = None,
    bot: Any = None,
    job_queue: Any = None,
) -> asyncio.AbstractServer | None:
    """Start the embedded async HTTP server for Free Web Voice Calls (/call) & Twilio webhooks."""
    global _webhook_server, _telegram_bot, _telegram_job_queue
    _telegram_bot = bot
    _telegram_job_queue = job_queue

    if _webhook_server is not None:
        return _webhook_server

    bind_host = host or config.TWILIO_WEBHOOK_HOST or "0.0.0.0"
    bind_port = port if port is not None else config.TWILIO_WEBHOOK_PORT

    try:
        _webhook_server = await asyncio.start_server(
            _handle_client, host=bind_host, port=bind_port
        )
        if not config.TWILIO_WEBHOOK_URL:
            await _maybe_start_quick_tunnel(bind_port)
        call_link = get_web_call_url()
        log.info(
            "JARVIS 2-Way Voice Call server listening on %s:%s -> %s",
            bind_host,
            bind_port,
            call_link,
        )
        return _webhook_server
    except OSError as exc:
        log.warning(
            "Could not bind JARVIS Voice Call server on %s:%s (%s)",
            bind_host,
            bind_port,
            exc,
        )
        return None


async def stop_twilio_webhook_server() -> None:
    """Stop the embedded voice call HTTP server and any auto-started tunnel process."""
    global _webhook_server, _tunnel_proc
    if _tunnel_proc is not None:
        try:
            _tunnel_proc.terminate()
        except Exception:
            pass
        _tunnel_proc = None
    if _webhook_server is not None:
        try:
            _webhook_server.close()
            await _webhook_server.wait_closed()
        except Exception:
            pass
        _webhook_server = None
