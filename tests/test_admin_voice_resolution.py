from __future__ import annotations

import app as admin_app
from shared import settings


def test_show_primary_host_meta_uses_roster_google_voice(monkeypatch) -> None:
    monkeypatch.setattr(
        admin_app,
        "get_all_hosts",
        lambda: {
            "charon_host": {
                "name": "Charon",
                "voice_google": "Charon",
                "tts_voice": "am_michael",
            }
        },
    )
    monkeypatch.setattr(
        admin_app,
        "load_schedule",
        lambda: {
            "shows": {
                "alien_theory": {
                    "name": "Alien Theory",
                    "host": "charon_host",
                    "tts_backend": "google",
                    "hosts": [
                        {
                            "id": "charon_host",
                            "role": "primary",
                            "tts_backend": "google",
                        }
                    ],
                }
            }
        },
    )

    meta = admin_app._show_primary_host_meta("alien_theory")

    assert meta == {
        "host_id": "charon_host",
        "host_name": "Charon",
        "backend": "google",
        "voice": "Charon",
    }


def test_normalize_show_converts_legacy_fields_to_hosts_and_drops_legacy(monkeypatch) -> None:
    monkeypatch.setattr(
        admin_app,
        "get_all_hosts",
        lambda: {
            "charon_host": {
                "name": "Charon",
                "tts_voice": "am_michael",
                "voice_minimax": "Deep_Voice_Man",
                "voice_google": "Charon",
            }
        },
    )

    normalized = admin_app._normalize_show(
        "alien_theory",
        {
            "name": "Alien Theory",
            "description": "Signals from the void",
            "host": "charon_host",
            "tts_backend": "google",
            "voices": {"host": "Charon", "guest": "Puck"},
        },
    )

    assert "host" not in normalized
    assert "voices" not in normalized
    assert normalized["tts_backend"] == "google"
    assert normalized["hosts"][0]["id"] == "charon_host"
    assert normalized["hosts"][0]["tts_backend"] == "google"
    assert normalized["hosts"][0]["voice_google"] == "Charon"
    assert normalized["hosts"][1]["voice_google"] == "Puck"


def test_update_show_persists_canonical_hosts_only(monkeypatch) -> None:
    saved = {}

    monkeypatch.setattr(admin_app, "load_schedule", lambda: {"shows": {"alien_theory": {"name": "Old", "description": "Old"}}})
    monkeypatch.setattr(admin_app, "save_schedule", lambda data: saved.update(data))
    monkeypatch.setattr(admin_app, "get_all_hosts", lambda: {"charon_host": {"name": "Charon"}, "guest_host": {"name": "Guest"}})
    monkeypatch.setattr(admin_app, "get_segment_types_api", lambda: {"show_intro": {}, "deep_dive": {}})

    payload = admin_app.ShowUpdate(
        name="Alien Theory",
        description="Signals from the void",
        hosts=[
            {"id": "charon_host", "role": "primary", "tts_backend": "google", "voice_google": "Charon"},
            {"id": "guest_host", "role": "guest", "tts_backend": "google", "voice_google": "Puck"},
        ],
        tts_backend="google",
        segment_types=["show_intro"],
        bumper_style="ambient",
        playback_sequence={},
        content_lifecycle={},
        generation={},
    )

    result = admin_app.update_show("alien_theory", payload)

    assert result == {"ok": True, "show_id": "alien_theory"}
    persisted = saved["shows"]["alien_theory"]
    assert persisted["hosts"][0]["voice_google"] == "Charon"
    assert "host" not in persisted
    assert persisted["tts_backend"] == "google"
    assert "voices" not in persisted


def test_create_show_persists_canonical_hosts_only(monkeypatch) -> None:
    saved = {}

    monkeypatch.setattr(admin_app, "load_schedule", lambda: {"shows": {}})
    monkeypatch.setattr(admin_app, "save_schedule", lambda data: saved.update(data))
    monkeypatch.setattr(admin_app, "get_all_hosts", lambda: {"charon_host": {"name": "Charon"}})
    monkeypatch.setattr(admin_app, "get_segment_types_api", lambda: {"show_intro": {}, "deep_dive": {}})

    payload = admin_app.ShowUpdate(
        name="Alien Theory",
        description="Signals from the void",
        hosts=[
            {"id": "charon_host", "role": "primary", "tts_backend": "google", "voice_google": "Charon"},
        ],
        tts_backend="google",
        segment_types=["show_intro"],
        bumper_style="ambient",
        playback_sequence={},
        content_lifecycle={},
        generation={},
    )

    result = admin_app.create_show("alien_theory", payload)

    assert result == {"ok": True, "show_id": "alien_theory"}
    persisted = saved["shows"]["alien_theory"]
    assert persisted["hosts"][0]["voice_google"] == "Charon"
    assert "host" not in persisted
    assert persisted["tts_backend"] == "google"
    assert "voices" not in persisted


def test_run_generation_job_uses_shared_backend_defaults(monkeypatch) -> None:
    monkeypatch.setattr(admin_app, "ollama_url", lambda: "http://test-ollama:11434")
    monkeypatch.setattr(admin_app, "ollama_model", lambda: "test-model")
    monkeypatch.setattr(admin_app, "minimax_music_model", lambda: "music-test")
    monkeypatch.setattr(admin_app, "google_tts_model", lambda: "google-test")
    monkeypatch.setattr(
        admin_app,
        "load_schedule",
        lambda: {
            "shows": {
                "alien_theory": {
                    "name": "Alien Theory",
                    "hosts": [{"id": "charon_host", "role": "primary", "tts_backend": "google", "voice_google": "Charon"}],
                }
            }
        },
    )
    monkeypatch.setattr(admin_app, "_log_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(admin_app, "_invalidate_inventory_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(admin_app, "_save_job", lambda *args, **kwargs: None)

    captured = {}

    class FakeProc:
        def __init__(self, env):
            self.stdout = iter([])
            self.returncode = 0
            captured["env"] = env

        def wait(self):
            return 0

    def fake_popen(cmd, stdout, stderr, text, env, cwd):
        captured["env"] = env
        return FakeProc(env)

    monkeypatch.setattr(admin_app.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(admin_app.Path, "exists", lambda self: True)

    admin_app._jobs["job-1"] = {"id": "job-1", "status": "queued", "log": []}
    req = admin_app.GenerateRequest(show_id="alien_theory", content_type="talk", count=1)
    admin_app._run_generation_job("job-1", req)

    assert captured["env"]["OLLAMA_URL"] == "http://test-ollama:11434"
    assert captured["env"]["OLLAMA_MODEL"] == "test-model"
    assert captured["env"]["MINIMAX_MUSIC_MODEL"] == "music-test"
    assert captured["env"]["GOOGLE_TTS_MODEL"] == "google-test"
    assert captured["env"]["WRIT_TTS_BACKEND"] == "google"


def test_run_generation_job_prefers_show_backend_over_primary_host_backend(monkeypatch) -> None:
    monkeypatch.setattr(
        admin_app,
        "load_schedule",
        lambda: {
            "shows": {
                "alien_theory": {
                    "name": "Alien Theory",
                    "tts_backend": "google",
                    "hosts": [{"id": "charon_host", "role": "primary", "tts_backend": "kokoro", "voice_google": "Charon"}],
                }
            }
        },
    )
    monkeypatch.setattr(admin_app, "_log_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(admin_app, "_invalidate_inventory_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(admin_app, "_save_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(admin_app.Path, "exists", lambda self: True)

    captured = {}

    class FakeProc:
        def __init__(self, env):
            self.stdout = iter([])
            self.returncode = 0
            captured["env"] = env

        def wait(self):
            return 0

    def fake_popen(cmd, stdout, stderr, text, env, cwd):
        captured["env"] = env
        return FakeProc(env)

    monkeypatch.setattr(admin_app.subprocess, "Popen", fake_popen)

    admin_app._jobs["job-show-default"] = {"id": "job-show-default", "status": "queued", "log": []}
    req = admin_app.GenerateRequest(show_id="alien_theory", content_type="talk", count=1)
    admin_app._run_generation_job("job-show-default", req)

    assert captured["env"]["WRIT_TTS_BACKEND"] == "google"


def test_shared_settings_default_voice_and_icecast_url(monkeypatch) -> None:
    monkeypatch.delenv("ICECAST_STATUS_URL", raising=False)
    monkeypatch.setenv("ICECAST_HOST", "radio.local")
    monkeypatch.setenv("ICECAST_PORT", "8010")

    assert settings.default_voice_for_backend("google", "host") == "Kore"
    assert settings.default_voice_for_backend("google", "guest") == "Puck"
    assert settings.icecast_status_url() == "http://radio.local:8010/status-json.xsl"
