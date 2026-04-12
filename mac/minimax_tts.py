#!/usr/bin/env python3
"""MiniMax TTS client for WRIT-FM.

Uses MiniMax speech-2.8-hd model for high-quality cloud TTS.
API: POST https://api.minimax.io/v1/t2a_v2
Returns hex-encoded MP3 audio.

Available voices (English-capable):
    Wise_Woman          - Thoughtful, measured female
    Friendly_Person     - Casual, conversational
    Inspirational_Girl  - Energetic female
    Deep_Voice_Man      - Deep, resonant male
    Calm_Woman          - Calm, soothing female
    Casual_Guy          - Relaxed male
    Lively_Girl         - Bright, animated female
    Patient_Man         - Steady, patient male
    Confident_Man       - Authoritative male
    Elegant_Man         - Refined, measured male
"""

from __future__ import annotations

import binascii
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", "")
MINIMAX_BASE_URL = "https://api.minimax.io/v1"
MINIMAX_TTS_MODEL = "speech-2.8-hd"

# Voice map: our host IDs → MiniMax voice IDs
HOST_VOICE_MAP = {
    "liminal_operator": "Deep_Voice_Man",    # Dawn Chorus host - deep, late night
    "signal_keeper":    "Calm_Woman",         # The Signal - calm, measured
    "velvet_underground": "Wise_Woman",       # Velvet Hour - warm, intimate
    "midnight_sage":    "Patient_Man",        # Midnight Signal - thoughtful
    "morning_ritual":   "Friendly_Person",    # Morning Ritual - conversational
    "vinyl_vault":      "Elegant_Man",        # Vinyl Vault - refined
    "soul_kitchen":     "Casual_Guy",         # Soul Kitchen - relaxed
    "static_dreams":    "Inspirational_Girl", # Static Dreams - energetic
}

# Default fallbacks by gender
DEFAULT_MALE_VOICE = "Deep_Voice_Man"
DEFAULT_FEMALE_VOICE = "Wise_Woman"


def generate_speech(
    text: str,
    output_path: Path,
    voice_id: str = DEFAULT_MALE_VOICE,
    speed: float = 1.0,
    vol: float = 1.0,
    model: str = MINIMAX_TTS_MODEL,
    timeout: float = 120.0,
) -> bool:
    """Generate speech via MiniMax TTS and save to output_path (MP3).

    Args:
        text: Text to synthesize.
        output_path: Where to save the audio file.
        voice_id: MiniMax voice ID (see module docstring).
        speed: Speech speed (0.5–2.0).
        vol: Volume (0.1–10.0).
        model: MiniMax TTS model ID.
        timeout: HTTP request timeout in seconds.

    Returns:
        True if successful, False otherwise.
    """
    api_key = MINIMAX_API_KEY
    if not api_key:
        print("[minimax_tts] MINIMAX_API_KEY not set")
        return False

    payload = {
        "model": model,
        "text": text,
        "voice_setting": {
            "voice_id": voice_id,
            "speed": speed,
            "vol": vol,
            "pitch": 0,
        },
        "audio_setting": {
            "audio_sample_rate": 32000,
            "bitrate": 128000,
            "format": "mp3",
            "channel": 1,
        },
    }

    req = urllib.request.Request(
        f"{MINIMAX_BASE_URL}/t2a_v2",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:500]
        print(f"[minimax_tts] HTTP {e.code}: {body}")
        return False
    except Exception as e:
        print(f"[minimax_tts] Request failed: {e}")
        return False

    base_resp = data.get("base_resp", {})
    if base_resp.get("status_code", -1) != 0:
        print(f"[minimax_tts] API error {base_resp.get('status_code')}: {base_resp.get('status_msg')}")
        return False

    audio_hex = data.get("data", {}).get("audio", "")
    if not audio_hex:
        # Some versions return audio at top level
        audio_hex = data.get("audio", "")
    if not audio_hex:
        print(f"[minimax_tts] No audio in response. Keys: {list(data.keys())}")
        return False

    try:
        audio_bytes = binascii.unhexlify(audio_hex)
        out = output_path.with_suffix(".mp3")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(audio_bytes)
        # Rename to requested path if different extension
        if out != output_path:
            if not output_path.exists() or output_path.suffix != ".mp3":
                out.rename(output_path)
        return True
    except Exception as e:
        print(f"[minimax_tts] Failed to save audio: {e}")
        return False


def voice_for_host(host_id: str, gender: str = "male") -> str:
    """Return the best MiniMax voice for a WRIT-FM host."""
    if host_id in HOST_VOICE_MAP:
        return HOST_VOICE_MAP[host_id]
    return DEFAULT_MALE_VOICE if gender == "male" else DEFAULT_FEMALE_VOICE


if __name__ == "__main__":
    import argparse
    import time

    parser = argparse.ArgumentParser(description="MiniMax TTS test")
    parser.add_argument("text", nargs="?", default="This is WRIT-FM. The signal persists.")
    parser.add_argument("-o", "--output", default="/tmp/minimax_tts_test.mp3")
    parser.add_argument("-v", "--voice", default=DEFAULT_MALE_VOICE)
    parser.add_argument("--list-voices", action="store_true")
    args = parser.parse_args()

    if args.list_voices:
        print("Available voices:")
        voices = [
            "Wise_Woman", "Friendly_Person", "Inspirational_Girl", "Deep_Voice_Man",
            "Calm_Woman", "Casual_Guy", "Lively_Girl", "Patient_Man",
            "Confident_Man", "Elegant_Man",
        ]
        for v in voices:
            host = next((h for h, mv in HOST_VOICE_MAP.items() if mv == v), "")
            print(f"  {v:25s}  {'← ' + host if host else ''}")
    else:
        print(f"Generating speech with voice '{args.voice}'...")
        t0 = time.time()
        ok = generate_speech(args.text, Path(args.output), voice_id=args.voice)
        elapsed = time.time() - t0
        if ok:
            size = Path(args.output).stat().st_size // 1024
            print(f"Saved to {args.output} ({size} KB, {elapsed:.1f}s)")
        else:
            print("Failed.")
