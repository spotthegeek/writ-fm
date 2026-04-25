from __future__ import annotations

from types import SimpleNamespace

from shared.hosts import (
    assignment_voice,
    assignment_wpm,
    host_label,
    primary_host_assignment,
    secondary_host_assignment,
)


def test_primary_host_assignment_uses_legacy_show_fields_with_backend_voice() -> None:
    show = SimpleNamespace(
        host="charon_host",
        tts_backend="google",
        voices={"host": "Charon"},
        hosts=[],
    )

    primary = primary_host_assignment(show)

    assert primary["id"] == "charon_host"
    assert primary["tts_backend"] == "google"
    assert primary["voice_google"] == "Charon"


def test_primary_host_assignment_prefers_show_backend_over_host_backend() -> None:
    show = SimpleNamespace(
        tts_backend="google",
        hosts=[{"id": "charon_host", "role": "primary", "tts_backend": "kokoro"}],
    )

    primary = primary_host_assignment(show)

    assert primary["tts_backend"] == "google"


def test_secondary_host_assignment_prefers_dialogue_roles() -> None:
    show = SimpleNamespace(
        hosts=[
            {"id": "host_a", "role": "primary"},
            {"id": "host_b", "role": "secondary"},
        ]
    )

    secondary = secondary_host_assignment(show)

    assert secondary == {"id": "host_b", "role": "secondary"}


def test_secondary_host_assignment_ignores_primary_when_primary_is_copied() -> None:
    show = SimpleNamespace(
        tts_backend="kokoro",
        hosts=[{"id": "host_a", "role": "primary", "voice_kokoro": "am_liam"}],
    )

    primary = primary_host_assignment(show)
    secondary = secondary_host_assignment(show, primary)

    assert primary["id"] == "host_a"
    assert secondary is None


def test_assignment_voice_and_wpm_use_roster_fallbacks() -> None:
    roster = {
        "charon_host": {
            "name": "Charon",
            "voice_google": "Charon",
            "speaking_pace_wpm_google": 152,
        }
    }

    lookup = lambda host_id: roster.get(host_id)

    assert assignment_voice({"id": "charon_host"}, "google", role="host", roster_lookup=lookup) == "Charon"
    assert assignment_wpm({"id": "charon_host"}, "google", roster_lookup=lookup) == 152


def test_host_label_uses_roster_name_when_available() -> None:
    lookup = lambda host_id: {"name": "The Signal Keeper"} if host_id == "signal_keeper" else None
    assert host_label("signal_keeper", roster_lookup=lookup) == "The Signal Keeper"
