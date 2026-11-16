"""Helper script to automatically create an ElevenLabs Marvel JARVIS Instant Voice Clone

Uses the combined studio-quality training clips generated from openpeon-jarvis.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
import httpx
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)

API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
SAMPLE_PATH = BASE_DIR / "assets" / "elevenlabs_samples" / "jarvis_training_sample_full.mp3"


def main() -> None:
    print("=" * 60)
    print(" ElevenLabs Marvel JARVIS Voice Clone Assistant")
    print("=" * 60)

    if not SAMPLE_PATH.exists():
        print(f"Error: Training sample not found at {SAMPLE_PATH}")
        sys.exit(1)

    dur_sec = SAMPLE_PATH.stat().st_size / 16000.0
    print(f"Loaded master training sample: {SAMPLE_PATH.name} (~{dur_sec:.1f} seconds)")

    if not API_KEY:
        print("\n[!] No ELEVENLABS_API_KEY found in .env.")
        print("\nYou can easily upload the sample manually in the web dashboard:")
        print("  1. Go to https://elevenlabs.io/app/voice-lab")
        print("  2. Click 'Add Generative or Cloned Voice' -> 'Instant Voice Clone'")
        print(f"  3. Drag and drop this audio file:")
        print(f"     {SAMPLE_PATH.resolve()}")
        print("  4. Name it 'Marvel JARVIS' and click Create Voice.")
        print("  5. Copy the Voice ID and set ELEVENLABS_VOICE_ID in your .env file.\n")
        return

    print("\nFound ELEVENLABS_API_KEY. Creating instant voice clone via API...")
    url = "https://api.elevenlabs.io/v1/voices/add"
    headers = {"xi-api-key": API_KEY}

    try:
        with open(SAMPLE_PATH, "rb") as f:
            files = [("files", (SAMPLE_PATH.name, f, "audio/mpeg"))]
            data = {
                "name": "Marvel JARVIS (Paul Bettany)",
                "description": "MCU JARVIS AI butler clone created from openpeon-jarvis studio samples.",
            }
            resp = httpx.post(url, headers=headers, data=data, files=files, timeout=45.0)

        if resp.status_code == 200:
            voice_id = resp.json().get("voice_id")
            print(f"\n[SUCCESS] Cloned Marvel JARVIS Voice successfully!")
            print(f"Voice ID: {voice_id}")

            # Update .env
            env_path = BASE_DIR / ".env"
            content = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
            if "ELEVENLABS_VOICE_ID=" in content:
                import re
                new_content = re.sub(r"ELEVENLABS_VOICE_ID=.*", f"ELEVENLABS_VOICE_ID={voice_id}", content)
            else:
                new_content = content.rstrip() + f"\nELEVENLABS_VOICE_ID={voice_id}\n"
            env_path.write_text(new_content, encoding="utf-8")
            print(f"Saved ELEVENLABS_VOICE_ID={voice_id} directly into your .env file!")
        else:
            print(f"\n[!] API returned status {resp.status_code}: {resp.text}")
    except Exception as exc:
        print(f"\n[!] Error contacting ElevenLabs: {exc}")


if __name__ == "__main__":
    main()
