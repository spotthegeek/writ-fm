"""crouch-tts client: Breeze TTS 2, Qwen3-TTS VoiceDesign and Phonon, served from mac01.

The service (agent-os bootstrap/mac01/crouch-tts) speaks the OpenAI-style API the Kokoro service does:
POST {MAC_TTS_SERVICE_URL}/v1/audio/speech with {"model": "breeze"|"qwen"|"phonon", "input", "voice",
"response_format": "wav", "instruct"?} returns 24 kHz mono 16-bit WAV, the same format Kokoro writes,
so the existing chunk/gap/concat paths can join it with `-c copy`. Voices are designed once on the
service and named (GET /v1/voices; Phonon has its own list, GET /v1/voices?model=phonon). The service renders
one request at a time at roughly 0.25-0.7 s per second of audio (Phonon about 0.02 s), so the read timeout is long; a refused or timed-out connection (the daemon is stopped
during H3 video renders on mac01) fails fast so the generator's fallback backend can take over.
"""
from __future__ import annotations

import time
from pathlib import Path

import httpx

from shared.settings import mac_tts_max_retries, mac_tts_service_url, mac_tts_timeout_seconds

MODELS = ("breeze", "qwen", "phonon")
_VOICE_CACHE: dict[str, dict] = {}      # per voice list: Breeze and Qwen share one, Phonon has its own
_VOICE_CACHE_TTL = 300.0


def _log(msg: str) -> None:
    print(msg, flush=True)


def generate_speech(text: str, output_path: Path, voice_id: str, *, model: str = "breeze",
                    instruct: str | None = None) -> bool:
    url = mac_tts_service_url()
    if not url:
        _log("  crouch-tts: MAC_TTS_SERVICE_URL is not set")
        return False
    if model not in MODELS:
        _log(f"  crouch-tts: unknown model {model!r}")
        return False
    body = {"model": model, "input": text, "voice": voice_id, "response_format": "wav"}
    if instruct:
        body["instruct"] = instruct
    timeout = httpx.Timeout(mac_tts_timeout_seconds(), connect=10.0)
    attempts = mac_tts_max_retries()
    for attempt in range(1, attempts + 1):
        try:
            resp = httpx.post(f"{url}/v1/audio/speech", json=body, timeout=timeout)
            if resp.status_code < 400 and resp.content[:4] == b"RIFF":
                Path(output_path).write_bytes(resp.content)
                return True
            detail = resp.text[:200] if resp.status_code >= 400 else "response was not a WAV file"
            _log(f"  crouch-tts {model}/{voice_id}: HTTP {resp.status_code}: {detail}")
            if resp.status_code < 500:
                return False                      # a bad voice or request will not improve on retry
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            _log(f"  crouch-tts unreachable ({e.__class__.__name__}); not retrying")
            return False
        except httpx.HTTPError as e:
            _log(f"  crouch-tts {model}/{voice_id}: {e.__class__.__name__}: {e}")
        if attempt < attempts:
            time.sleep(2 * attempt)
    return False


def list_voices(model: str = "breeze") -> list[dict]:
    """A backend's voices on the service, cached for five minutes; [] if it cannot be reached quickly."""
    phonon = model == "phonon"
    cache = _VOICE_CACHE.setdefault("phonon" if phonon else "designed", {"at": 0.0, "voices": []})
    now = time.time()
    if now - float(cache["at"]) < _VOICE_CACHE_TTL and cache["voices"]:
        return list(cache["voices"])
    url = mac_tts_service_url()
    if not url:
        return []
    try:
        voices = httpx.get(f"{url}/v1/voices", params={"model": "phonon"} if phonon else None, timeout=3.0).json().get("voices", [])
    except Exception:
        return list(cache["voices"])
    cache.update(at=now, voices=voices)
    return list(voices)
