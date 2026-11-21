"""A chunk with nothing to say is skipped, not a failed segment.

On air 2026-11-18 and 19, alien_theory scripts ended with a closing stage cue
that writ-fm kept as its own last chunk. crouch-tts strips cues before Phonon
speaks, found nothing left, and answered 400 "no speakable text"; writ-fm
treated that as a render failure, fell back to Kokoro (whose host had been
retired, so DNS failed) and threw the whole segment away.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from station import mac_tts
from station.content_generator import talk_generator as tg

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 28


def test_client_reports_nothing_to_say_apart_from_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("MAC_TTS_SERVICE_URL", "http://crouch-tts.invalid:9003")
    calls = []

    def empty(url, json, timeout):
        calls.append(1)
        return httpx.Response(400, json={"detail": "no speakable text"})

    monkeypatch.setattr(mac_tts.httpx, "post", empty)
    assert mac_tts.generate_speech("(The hum fades.)", tmp_path / "a.wav", "Toby", model="phonon") is None
    assert len(calls) == 1  # not retried

    monkeypatch.setattr(mac_tts.httpx, "post", lambda url, json, timeout: httpx.Response(400, json={"detail": "text too long"}))
    assert mac_tts.generate_speech("x" * 5000, tmp_path / "b.wav", "Toby", model="phonon") is False


class _Ffmpeg:
    """Stand-in for subprocess.run that remembers what each concat would join."""

    def __init__(self):
        self.joined: list[list[str]] = []

    def __call__(self, cmd, *a, **k):
        if "concat" in cmd:
            listing = Path(cmd[cmd.index("-i") + 1]).read_text(encoding="utf-8")
            self.joined.append([Path(line.split("'")[1]).name for line in listing.splitlines()])
        return type("R", (), {"returncode": 0, "stderr": ""})()


def _fake_render(results):
    """_local_render_speech that answers from `results` in order, writing a WAV on True."""
    answers = iter(results)

    def _render(backend, text, path, voice, speed=1.0):
        answer = next(answers)
        if answer:
            Path(path).write_bytes(WAV)
        return answer

    return _render


def test_single_voice_skips_a_trailing_unspeakable_chunk(monkeypatch, tmp_path):
    ffmpeg = _Ffmpeg()
    monkeypatch.setattr(tg.subprocess, "run", ffmpeg)
    monkeypatch.setattr(tg, "_split_tts_text", lambda script, max_chars=900: ["One.", "Two.", "Outro."])
    monkeypatch.setattr(tg, "_local_render_speech", _fake_render([True, True, None]))

    assert tg.render_single_voice_chunked("ignored", tmp_path / "x.wav", "Toby", backend="phonon")
    # Two spoken chunks with one gap between them, and no gap left dangling at the end.
    assert ffmpeg.joined == [["segment_000.wav", "gap.wav", "segment_001.wav"]]


def test_single_voice_skips_a_leading_unspeakable_chunk_without_a_leading_gap(monkeypatch, tmp_path):
    ffmpeg = _Ffmpeg()
    monkeypatch.setattr(tg.subprocess, "run", ffmpeg)
    monkeypatch.setattr(tg, "_split_tts_text", lambda script, max_chars=900: ["Static.", "One.", "Two."])
    monkeypatch.setattr(tg, "_local_render_speech", _fake_render([None, True, True]))

    assert tg.render_single_voice_chunked("ignored", tmp_path / "x.wav", "Toby", backend="phonon")
    assert ffmpeg.joined == [["segment_001.wav", "gap.wav", "segment_002.wav"]]


def test_a_real_failure_still_fails_the_segment(monkeypatch, tmp_path):
    monkeypatch.setattr(tg.subprocess, "run", _Ffmpeg())
    monkeypatch.setattr(tg, "_split_tts_text", lambda script, max_chars=900: ["One.", "Two."])
    monkeypatch.setattr(tg, "_local_render_speech", _fake_render([True, False]))

    assert tg.render_single_voice_chunked("ignored", tmp_path / "x.wav", "Toby", backend="phonon") is False


def test_nothing_speakable_at_all_is_still_a_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(tg.subprocess, "run", _Ffmpeg())
    monkeypatch.setattr(tg, "_split_tts_text", lambda script, max_chars=900: ["Hum.", "Static."])
    monkeypatch.setattr(tg, "_local_render_speech", _fake_render([None, None]))

    assert tg.render_single_voice_chunked("ignored", tmp_path / "x.wav", "Toby", backend="phonon") is False


def test_dialogue_skips_an_unspeakable_line(monkeypatch, tmp_path):
    ffmpeg = _Ffmpeg()
    monkeypatch.setattr(tg.subprocess, "run", ffmpeg)
    monkeypatch.setattr(tg, "_local_render_speech", _fake_render([True, True, None]))
    script = tg._canonical_dialogue_script(
        "ROOT: One.\nSUDO: Two.\nROOT: Clatter.",
        {"primary_host_name": "root", "secondary_host_name": "sudo"},
    )

    assert tg.render_multi_voice(script, tmp_path / "x.wav", {"host": "hank", "guest": "Alfie"}, backend="phonon")
    assert ffmpeg.joined == [["segment_000.wav", "gap.wav", "segment_001.wav"]]
