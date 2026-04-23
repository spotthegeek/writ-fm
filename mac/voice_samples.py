#!/usr/bin/env python3
"""Shared voice sample helpers for WRIT-FM.

This module keeps the voice catalog, sample text, cache layout, and
one-time sample generation logic in one place so the admin UI and any
maintenance scripts can stay in sync.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from mac.google_tts import VOICES as GOOGLE_VOICE_LABELS, generate_speech as generate_google_speech
from mac.kokoro.tts import VOICES as KOKORO_VOICE_LABELS, render_speech
from mac.minimax_tts import generate_speech

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VOICE_SAMPLE_DIR = PROJECT_ROOT / "output" / "voice_samples"

VOICE_SAMPLE_TEXT = (
    "This is a brief voice sample. Clear, steady, and easy to follow on air."
)

KOKORO_VOICES = list(KOKORO_VOICE_LABELS.keys())
KOKORO_VOICE_LABELS_ORDERED = [
    (voice_id, KOKORO_VOICE_LABELS[voice_id]) for voice_id in KOKORO_VOICES
]

MINIMAX_VOICES = [
    "Wise_Woman",
    "Friendly_Person",
    "Deep_Voice_Man",
    "Calm_Woman",
    "Casual_Guy",
    "Lively_Girl",
    "Patient_Man",
    "Elegant_Man",
]
GOOGLE_VOICES = list(GOOGLE_VOICE_LABELS.keys())
GOOGLE_VOICE_LABELS_ORDERED = [
    (voice_id, GOOGLE_VOICE_LABELS[voice_id]) for voice_id in GOOGLE_VOICES
]


def _safe_voice_id(voice_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", voice_id.strip()) or "voice"


def sample_path(backend: str, voice_id: str) -> Path:
    backend = backend.lower().strip()
    ext = ".mp3" if backend == "minimax" else ".wav"
    return VOICE_SAMPLE_DIR / backend / f"{_safe_voice_id(voice_id)}{ext}"


def sample_url(backend: str, voice_id: str) -> str:
    return f"/api/voice-samples/audio/{backend}/{voice_id}"


def sample_media_type(path: Path) -> str:
    return {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
    }.get(path.suffix.lower(), "application/octet-stream")


def voice_label(backend: str, voice_id: str) -> str:
    if backend == "kokoro":
        return KOKORO_VOICE_LABELS.get(voice_id, voice_id)
    if backend == "google":
        descriptor = GOOGLE_VOICE_LABELS.get(voice_id)
        return f"{voice_id} - {descriptor}" if descriptor else voice_id
    return voice_id.replace("_", " ")


def voice_catalog() -> dict[str, list[dict]]:
    return {
        "kokoro": [
            {
                "backend": "kokoro",
                "voice": voice_id,
                "label": voice_label("kokoro", voice_id),
                "sample_ready": sample_path("kokoro", voice_id).exists(),
                "sample_url": sample_url("kokoro", voice_id),
            }
            for voice_id in KOKORO_VOICES
        ],
        "minimax": [
            {
                "backend": "minimax",
                "voice": voice_id,
                "label": voice_label("minimax", voice_id),
                "sample_ready": sample_path("minimax", voice_id).exists(),
                "sample_url": sample_url("minimax", voice_id),
            }
            for voice_id in MINIMAX_VOICES
        ],
        "google": [
            {
                "backend": "google",
                "voice": voice_id,
                "label": voice_label("google", voice_id),
                "sample_ready": sample_path("google", voice_id).exists(),
                "sample_url": sample_url("google", voice_id),
            }
            for voice_id in GOOGLE_VOICES
        ],
    }


def ensure_voice_sample(
    backend: str,
    voice_id: str,
    *,
    force: bool = False,
    text: str = VOICE_SAMPLE_TEXT,
) -> Path:
    backend = backend.lower().strip()
    if backend not in {"kokoro", "minimax", "google"}:
        raise ValueError(f"Unknown backend: {backend}")

    out = sample_path(backend, voice_id)
    if out.exists() and not force:
        return out

    out.parent.mkdir(parents=True, exist_ok=True)
    if backend == "kokoro":
        ok = render_speech(text, out, voice=voice_id, allow_downloads=True)
    elif backend == "google":
        ok = generate_google_speech(text, out, voice_id=voice_id)
    else:
        ok = generate_speech(text, out, voice_id=voice_id)
    if not ok or not out.exists():
        raise RuntimeError(f"Failed to generate {backend} sample for {voice_id}")
    return out


def ensure_voice_samples(
    backends: Iterable[str] = ("kokoro", "minimax", "google"),
    *,
    force: bool = False,
) -> dict[str, list[str]]:
    created: dict[str, list[str]] = {}
    for backend in backends:
        backend = backend.lower().strip()
        if backend == "kokoro":
            voices = KOKORO_VOICES
        elif backend == "google":
            voices = GOOGLE_VOICES
        else:
            voices = MINIMAX_VOICES
        created[backend] = []
        for voice_id in voices:
            out = ensure_voice_sample(backend, voice_id, force=force)
            created[backend].append(str(out))
    return created
