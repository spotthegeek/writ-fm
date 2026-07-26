#!/usr/bin/env python3
"""Google Lyria 2 client for WRIT-FM bumper generation.

API: POST https://{location}-aiplatform.googleapis.com/v1/projects/{project}
         /locations/{location}/publishers/google/models/lyria-002:predict

Returns base64-encoded WAV. Unlike MiniMax (which always renders ~130s regardless
of the requested duration), Lyria is billed per 30s of output, so a 15-30s bumper
costs one unit instead of paying for ~110 discarded seconds.

Auth is Vertex AI OAuth, NOT an API key: set GOOGLE_APPLICATION_CREDENTIALS to a
service-account JSON with roles/aiplatform.user, or supply an access token
directly via GOOGLE_VERTEX_ACCESS_TOKEN.
"""

import base64
import binascii
import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from shared.settings import lyria_model, vertex_location, vertex_project

_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

# Cached across calls: minting a token per bumper is a wasted round-trip.
_credentials = None


def _access_token() -> str:
    """Bearer token for Vertex AI, from an explicit token or a service account."""
    explicit = os.environ.get("GOOGLE_VERTEX_ACCESS_TOKEN", "").strip()
    if explicit:
        return explicit

    global _credentials
    try:
        import google.auth
        import google.auth.transport.requests
    except ImportError:
        print("[lyria] google-auth not installed; run: uv add google-auth")
        return ""

    try:
        if _credentials is None:
            _credentials, _ = google.auth.default(scopes=[_SCOPE])
        if not _credentials.valid:
            _credentials.refresh(google.auth.transport.requests.Request())
        return _credentials.token or ""
    except Exception as exc:
        print(f"[lyria] could not obtain Vertex AI credentials: {exc}")
        return ""


def is_server_available() -> bool:
    """True when Lyria is configured well enough to attempt a generation."""
    if not vertex_project():
        return False
    if os.environ.get("GOOGLE_VERTEX_ACCESS_TOKEN", "").strip():
        return True
    # Check the file exists, not just that the variable is set: a stale or
    # mistyped path would otherwise look configured and fail on every bumper.
    creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    return bool(creds) and Path(creds).is_file()


def _endpoint() -> str:
    location, project = vertex_location(), vertex_project()
    return (
        f"https://{location}-aiplatform.googleapis.com/v1/projects/{project}"
        f"/locations/{location}/publishers/google/models/{lyria_model()}:predict"
    )


def _decode_audio(prediction: dict) -> bytes | None:
    """Extract the base64 WAV from a prediction.

    lyria-002 actually returns `bytesBase64Encoded` (verified against the live API
    on 2026-07-26), even though Google's published sample shows `audioContent`.
    Both are accepted so a change in either direction keeps working.
    """
    for field in ("bytesBase64Encoded", "audioContent", "audio"):
        blob = prediction.get(field)
        if not blob:
            continue
        try:
            return base64.b64decode(blob)
        except (binascii.Error, ValueError) as exc:
            print(f"[lyria] could not decode `{field}`: {exc}")
    return None


def _wav_to_mp3(wav_bytes: bytes, output_path: Path) -> bool:
    """Transcode to the .mp3 the bumper pipeline expects. Keeps WAV if ffmpeg is absent."""
    if output_path.suffix.lower() != ".mp3" or not shutil.which("ffmpeg"):
        output_path.write_bytes(wav_bytes)
        return True
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(wav_bytes)
        tmp_path = Path(tmp.name)
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(tmp_path), "-codec:a", "libmp3lame",
             "-b:a", "256k", str(output_path)],
            capture_output=True, timeout=120,
        )
        if result.returncode != 0 or not output_path.exists():
            print(f"[lyria] ffmpeg transcode failed: {result.stderr.decode()[:200]}")
            return False
        return True
    except Exception as exc:
        print(f"[lyria] ffmpeg transcode error: {exc}")
        return False
    finally:
        tmp_path.unlink(missing_ok=True)


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
    """Generate a bumper via Lyria 2 and save it to output_path.

    Signature mirrors music_gen_client.generate_music so the two are drop-in
    interchangeable. Lyria has no lyrics input and produces instrumental audio of a
    fixed length, so `duration`, `lyrics`, `guidance_scale` and `audio_format` are
    accepted but unused — the caller trims to its target length afterwards.
    """
    if not vertex_project():
        print("[lyria] GOOGLE_VERTEX_PROJECT (or GOOGLE_CLOUD_PROJECT) not set")
        return False
    token = _access_token()
    if not token:
        return False

    instance: dict = {"prompt": caption}
    if negative_prompt:
        instance["negative_prompt"] = negative_prompt
    if seed is not None and seed >= 0:
        instance["seed"] = int(seed)

    payload = {"instances": [instance], "parameters": {"sample_count": 1}}
    req = urllib.request.Request(
        _endpoint(),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:300]
        print(f"[lyria] API error {exc.code}: {detail}")
        return False
    except Exception as exc:
        print(f"[lyria] request failed: {exc}")
        return False

    predictions = body.get("predictions") or []
    if not predictions:
        print("[lyria] response contained no predictions")
        return False

    audio = _decode_audio(predictions[0])
    if not audio:
        print("[lyria] response contained no decodable audio")
        return False

    output_path.parent.mkdir(parents=True, exist_ok=True)
    return _wav_to_mp3(audio, output_path)
