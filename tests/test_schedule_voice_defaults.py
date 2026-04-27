from __future__ import annotations

from pathlib import Path

import yaml

from station.schedule import load_schedule


def test_load_schedule_normalizes_legacy_show_voice_defaults(tmp_path: Path) -> None:
    schedule_path = tmp_path / "schedule.yaml"
    schedule_path.write_text(
        yaml.safe_dump(
            {
                "station_name": "WRIT-FM",
                "timezone": "UTC",
                "shows": {
                    "alien_theory": {
                        "name": "Alien Theory",
                        "description": "Signals from the void",
                        "host": "charon_host",
                        "tts_backend": "google",
                        "voices": {"host": "Charon", "guest": "Puck"},
                    }
                },
                "schedule": {
                    "base": [
                        {"start": "00:00", "end": "23:59", "show": "alien_theory"},
                        {"start": "23:59", "end": "00:00", "show": "alien_theory"},
                    ],
                    "overrides": [],
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_schedule(schedule_path)
    show = loaded.shows["alien_theory"]

    assert show.host == "charon_host"
    assert show.tts_backend == "google"
    assert show.hosts[0]["id"] == "charon_host"
    assert show.hosts[0]["role"] == "primary"
    assert show.hosts[0]["voice_google"] == "Charon"
    assert show.hosts[0]["voice_minimax"] == "Deep_Voice_Man"
    assert show.hosts[0]["voice_kokoro"] == "am_michael"
    assert show.hosts[1]["role"] == "guest"
    assert show.hosts[1]["voice_google"] == "Puck"
    assert show.playback_sequence == {}
