"""Tests for the Lyria 2 music backend and provider selection.

Covers the parts that can be verified without a live Vertex AI project: request
shape, response decoding, availability gating, and that the bumper generator
dispatches to the configured provider.
"""
import base64
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import settings  # noqa: E402
from station import lyria_music_client as lyria  # noqa: E402


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in (
        "WRIT_MUSIC_BACKEND", "GOOGLE_VERTEX_PROJECT", "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_VERTEX_LOCATION", "GOOGLE_VERTEX_ACCESS_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS", "LYRIA_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    lyria._credentials = None


# ── settings ─────────────────────────────────────────────────────────────────

def test_defaults_to_lyria():
    assert settings.music_backend() == "lyria"
    assert settings.lyria_model() == "lyria-002"
    assert settings.vertex_location() == "us-central1"


def test_backend_is_overridable(monkeypatch):
    monkeypatch.setenv("WRIT_MUSIC_BACKEND", "MiniMax")
    assert settings.music_backend() == "minimax"


def test_project_falls_back_to_google_cloud_project(monkeypatch):
    assert settings.vertex_project() == ""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "fallback-proj")
    assert settings.vertex_project() == "fallback-proj"
    monkeypatch.setenv("GOOGLE_VERTEX_PROJECT", "explicit-proj")
    assert settings.vertex_project() == "explicit-proj"


# ── availability gating ──────────────────────────────────────────────────────

def test_unavailable_without_a_project(monkeypatch):
    monkeypatch.setenv("GOOGLE_VERTEX_ACCESS_TOKEN", "tok")
    assert lyria.is_server_available() is False


def test_unavailable_without_credentials(monkeypatch):
    monkeypatch.setenv("GOOGLE_VERTEX_PROJECT", "p")
    assert lyria.is_server_available() is False


def test_available_with_project_and_token(monkeypatch):
    monkeypatch.setenv("GOOGLE_VERTEX_PROJECT", "p")
    monkeypatch.setenv("GOOGLE_VERTEX_ACCESS_TOKEN", "tok")
    assert lyria.is_server_available() is True


def test_available_with_service_account(monkeypatch, tmp_path):
    sa = tmp_path / "sa.json"
    sa.write_text("{}")
    monkeypatch.setenv("GOOGLE_VERTEX_PROJECT", "p")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(sa))
    assert lyria.is_server_available() is True


def test_unavailable_when_credentials_path_is_missing(monkeypatch, tmp_path):
    """A mistyped path must read as unconfigured, not as ready-to-generate."""
    monkeypatch.setenv("GOOGLE_VERTEX_PROJECT", "p")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "nope.json"))
    assert lyria.is_server_available() is False


def test_generate_refuses_without_project(tmp_path, capsys):
    assert lyria.generate_music("warm synth bed", tmp_path / "b.mp3") is False
    assert "not set" in capsys.readouterr().out


# ── endpoint and request shape ───────────────────────────────────────────────

def test_endpoint_is_regional_and_versioned(monkeypatch):
    monkeypatch.setenv("GOOGLE_VERTEX_PROJECT", "my-proj")
    monkeypatch.setenv("GOOGLE_VERTEX_LOCATION", "europe-west4")
    assert lyria._endpoint() == (
        "https://europe-west4-aiplatform.googleapis.com/v1/projects/my-proj"
        "/locations/europe-west4/publishers/google/models/lyria-002:predict"
    )


def _wav() -> bytes:
    return b"RIFF$\x00\x00\x00WAVEfmt " + b"\x00" * 32


def _ok_response(field: str = "audioContent"):
    body = json.dumps(
        {"predictions": [{field: base64.b64encode(_wav()).decode(), "mimeType": "audio/wav"}]}
    ).encode()
    resp = mock.MagicMock()
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    return resp


def _generate(tmp_path, monkeypatch, **kwargs):
    monkeypatch.setenv("GOOGLE_VERTEX_PROJECT", "p")
    monkeypatch.setenv("GOOGLE_VERTEX_ACCESS_TOKEN", "tok-123")
    out = tmp_path / "bumper.mp3"
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        captured["body"] = json.loads(req.data.decode())
        return _ok_response()

    with mock.patch.object(lyria.urllib.request, "urlopen", fake_urlopen), \
         mock.patch.object(lyria, "_wav_to_mp3", lambda b, p: (p.write_bytes(b), True)[1]):
        ok = lyria.generate_music("warm synth bed", out, **kwargs)
    return ok, out, captured


def test_successful_generation_writes_audio(tmp_path, monkeypatch):
    ok, out, cap = _generate(tmp_path, monkeypatch)
    assert ok is True
    assert out.exists() and out.read_bytes().startswith(b"RIFF")
    assert cap["body"]["instances"][0]["prompt"] == "warm synth bed"
    assert cap["body"]["parameters"]["sample_count"] == 1


def test_bearer_token_is_sent(tmp_path, monkeypatch):
    _, _, cap = _generate(tmp_path, monkeypatch)
    headers = {k.lower(): v for k, v in cap["headers"].items()}
    assert headers["authorization"] == "Bearer tok-123"


def test_optional_params_are_omitted_when_unset(tmp_path, monkeypatch):
    _, _, cap = _generate(tmp_path, monkeypatch)
    instance = cap["body"]["instances"][0]
    assert "seed" not in instance
    assert "negative_prompt" not in instance


def test_optional_params_are_forwarded(tmp_path, monkeypatch):
    _, _, cap = _generate(tmp_path, monkeypatch, seed=42, negative_prompt="dissonant")
    instance = cap["body"]["instances"][0]
    assert instance["seed"] == 42
    assert instance["negative_prompt"] == "dissonant"


def test_minimax_only_params_are_accepted_and_ignored(tmp_path, monkeypatch):
    """Signature must stay drop-in compatible with music_gen_client."""
    ok, _, cap = _generate(
        tmp_path, monkeypatch,
        duration=18.5, instrumental=True, lyrics="[Instrumental]",
        guidance_scale=7.0, audio_format="mp3",
    )
    assert ok is True
    assert set(cap["body"]["instances"][0]) == {"prompt"}


# ── response handling ────────────────────────────────────────────────────────

def test_decodes_legacy_field_name():
    audio = lyria._decode_audio({"bytesBase64Encoded": base64.b64encode(_wav()).decode()})
    assert audio.startswith(b"RIFF")


def test_undecodable_audio_is_rejected():
    assert lyria._decode_audio({"audioContent": "!!!not base64!!!"}) is None
    assert lyria._decode_audio({}) is None


def test_empty_predictions_fail(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GOOGLE_VERTEX_PROJECT", "p")
    monkeypatch.setenv("GOOGLE_VERTEX_ACCESS_TOKEN", "tok")
    resp = mock.MagicMock()
    resp.read.return_value = json.dumps({"predictions": []}).encode()
    resp.__enter__.return_value = resp
    with mock.patch.object(lyria.urllib.request, "urlopen", lambda *a, **k: resp):
        assert lyria.generate_music("x", tmp_path / "b.mp3") is False
    assert "no predictions" in capsys.readouterr().out


def test_http_error_is_reported_not_raised(tmp_path, monkeypatch, capsys):
    import urllib.error
    monkeypatch.setenv("GOOGLE_VERTEX_PROJECT", "p")
    monkeypatch.setenv("GOOGLE_VERTEX_ACCESS_TOKEN", "tok")
    err = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
    err.read = lambda: b"permission denied on aiplatform"
    with mock.patch.object(lyria.urllib.request, "urlopen", mock.Mock(side_effect=err)):
        assert lyria.generate_music("x", tmp_path / "b.mp3") is False
    assert "403" in capsys.readouterr().out


# ── provider dispatch ────────────────────────────────────────────────────────

def test_generator_dispatches_to_configured_backend(monkeypatch):
    from station.content_generator import music_bumper_generator as gen

    monkeypatch.setenv("WRIT_MUSIC_BACKEND", "minimax")
    client, model = gen._music_client()
    assert client.__name__.endswith("music_gen_client")
    assert model == "music-2.6"

    monkeypatch.setenv("WRIT_MUSIC_BACKEND", "lyria")
    client, model = gen._music_client()
    assert client.__name__.endswith("lyria_music_client")
    assert model == "lyria-002"
