"""Tests for the ACE-Step music backend: request shape, seeds, failures and provider selection."""
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from station import ace_step_client as ace  # noqa: E402


class Resp:
    def __init__(self, status=200, content=b"RIFFxxxx", text=""):
        self.status_code, self.content, self.text = status, content, text


@pytest.fixture
def harness(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_TTS_SERVICE_URL", "http://mac01:9003/")
    posts, written = [], []
    resp = {"value": Resp()}

    def post(url, json, timeout):
        posts.append((url, json))
        if isinstance(resp["value"], Exception):
            raise resp["value"]
        return resp["value"]

    monkeypatch.setattr(ace.httpx, "post", post)
    monkeypatch.setattr(ace, "_wav_to_mp3", lambda wav, path: written.append((wav, path)) or True)
    return posts, written, resp, tmp_path / "show" / "b.mp3"


def test_request_goes_to_crouch_tts_and_the_wav_is_transcoded(harness):
    posts, written, _, out = harness
    assert ace.generate_music("warm synth bed", out, duration=23.5, seed=7, lyrics="[verse]\nignored")
    assert posts == [("http://mac01:9003/v1/audio/music", {"prompt": "warm synth bed", "duration_seconds": 23.5, "seed": 7})]
    assert written == [(b"RIFFxxxx", out)]


def test_unset_seed_varies(harness):
    posts, _, _, out = harness
    for _ in range(4):
        ace.generate_music("x", out)
    seeds = {body["seed"] for _, body in posts}
    assert len(seeds) > 1 and all(0 <= s < 2**31 for s in seeds)


@pytest.mark.parametrize("failure", [Resp(503, b"{}", "not enough memory"), Resp(200, b"<html>"),
                                     httpx.ConnectError("refused")])
def test_failures_write_nothing(harness, failure):
    _, written, resp, out = harness
    resp["value"] = failure
    assert ace.generate_music("x", out) is False
    assert written == []


def test_no_service_url(monkeypatch, tmp_path):
    monkeypatch.delenv("MAC_TTS_SERVICE_URL", raising=False)
    assert ace.is_server_available() is False
    assert ace.generate_music("x", tmp_path / "b.mp3") is False


def test_generator_dispatches_and_labels_it_local(monkeypatch):
    from station.content_generator import music_bumper_generator as gen
    monkeypatch.setenv("WRIT_MUSIC_BACKEND", "ace-step")
    client, model = gen._music_client()
    assert client.__name__.endswith("ace_step_client")
    assert model == "ace-step-1.5-xl-turbo-8bit"
