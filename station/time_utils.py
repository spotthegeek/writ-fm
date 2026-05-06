#!/usr/bin/env python3
"""
Station-aware time helpers.

Reads timezone from config/schedule.yaml and returns station-local datetimes.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import time as _time

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"

_TZ_CACHE: tuple[float, ZoneInfo | None] | None = None
_TZ_CACHE_TTL = 60.0


def _load_timezone_name() -> str | None:
    try:
        from shared.config_loader import load_station
        data = load_station(CONFIG_DIR)
        tz_name = str(data.get("timezone", "local")).strip() or "local"
        return tz_name
    except Exception:
        return None


def station_tz():
    global _TZ_CACHE
    now = _time.monotonic()
    if _TZ_CACHE is not None and now - _TZ_CACHE[0] < _TZ_CACHE_TTL:
        return _TZ_CACHE[1]
    tz_name = _load_timezone_name()
    if not tz_name or tz_name in {"local", "system"}:
        result = None
    else:
        try:
            result = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            result = None
    _TZ_CACHE = (now, result)
    return result


def station_now() -> datetime:
    tz = station_tz()
    return datetime.now(tz) if tz else datetime.now()


def station_from_timestamp(ts: float) -> datetime:
    tz = station_tz()
    return datetime.fromtimestamp(ts, tz) if tz else datetime.fromtimestamp(ts)


def station_iso_now() -> str:
    return station_now().isoformat()
