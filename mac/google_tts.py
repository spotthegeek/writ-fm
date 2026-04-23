#!/usr/bin/env python3
"""Google Gemini TTS client for WRIT-FM.

Uses the Gemini Developer API TTS endpoint and saves PCM output as WAV.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path

GOOGLE_TTS_API_KEY = (
    os.environ.get("GOOGLE_TTS_API_KEY", "")
    or os.environ.get("GEMINI_API_KEY", "")
    or os.environ.get("GOOGLE_API_KEY", "")
)
GOOGLE_TTS_MODEL = os.environ.get("GOOGLE_TTS_MODEL", "gemini-3.1-flash-tts-preview")
GOOGLE_TTS_SAMPLE_RATE = 24000

VOICES = {
    "Zephyr": "Bright",
    "Puck": "Upbeat",
    "Charon": "Informative",
    "Kore": "Firm",
    "Fenrir": "Excitable",
    "Leda": "Youthful",
    "Orus": "Firm",
    "Aoede": "Breezy",
    "Callirrhoe": "Easy-going",
    "Autonoe": "Bright",
    "Enceladus": "Breathy",
    "Iapetus": "Clear",
    "Umbriel": "Easy-going",
    "Algieba": "Smooth",
    "Despina": "Smooth",
    "Erinome": "Clear",
    "Algenib": "Gravelly",
    "Rasalgethi": "Informative",
    "Laomedeia": "Upbeat",
    "Achernar": "Soft",
    "Alnilam": "Firm",
    "Schedar": "Even",
    "Gacrux": "Mature",
    "Pulcherrima": "Forward",
    "Achird": "Friendly",
    "Zubenelgenubi": "Casual",
    "Vindemiatrix": "Gentle",
    "Sadachbia": "Lively",
    "Sadaltager": "Knowledgeable",
    "Sulafat": "Warm",
}

DEFAULT_HOST_VOICE = "Kore"
DEFAULT_GUEST_VOICE = "Puck"


def _write_wav(output_path: Path, pcm_data: bytes, *, rate: int = GOOGLE_TTS_SAMPLE_RATE) -> Path:
    out = output_path.with_suffix(".wav")
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm_data)
    if out != output_path and output_path.suffix.lower() == ".wav":
        out.rename(output_path)
        return output_path
    return out


def generate_speech(
    text: str,
    output_path: Path,
    voice_id: str = DEFAULT_HOST_VOICE,
    *,
    wpm: int | None = None,
    model: str = GOOGLE_TTS_MODEL,
    timeout: float = 120.0,
) -> bool:
    """Generate speech via Gemini TTS and save to output_path (WAV)."""
    api_key = GOOGLE_TTS_API_KEY
    if not api_key:
        print("[google_tts] GOOGLE_TTS_API_KEY or GEMINI_API_KEY not set")
        return False

    prompt_text = text
    if wpm:
        prompt_text = (
            f"Read the quoted script aloud at approximately {int(wpm)} words per minute. "
            "Preserve the exact wording of the quoted script and speak naturally.\n\n"
            f'\"\"\"\n{text}\n\"\"\"'
        )

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": prompt_text,
                    }
                ]
            }
        ],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {
                        "voiceName": voice_id,
                    }
                }
            },
        },
    }

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{urllib.parse.quote(model, safe='')}:generateContent"
    )
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:500]
        print(f"[google_tts] HTTP {exc.code}: {body}")
        return False
    except Exception as exc:
        print(f"[google_tts] Request failed: {exc}")
        return False

    try:
        audio_b64 = data["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
    except Exception:
        print(f"[google_tts] No audio in response. Keys: {list(data.keys())}")
        return False

    try:
        pcm_data = base64.b64decode(audio_b64)
        _write_wav(output_path, pcm_data)
        return True
    except Exception as exc:
        print(f"[google_tts] Failed to save audio: {exc}")
        return False
