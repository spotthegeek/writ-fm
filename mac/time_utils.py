#!/usr/bin/env python3
"""
Station-aware time helpers.

Reads timezone from config/schedule.yaml and returns station-local datetimes.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEDULE_PATH = PROJECT_ROOT / "config" / "schedule.yaml"


def _load_timezone_name() -> str | None:
    try:
        with open(SCHEDULE_PATH) as f:
            data = yaml.safe_load(f) or {}
        tz_name = str(data.get("timezone", "local")).strip() or "local"
        return tz_name
    except Exception:
        return None


def station_tz():
    tz_name = _load_timezone_name()
    if not tz_name or tz_name in {"local", "system"}:
        return None
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return None


def station_now() -> datetime:
    tz = station_tz()
    return datetime.now(tz) if tz else datetime.now()


def station_from_timestamp(ts: float) -> datetime:
    tz = station_tz()
    return datetime.fromtimestamp(ts, tz) if tz else datetime.fromtimestamp(ts)


def station_iso_now() -> str:
    return station_now().isoformat()
