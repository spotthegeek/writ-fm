"""Breeze and Qwen3-TTS on crouch-tts (mac01) as writ-fm backends, Breeze the default."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from shared import settings
from shared.hosts import assignment_voice
from station import mac_tts
from station.content_generator import helpers
from station.content_generator import talk_generator as tg

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 28


def test_breeze_is_the_default_backend(monkeypatch):
    monkeypatch.delenv("WRIT_DEFAULT_TTS_BACKEND", raising=False)
    assert settings.default_tts_backend() == "breeze"
    monkeypatch.setenv("WRIT_DEFAULT_TTS_BACKEND", "qwen")
    assert settings.default_tts_backend() == "qwen"
    monkeypatch.setenv("WRIT_DEFAULT_TTS_BACKEND", "nonsense")
    assert settings.default_tts_backend() == "breeze"


def test_fallback_backend_is_opt_in(monkeypatch):
    monkeypatch.delenv("WRIT_TTS_FALLBACK_BACKEND", raising=False)
    assert settings.tts_fallback_backend() == ""
    monkeypatch.setenv("WRIT_TTS_FALLBACK_BACKEND", "kokoro")
    assert settings.tts_fallback_backend() == "kokoro"


def test_voice_fields_per_backend():
    assert settings.voice_field("breeze") == "voice_breeze"
    assert settings.roster_voice_field("breeze") == "voice_breeze"
    assert settings.roster_voice_field("kokoro") == "tts_voice"   # Kokoro's older roster key


def test_assignment_voice_for_breeze_prefers_show_then_roster_then_default():
    roster = {"root": {"voice_breeze": "hank", "tts_voice": "am_liam"}}
    lookup = roster.get
    assert assignment_voice({"id": "root", "voice_breeze": "mick"}, "breeze", roster_lookup=lookup) == "mick"
    assert assignment_voice({"id": "root"}, "breeze", roster_lookup=lookup) == "hank"
    assert assignment_voice({"id": "root"}, "kokoro", roster_lookup=lookup) == "am_liam"
    assert assignment_voice({"id": "nobody"}, "breeze", roster_lookup=lookup) == settings.default_voice_for_backend("breeze")


def test_local_render_sends_breeze_to_crouch_tts(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(mac_tts, "generate_speech", lambda text, out, voice, model="breeze", instruct=None: calls.append((text, voice, model)) or True)
    assert tg._local_render_speech("qwen", "Hello.", tmp_path / "a.wav", voice="wren")
    assert calls == [("Hello.", "wren", "qwen")]


def test_an_unknown_backend_fails_loudly_instead_of_using_kokoro(monkeypatch, tmp_path):
    monkeypatch.setattr(tg, "_kokoro_render_speech", lambda *a, **k: pytest.fail("fell through to Kokoro"))
    assert tg._local_render_speech("brezee", "Hello.", tmp_path / "a.wav", voice="silas") is False


def test_preprocess_for_breeze_keeps_cues_and_drops_labels():
    out = helpers.preprocess_for_tts('HOST_A: [sigh] It is "always" DNS.\n(beat)\nRight.', backend="breeze")
    assert out.startswith("[sigh] It is always DNS.")
    assert "HOST_A" not in out and "(beat)" not in out


def test_client_writes_wav_and_does_not_retry_an_unreachable_service(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_TTS_SERVICE_URL", "http://crouch-tts.invalid:9003")
    seen = []

    def ok(url, json, timeout):
        seen.append(json)
        return httpx.Response(200, content=WAV)

    monkeypatch.setattr(mac_tts.httpx, "post", ok)
    out = tmp_path / "x.wav"
    assert mac_tts.generate_speech("Hi.", out, "silas", model="breeze", instruct="Warm.")
    assert out.read_bytes() == WAV and seen[0]["instruct"] == "Warm." and seen[0]["model"] == "breeze"

    tries = []

    def refused(url, json, timeout):
        tries.append(1)
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(mac_tts.httpx, "post", refused)
    assert mac_tts.generate_speech("Hi.", tmp_path / "y.wav", "silas") is False
    assert len(tries) == 1


def test_client_retries_server_errors_but_not_bad_requests(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_TTS_SERVICE_URL", "http://crouch-tts.invalid:9003")
    monkeypatch.setenv("MAC_TTS_MAX_RETRIES", "3")
    monkeypatch.setattr(mac_tts.time, "sleep", lambda *_: None)
    codes = iter([500, 500, 200])
    monkeypatch.setattr(mac_tts.httpx, "post", lambda url, json, timeout: httpx.Response(c := next(codes), content=WAV if c == 200 else b"err"))
    assert mac_tts.generate_speech("Hi.", tmp_path / "a.wav", "silas")
    calls = []
    monkeypatch.setattr(mac_tts.httpx, "post", lambda url, json, timeout: calls.append(1) or httpx.Response(404, content=b"unknown voice"))
    assert mac_tts.generate_speech("Hi.", tmp_path / "b.wav", "nobody") is False
    assert len(calls) == 1


def test_host_save_carries_breeze_voice_and_keeps_measured_paces():
    from admin import app as admin_app
    update = admin_app.HostUpdate(name="root", voice_breeze="hank")
    data = admin_app._host_update_to_yaml(update)
    assert data["voice_breeze"] == "hank" and "speaking_pace_wpm_breeze" not in data
    merged = {**{"speaking_pace_wpm_breeze": 141, "tts_backend": "breeze"}, **data}
    assert merged["speaking_pace_wpm_breeze"] == 141 and merged["tts_backend"] == "breeze"


def test_a_missing_breeze_sample_is_made_on_first_play(monkeypatch, tmp_path):
    from admin import app as admin_app
    made = []
    target = tmp_path / "silas.wav"

    def fake_ensure(backend, voice):
        made.append((backend, voice)); target.write_bytes(WAV); return target

    monkeypatch.setattr(admin_app, "sample_path", lambda b, v: tmp_path / "missing.wav")
    monkeypatch.setattr(admin_app, "ensure_voice_sample", fake_ensure)
    resp = admin_app.stream_voice_sample("breeze", "silas")
    assert made == [("breeze", "silas")] and resp.path == str(target)


def test_named_speaker_labels_map_to_host_roles():
    labels = {"primary_host_name": "root", "secondary_host_name": "Sudo"}
    script = "ROOT: [sigh] DNS again.\n\nSUDO: It's always DNS.\nROOT (dryly): Always."
    out = tg._canonical_dialogue_script(script, labels)
    assert out.splitlines()[0].startswith("HOST_A:") and out.splitlines()[2].startswith("HOST_B:")
    assert out.splitlines()[3].startswith("HOST_A (dryly):")
    parts = tg._parse_dialogue_parts(out)
    assert [p[0] for p in parts] == ["HOST_A", "HOST_B", "HOST_A"]


def test_dialogue_with_named_labels_uses_both_voices(monkeypatch, tmp_path):
    used = []
    monkeypatch.setattr(tg, "_local_render_speech", lambda backend, text, path, voice, speed=1.0: used.append(voice) or Path(path).write_bytes(WAV) or True)
    monkeypatch.setattr(tg.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0, "stderr": ""})())
    script = tg._canonical_dialogue_script("ROOT: One.\nSUDO: Two.", {"primary_host_name": "root", "secondary_host_name": "sudo"})
    tg.render_multi_voice(script, tmp_path / "x.wav", {"host": "hank", "guest": "jasper"}, backend="breeze")
    assert used == ["hank", "jasper"]


def test_labels_already_in_role_form_are_untouched():
    script = "HOST_A: Hi.\nHOST_B: Hello."
    assert tg._canonical_dialogue_script(script, {"primary_host_name": "root", "secondary_host_name": "sudo"}) == script
