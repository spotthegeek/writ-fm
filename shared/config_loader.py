"""
Loads the split config files (station.yaml, shows.yaml, sources.yaml) and
merges them into the dict structure that the rest of the codebase expects,
matching the old monolithic schedule.yaml format.

Save functions split a merged dict back to the three files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


# ── Loaders ──────────────────────────────────────────────────────────────────

def load_station(config_dir: Path = CONFIG_DIR) -> dict:
    return _read_yaml(config_dir / "station.yaml")


def load_shows(config_dir: Path = CONFIG_DIR) -> dict[str, Any]:
    return _read_yaml(config_dir / "shows.yaml").get("shows") or {}


def load_sources(config_dir: Path = CONFIG_DIR) -> dict[str, list]:
    return _read_yaml(config_dir / "sources.yaml").get("sources") or {}


def load_station_config(config_dir: Path = CONFIG_DIR) -> dict:
    """Return a merged dict in the legacy schedule.yaml format."""
    station = load_station(config_dir)
    shows = {k: dict(v) for k, v in load_shows(config_dir).items()}
    sources = load_sources(config_dir)
    for show_id, srcs in sources.items():
        if show_id in shows:
            shows[show_id]["research_sources"] = srcs
            shows[show_id]["source_rules"] = srcs
    return {
        "station_name": station.get("station_name", ""),
        "timezone": station.get("timezone", ""),
        "shows": shows,
        "schedule": station.get("schedule", {"base": [], "overrides": []}),
    }


# ── Savers ───────────────────────────────────────────────────────────────────

def _dump(path: Path, data: dict) -> None:
    with open(path, "w") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def save_station(config_dir: Path, data: dict) -> None:
    _dump(config_dir / "station.yaml", {
        "station_name": data.get("station_name", ""),
        "timezone": data.get("timezone", ""),
        "schedule": data.get("schedule", {"base": [], "overrides": []}),
    })


def save_shows(config_dir: Path, data: dict) -> None:
    shows = {}
    for show_id, show in (data.get("shows") or {}).items():
        shows[show_id] = {k: v for k, v in show.items()
                          if k not in ("research_sources", "source_rules")}
    _dump(config_dir / "shows.yaml", {"shows": shows})


def save_sources(config_dir: Path, data: dict) -> None:
    sources = {}
    for show_id, show in (data.get("shows") or {}).items():
        srcs = show.get("research_sources") or show.get("source_rules") or []
        if srcs:
            sources[show_id] = [dict(s) for s in srcs if isinstance(s, dict)]
    _dump(config_dir / "sources.yaml", {"sources": sources})


def save_station_config(config_dir: Path, data: dict) -> None:
    """Write a merged config dict back to station.yaml, shows.yaml, sources.yaml."""
    save_station(config_dir, data)
    save_shows(config_dir, data)
    save_sources(config_dir, data)
