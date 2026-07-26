"""Regression tests for the station's on-air name.

Of 550 scripts written in the 30 days to 2026-07-26, 62 said "WRIT-FM" and 132
said "Crouch" — the station contradicted itself several times an hour. config
was correct the whole time and correctly plumbed; what defeated it was host
prose that spelled the old name out and was injected into the prompt verbatim.

Nothing in the suite covered prompt *content*, which is why this ran for months.
These tests cover it: no host prose may contain a literal station name, and no
placeholder may survive into a built prompt.

Note config/hosts.yaml overrides persona.HOSTS field-by-field at import, so the
YAML is the text that actually ships. It is checked here too.
"""
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from station.content_generator import persona  # noqa: E402
from station.content_generator.persona import (  # noqa: E402
    HOSTS,
    STATION_PLACEHOLDER,
    build_host_prompt,
    resolve_station_name,
)

# Any literal that means "the station" and must never be hardcoded in prose.
FORBIDDEN = ("WRIT-FM", "WRIT FM", "Crouch-FM", "Crouch FM")

PROSE_FIELDS = ("identity", "voice_style", "philosophy", "anti_patterns")


def _offending(text: str) -> list[str]:
    return [lit for lit in FORBIDDEN if lit in (text or "")]


@pytest.mark.parametrize("host_id", sorted(HOSTS))
@pytest.mark.parametrize("field", PROSE_FIELDS)
def test_host_prose_never_hardcodes_the_station_name(host_id, field):
    found = _offending(HOSTS[host_id].get(field, ""))
    assert not found, (
        f"{host_id}.{field} hardcodes {found}. Write {STATION_PLACEHOLDER} instead — "
        "a literal here overrides config and goes to air."
    )


def test_hosts_yaml_never_hardcodes_the_station_name():
    """The YAML wins over persona.py, so fixing only persona.py fixes nothing."""
    path = ROOT / "config" / "hosts.yaml"
    if not path.exists():
        pytest.skip("config/hosts.yaml not present")
    data = yaml.safe_load(path.read_text()) or {}
    problems = {}
    for host_id, host in (data.get("hosts") or {}).items():
        for field in PROSE_FIELDS:
            found = _offending(str(host.get(field) or ""))
            if found:
                problems[f"{host_id}.{field}"] = found
    assert not problems, f"config/hosts.yaml hardcodes station names: {problems}"


def test_time_period_moods_never_hardcode_the_station_name():
    """Mood text is injected into every prompt via CURRENT STATE."""
    problems = {}
    for period, cfg in persona.TIME_PERIOD_MOODS.items():
        for key, value in cfg.items():
            if isinstance(value, str) and _offending(value):
                problems[f"{period}.{key}"] = _offending(value)
    assert not problems, f"mood text hardcodes station names: {problems}"


@pytest.mark.parametrize("host_id", sorted(HOSTS))
def test_built_prompt_uses_the_configured_name_only(host_id):
    prompt = build_host_prompt(
        host_id,
        {"show_name": "Test Show", "show_description": "d", "segment_type": "station_id",
         "station_name": "Test-FM"},
    )
    assert "Test-FM" in prompt
    assert STATION_PLACEHOLDER not in prompt, "placeholder leaked into the prompt"
    for lit in FORBIDDEN:
        assert lit not in prompt, f"{lit} reached the prompt for {host_id}"


def test_show_name_falls_back_to_the_station_not_a_literal():
    prompt = build_host_prompt("liminal_operator", {"station_name": "Test-FM"})
    assert "CURRENT SHOW: Test-FM" in prompt


def test_station_name_resolves_from_config_when_not_supplied():
    assert resolve_station_name() == resolve_station_name({})


def test_missing_station_name_is_an_error_not_a_default(monkeypatch):
    """The whole failure mode was a silent fallback. There must not be one."""
    import shared.config_loader as loader

    monkeypatch.setattr(loader, "load_station_config", lambda *a, **k: {"station_name": "  "})
    with pytest.raises(RuntimeError, match="station_name"):
        resolve_station_name()


def test_apply_station_name_tolerates_braces_in_prose():
    """Host prose contains braces of its own; format() would explode on them."""
    text = "Welcome to {station_name}. Acting notes look like {this} and [pause]."
    out = persona._apply_station_name(text, "Test-FM")
    assert out == "Welcome to Test-FM. Acting notes look like {this} and [pause]."
