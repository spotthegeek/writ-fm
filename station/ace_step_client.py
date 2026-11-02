#!/usr/bin/env python3
"""ACE-Step 1.5 client for WRIT-FM bumper generation, served by crouch-tts on mac01.

POST {MAC_TTS_SERVICE_URL}/v1/audio/music with {"prompt", "duration_seconds", "seed"} returns an instrumental
48 kHz stereo WAV with ACE-Step\x27s trailing silence already cut, so a clip comes back 1.5-4 s shorter than asked. The
service stops the model when idle and refuses to load it without the memory, which is why the scheduler runs this
inside WRIT_GEN_WINDOW. A refused connection means an H3 render has the daemon stopped: fail fast and retry later.
"""

import random
from pathlib import Path

import httpx

from shared.settings import mac_tts_service_url
from station.lyria_music_client import _wav_to_mp3


def is_server_available() -> bool:
    return bool(mac_tts_service_url())


def generate_music(
    caption: str,
    output_path: Path,
    duration: float = 30.0,
    audio_format: str = "mp3",
    seed: int = -1,
    instrumental: bool = True,
    lyrics: str = "[Instrumental]",
    guidance_scale: float = 0.0,
    negative_prompt: str = "",
    timeout: float = 300.0,
) -> bool:
    """Generate a bumper via ACE-Step and save it to output_path.

    Signature mirrors lyria_music_client.generate_music. Instrumental only, as Lyria is, so `instrumental` and `lyrics`
    are accepted but unused, as are `audio_format`, `guidance_scale` and `negative_prompt`. The same seed gives the same
    clip, so an unset seed is randomised here.
    """
    url = mac_tts_service_url()
    if not url:
        print("[ace-step] MAC_TTS_SERVICE_URL is not set")
        return False
    body = {"prompt": caption, "duration_seconds": duration,
            "seed": seed if seed is not None and seed >= 0 else random.randrange(2**31)}
    try:
        resp = httpx.post(f"{url}/v1/audio/music", json=body, timeout=httpx.Timeout(timeout, connect=10.0))
    except httpx.HTTPError as exc:
        print(f"[ace-step] request failed: {exc.__class__.__name__}: {exc}")
        return False
    if resp.status_code >= 400 or resp.content[:4] != b"RIFF":
        detail = resp.text[:300] if resp.status_code >= 400 else "response was not a WAV file"
        print(f"[ace-step] HTTP {resp.status_code}: {detail}")
        return False
    output_path.parent.mkdir(parents=True, exist_ok=True)
    return _wav_to_mp3(resp.content, output_path)
