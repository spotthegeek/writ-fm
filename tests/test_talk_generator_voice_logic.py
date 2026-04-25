from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

from mac.content_generator import talk_generator
from mac.schedule import Show


def test_voice_for_assignment_uses_roster_google_default(monkeypatch) -> None:
    monkeypatch.setattr(
        talk_generator,
        "get_host",
        lambda host_id: {"voice_google": "Charon", "tts_voice": "am_michael"},
    )

    voice = talk_generator._voice_for_assignment(
        {"id": "charon_host", "role": "primary"},
        "google",
        "Kore",
    )

    assert voice == "Charon"


def test_pace_wpm_for_assignment_uses_backend_specific_host_setting(monkeypatch) -> None:
    monkeypatch.setattr(
        talk_generator,
        "get_host",
        lambda host_id: {
            "speaking_pace_wpm": 130,
            "speaking_pace_wpm_google": 156,
        },
    )

    pace = talk_generator._pace_wpm_for_assignment(
        {"id": "charon_host", "role": "primary"},
        backend="google",
    )

    assert pace == 156


def test_voice_plan_prefers_roster_google_default_for_primary_host(monkeypatch) -> None:
    monkeypatch.setattr(
        talk_generator,
        "get_host",
        lambda host_id: {
            "name": "Charon",
            "voice_google": "Charon",
            "speaking_pace_wpm_google": 150,
        },
    )

    show = Show(
        show_id="alien_theory",
        name="Alien Theory",
        description="Signals from the void",
        host="charon_host",
        hosts=[{"id": "charon_host", "role": "primary", "tts_backend": "google"}],
        tts_backend="google",
    )

    labels, voices = talk_generator._voice_plan(show, "show_intro", "google")

    assert labels["primary_host_name"] == "Charon"
    assert voices["host"] == "Charon"
    assert voices["host_wpm"] == 150


def test_render_single_voice_google_passes_wpm_to_provider(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    fake_google = types.SimpleNamespace()

    def fake_generate_speech(text, output_path, voice_id, wpm=None):
        calls.append(
            {
                "text": text,
                "output_path": output_path,
                "voice_id": voice_id,
                "wpm": wpm,
            }
        )
        Path(output_path).write_bytes(b"RIFF")
        return True

    fake_google.generate_speech = fake_generate_speech
    sys.modules["google_tts"] = fake_google
    try:
        out = tmp_path / "sample.wav"
        ok = talk_generator.render_single_voice(
            "Testing the signal.",
            out,
            "Charon",
            backend="google",
            wpm=147,
        )
    finally:
        sys.modules.pop("google_tts", None)

    assert ok is True
    assert calls == [
        {
            "text": "Testing the signal.",
            "output_path": out,
            "voice_id": "Charon",
            "wpm": 147,
        }
    ]


def test_render_single_voice_google_chunks_long_scripts(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    fake_google = types.SimpleNamespace()

    def fake_generate_speech(text, output_path, voice_id, wpm=None):
        calls.append(
            {
                "text": text,
                "output_path": output_path,
                "voice_id": voice_id,
                "wpm": wpm,
            }
        )
        Path(output_path).write_bytes(b"RIFF")
        return True

    fake_google.generate_speech = fake_generate_speech
    sys.modules["google_tts"] = fake_google

    def fake_run(cmd, capture_output=True, text=True):
        out_path = Path(cmd[-1])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"RIFF")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(talk_generator.subprocess, "run", fake_run)
    try:
        out = tmp_path / "sample.wav"
        ok = talk_generator.render_single_voice(
            ("This is a long story. " * 200).strip(),
            out,
            "Charon",
            backend="google",
            wpm=147,
        )
    finally:
        sys.modules.pop("google_tts", None)

    assert ok is True
    assert len(calls) > 1
    assert out.exists()


def test_render_single_voice_minimax_chunks_long_scripts(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    fake_minimax = types.SimpleNamespace()

    def fake_generate_speech(text, output_path, voice_id, speed=1.0):
        calls.append(
            {
                "text": text,
                "output_path": output_path,
                "voice_id": voice_id,
                "speed": speed,
            }
        )
        Path(output_path).write_bytes(b"ID3")
        return True

    fake_minimax.generate_speech = fake_generate_speech
    sys.modules["minimax_tts"] = fake_minimax

    def fake_run(cmd, capture_output=True, text=True):
        out_path = Path(cmd[-1])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"ID3")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(talk_generator.subprocess, "run", fake_run)
    try:
        out = tmp_path / "sample.mp3"
        ok = talk_generator.render_single_voice(
            ("This is a long story. " * 200).strip(),
            out,
            "Deep_Voice_Man",
            backend="minimax",
            speed=1.0,
        )
    finally:
        sys.modules.pop("minimax_tts", None)

    assert ok is True
    assert len(calls) > 1
    assert out.exists()


def test_generate_segment_youtube_requires_youtube_source(monkeypatch) -> None:
    show = Show(
        show_id="youtube_ai",
        name="YouTube AI",
        description="Videos only",
        host="charon_host",
        hosts=[{"id": "charon_host", "role": "primary", "tts_backend": "google"}],
        tts_backend="google",
    )

    monkeypatch.setattr(talk_generator, "load_source_context", lambda *args, **kwargs: None)

    result = talk_generator.generate_segment(show, "youtube", topic="")

    assert result is None


def test_generate_segment_youtube_ingests_direct_audio_even_for_single_host_show(monkeypatch, tmp_path: Path) -> None:
    show = Show(
        show_id="youtube_ai",
        name="YouTube AI",
        description="Videos only",
        host="charon_host",
        hosts=[{"id": "charon_host", "role": "primary", "tts_backend": "google"}],
        tts_backend="google",
    )
    source_audio = tmp_path / "cached.mp3"
    source_audio.write_bytes(b"ID3")
    source_context = talk_generator.SourceContext(
        source_type="youtube",
        source_value="https://www.youtube.com/watch?v=abc123def45",
        title="A useful video",
        topic="A useful video",
        channel="Build In Public",
        audio_path=str(source_audio),
        transcript="Transcript text",
    )

    monkeypatch.setattr(talk_generator, "OUTPUT_DIR", tmp_path / "segments")
    monkeypatch.setattr(talk_generator, "SCRIPTS_DIR", tmp_path / "scripts")
    monkeypatch.setattr(talk_generator, "get_duration", lambda path: 42.0)
    monkeypatch.setattr(
        talk_generator,
        "station_now",
        lambda: SimpleNamespace(strftime=lambda fmt: "20260425_121700"),
    )
    monkeypatch.setattr(talk_generator, "station_iso_now", lambda: "2026-04-25T12:17:00+00:00")

    result = talk_generator.generate_segment(
        show,
        "youtube",
        topic="",
        source_context=source_context,
    )

    assert result is not None
    assert result.exists()
    assert result.suffix == ".mp3"
    meta = (tmp_path / "scripts" / "talk_youtube_20260425_121700.json").read_text()
    assert '"tts_backend": "youtube_ingest"' in meta


def test_generate_for_show_uses_show_source_rules_when_youtube_source_value_is_blank(monkeypatch) -> None:
    show = Show(
        show_id="youtube_ai",
        name="YouTube AI",
        description="Videos only",
        host="charon_host",
        hosts=[{"id": "charon_host", "role": "primary", "tts_backend": "google"}],
        tts_backend="google",
        segment_types=["youtube"],
        source_rules=[
            {
                "type": "youtube_channel",
                "value": "https://www.youtube.com/@buildnpublic",
                "lookback_days": 1,
                "selection_strategy": "latest",
                "segment_type": "",
            }
        ],
    )
    schedule = SimpleNamespace(shows={"youtube_ai": show}, station_name="WRIT-FM")
    chosen_context = talk_generator.SourceContext(
        source_type="youtube",
        source_value="https://www.youtube.com/watch?v=abc123def45",
        title="A useful video",
        topic="A useful video",
        audio_path="/tmp/cached.mp3",
    )

    monkeypatch.setattr(talk_generator, "_used_source_keys_for_show", lambda show_id: set())
    monkeypatch.setattr(talk_generator, "_choose_source_rule_for_show", lambda *args, **kwargs: show.source_rules[0])
    monkeypatch.setattr(talk_generator, "load_source_context", lambda *args, **kwargs: chosen_context)
    calls = []

    def fake_generate_segment(*args, **kwargs):
        calls.append(kwargs)
        return Path("/tmp/generated.mp3")

    monkeypatch.setattr(talk_generator, "generate_segment", fake_generate_segment)

    generated = talk_generator.generate_for_show(
        "youtube_ai",
        schedule,
        count=1,
        segment_type="youtube",
        source_type="youtube",
        source_value="",
    )

    assert generated == 1
    assert calls[0]["source_type"] == "youtube_channel"
    assert calls[0]["source_value"] == "https://www.youtube.com/@buildnpublic"
    assert calls[0]["source_context"] is chosen_context


def test_generate_segment_reddit_storytelling_requires_reddit_source(monkeypatch) -> None:
    show = Show(
        show_id="story_hour",
        name="Story Hour",
        description="Stories only",
        host="charon_host",
        hosts=[{"id": "charon_host", "role": "primary", "tts_backend": "google"}],
        tts_backend="google",
    )

    monkeypatch.setattr(talk_generator, "load_source_context", lambda *args, **kwargs: None)

    result = talk_generator.generate_segment(show, "reddit_storytelling", topic="")

    assert result is None
