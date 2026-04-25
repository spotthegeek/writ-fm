#!/usr/bin/env python3
"""MiniMax music-2.6 client for WRIT-FM bumper generation.

Replaces the previous ACE-Step (localhost:4009) integration.
API: POST https://api.minimax.io/v1/music_generation
Returns hex-encoded MP3 audio (~130s per generation).
"""

import binascii
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from shared.settings import minimax_music_model

MINIMAX_BASE_URL = "https://api.minimax.io/v1"
MINIMAX_MODEL = minimax_music_model()

# Legacy env var kept for health-check callers that test server availability.
MUSIC_GEN_BASE_URL = os.environ.get("MUSIC_GEN_URL", "http://localhost:4009")


def is_server_available(base_url: str = MUSIC_GEN_BASE_URL, timeout: float = 2.0) -> bool:
    """Check if music generation is available. For MiniMax, just verify API key is set."""
    return bool(_music_api_key())


def _music_api_key() -> str:
    """Prefer the token-plan key for music generation, falling back safely."""
    return (
        os.environ.get("MINIMAX_TOKEN_PLAN_API_KEY", "").strip()
        or os.environ.get("MINIMAX_API_KEY", "").strip()
    )


def generate_music(
    caption: str,
    output_path: Path,
    duration: float = 90.0,
    audio_format: str = "mp3",
    seed: int = -1,
    instrumental: bool = True,
    lyrics: str = "[Instrumental]",
    guidance_scale: float = 0.0,
    base_url: str = MUSIC_GEN_BASE_URL,
    timeout: float = 300.0,
) -> bool:
    """Generate music via MiniMax music-2.6 and save to output_path.

    Args:
        caption: Text description of music style/mood (used as prompt).
        output_path: Where to save the generated audio file (.mp3).
        duration: Ignored — MiniMax generates ~130s tracks regardless.
        audio_format: Ignored — MiniMax always returns MP3.
        seed: Ignored — MiniMax does not support seeding.
        instrumental: If True, no vocals generated.
        lyrics: Lyrics text (used when instrumental=False).
        guidance_scale: Ignored.
        base_url: Ignored (legacy ACE-Step param).
        timeout: HTTP request timeout in seconds.

    Returns:
        True if successful, False otherwise.
    """
    api_key = _music_api_key()
    if not api_key:
        print("[music_gen] MINIMAX_TOKEN_PLAN_API_KEY not set")
        return False

    payload: dict = {
        "model": MINIMAX_MODEL,
        "prompt": caption,
        "audio_setting": {
            "sample_rate": 44100,
            "bitrate": 256000,
            "format": "mp3",
        },
    }
    if instrumental:
        payload["is_instrumental"] = True
    else:
        payload["lyrics"] = lyrics if lyrics and lyrics != "[Instrumental]" else "[Instrumental]\n"
        payload["is_instrumental"] = False

    req = urllib.request.Request(
        f"{MINIMAX_BASE_URL}/music_generation",
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
        print(f"[music_gen] HTTP {e.code}: {e.read().decode()[:300]}")
        return False
    except Exception as e:
        print(f"[music_gen] Request failed: {e}")
        return False

    base_resp = data.get("base_resp", {})
    if base_resp.get("status_code", -1) != 0:
        print(f"[music_gen] API error {base_resp.get('status_code')}: {base_resp.get('status_msg')}")
        return False

    audio_hex = data.get("data", {}).get("audio", "")
    if not audio_hex:
        print("[music_gen] No audio in response")
        return False

    try:
        audio_bytes = binascii.unhexlify(audio_hex)
        # Always save as .mp3 regardless of requested format
        out = output_path.with_suffix(".mp3")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(audio_bytes)
        if out != output_path:
            # Caller expected a different extension — rename so callers find it
            output_path.parent.mkdir(parents=True, exist_ok=True)
            out.rename(output_path) if output_path.suffix == ".mp3" else None
        return True
    except Exception as e:
        print(f"[music_gen] Failed to save audio: {e}")
        return False
