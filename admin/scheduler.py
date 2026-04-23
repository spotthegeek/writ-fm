#!/usr/bin/env python3
"""
WRIT-FM Auto-Generation Scheduler

Runs as a background thread inside the admin server.
Periodically checks talk segment and music bumper inventory per show,
and triggers generation jobs when inventory falls below configured minimums.

Config lives in each show's `generation` block inside schedule.yaml:

  generation:
    talk:
      enabled: true
      min_inventory: 5        # trigger when below this
      target_inventory: 15    # generate up to this many
      cadence: continuous     # continuous | hourly | daily | weekly
    music:
      enabled: true
      min_inventory: 3
      target_inventory: 8
      cadence: weekly
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path
from typing import Callable

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEDULE_PATH = PROJECT_ROOT / "config" / "schedule.yaml"
TALK_DIR = PROJECT_ROOT / "output" / "talk_segments"
BUMPERS_DIR = PROJECT_ROOT / "output" / "music_bumpers"
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python3"
AUDIO_EXTS = {".wav", ".mp3", ".flac"}

CADENCE_SECONDS = {
    "continuous": 0,        # generate as soon as inventory drops
    "hourly":     3600,
    "daily":      86400,
    "weekly":     604800,
    "monthly":    2592000,
}

DEFAULT_TALK_CONFIG = {
    "enabled": False,
    "min_inventory": 5,
    "target_inventory": 15,
    "cadence": "continuous",
}

DEFAULT_MUSIC_CONFIG = {
    "enabled": False,
    "min_inventory": 3,
    "target_inventory": 8,
    "cadence": "weekly",
}


def _summarize_process_failure(stderr: str, stdout: str, limit: int = 400) -> str:
    """Return the most useful tail of a failed subprocess output."""
    combined = ((stderr or "").strip() or (stdout or "").strip()).strip()
    if not combined:
        return "No error output captured"

    lines = [line.strip() for line in combined.splitlines() if line.strip()]
    if not lines:
        return combined[:limit]

    # Failures are usually at the end; warnings often appear first.
    summary = " | ".join(lines[-6:])
    if len(summary) > limit:
        summary = summary[-limit:]
    return summary


def _default_generation_enabled(show_type: str, content_type: str) -> bool:
    """Infer whether a generator should be enabled when a show has no explicit config.

    We keep this intentionally conservative:
    - music_first defaults to music generation
    - everything else defaults to talk generation
    """
    show_type = (show_type or "research").strip() or "research"
    if content_type == "music":
        return show_type == "music_first"
    if content_type == "talk":
        return show_type != "music_first"
    return False


def _effective_generation_configs(show: dict) -> tuple[dict, dict]:
    """Merge explicit generation config with show-type defaults."""
    show_type = str(show.get("show_type") or "research").strip() or "research"
    gen_cfg = show.get("generation") or {}

    raw_talk = gen_cfg.get("talk") or {}
    raw_music = gen_cfg.get("music") or {}

    talk_cfg = {**DEFAULT_TALK_CONFIG, **raw_talk}
    if "enabled" not in raw_talk:
        talk_cfg["enabled"] = _default_generation_enabled(show_type, "talk")

    music_cfg = {**DEFAULT_MUSIC_CONFIG, **raw_music}
    if "enabled" not in raw_music:
        music_cfg["enabled"] = _default_generation_enabled(show_type, "music")

    return talk_cfg, music_cfg


class SchedulerState:
    """Shared state for the scheduler — readable by the API."""

    def __init__(self):
        self._lock = threading.Lock()
        self.running = False
        self.last_check: datetime | None = None
        self.last_run_per_show: dict[str, dict] = {}  # show_id → {talk: dt, music: dt}
        self.log: list[dict] = []  # recent activity log, newest first
        self.active_jobs: dict[str, dict] = {}  # job_id → info
        self.recent_jobs: list[dict] = []  # recent job history, newest first

    def add_log(self, show_id: str, content_type: str, msg: str, level: str = "info"):
        entry = {
            "ts": _station_now().strftime("%Y-%m-%d %H:%M:%S"),
            "show_id": show_id,
            "type": content_type,
            "msg": msg,
            "level": level,
        }
        with self._lock:
            self.log.insert(0, entry)
            self.log = self.log[:200]  # keep last 200 entries
        print(f"[scheduler] [{show_id}/{content_type}] {msg}")

    def get_log(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return list(self.log[:limit])

    def record_run(self, show_id: str, content_type: str):
        with self._lock:
            if show_id not in self.last_run_per_show:
                self.last_run_per_show[show_id] = {}
            self.last_run_per_show[show_id][content_type] = _station_now()

    def last_run(self, show_id: str, content_type: str) -> datetime | None:
        with self._lock:
            return self.last_run_per_show.get(show_id, {}).get(content_type)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "last_check": self.last_check.isoformat() if self.last_check else None,
                "last_run_per_show": {
                    sid: {ct: dt.isoformat() for ct, dt in runs.items()}
                    for sid, runs in self.last_run_per_show.items()
                },
                "active_jobs": dict(self.active_jobs),
                "recent_jobs": list(self.recent_jobs[:20]),
            }


# Singleton
state = SchedulerState()
_inventory_invalidator: Callable[[str | None], None] | None = None


def _count_inventory(directory: Path, show_id: str) -> int:
    d = directory / show_id
    if not d.exists():
        return 0
    return sum(1 for f in d.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTS)


def _cadence_ok(show_id: str, content_type: str, cadence: str) -> bool:
    """Return True if enough time has passed since the last run for this cadence."""
    min_gap = CADENCE_SECONDS.get(cadence, 0)
    if min_gap == 0:
        return True
    last = state.last_run(show_id, content_type)
    if last is None:
        return True
    return (_station_now() - last).total_seconds() >= min_gap


def _load_schedule() -> dict:
    with open(SCHEDULE_PATH) as f:
        return yaml.safe_load(f)


def _station_tz():
    try:
        data = _load_schedule()
        tz_name = str(data.get("timezone", "local")).strip() or "local"
        if tz_name in {"local", "system"}:
            return None
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, Exception):
        return None


def _station_now() -> datetime:
    tz = _station_tz()
    return datetime.now(tz) if tz else datetime.now()


def _resolve_show_key(shows: dict, show_id: str) -> tuple[str | None, dict | None]:
    if show_id in shows:
        return show_id, shows[show_id]
    needle = (show_id or "").strip().lower()
    for sid, show in shows.items():
        if str(show.get("name", "")).strip().lower() == needle:
            return sid, show
    return None, None


def _build_generation_env() -> dict:
    return {
        "OLLAMA_URL": os.environ.get("OLLAMA_URL", "http://ollama.area4.net:11434"),
        "OLLAMA_MODEL": os.environ.get("OLLAMA_MODEL", "qwen3.5:4b"),
        "MINIMAX_API_KEY": os.environ.get("MINIMAX_API_KEY", ""),
        "MINIMAX_TOKEN_PLAN_API_KEY": os.environ.get("MINIMAX_TOKEN_PLAN_API_KEY", ""),
        "MINIMAX_MUSIC_MODEL": os.environ.get("MINIMAX_MUSIC_MODEL", "music-2.6"),
        "WRIT_CONSUME_SEGMENTS": os.environ.get("WRIT_CONSUME_SEGMENTS", "1"),
    }


def _run_talk_generation(show_id: str, count: int, job_registry: dict, env: dict, cache_invalidator: Callable[[str | None], None] | None = None):
    """Generate talk segments for a show in a background thread."""
    gen_script = PROJECT_ROOT / "mac" / "content_generator" / "talk_generator.py"
    cmd = [str(VENV_PYTHON), str(gen_script), "--show", show_id, "--count", str(count)]
    job_id = f"sched-talk-{show_id}-{int(time.time())}"
    state.add_log(show_id, "talk", f"Generating {count} segment(s) (job {job_id})")
    state.active_jobs[job_id] = {"show_id": show_id, "type": "talk", "count": count,
                                  "started": _station_now().isoformat(), "status": "running"}

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True,
            env={**os.environ, **env},
            cwd=str(PROJECT_ROOT / "mac" / "content_generator"),
            timeout=1800,
        )
        if proc.returncode == 0:
            state.add_log(show_id, "talk", f"Generation complete ({count} requested)")
            final_status = "completed"
        else:
            details = _summarize_process_failure(proc.stderr, proc.stdout)
            state.add_log(show_id, "talk", f"Generation failed: {details}", "error")
            final_status = "failed"
    except subprocess.TimeoutExpired:
        state.add_log(show_id, "talk", "Generation timed out", "error")
        final_status = "timeout"
    except Exception as e:
        state.add_log(show_id, "talk", f"Error: {e}", "error")
        final_status = "error"
    finally:
        state.active_jobs[job_id]["status"] = final_status
        finished = dict(state.active_jobs.get(job_id, {}))
        if finished:
            finished["job_id"] = job_id
            finished["ended"] = _station_now().isoformat()
            finished["status"] = final_status
            with state._lock:
                state.recent_jobs.insert(0, finished)
                state.recent_jobs = state.recent_jobs[:20]
        state.active_jobs.pop(job_id, None)

    state.record_run(show_id, "talk")
    if job_id in job_registry:
        job_registry[job_id]["status"] = final_status
    if final_status == "completed" and cache_invalidator:
        cache_invalidator("segments")


def _run_music_generation(show_id: str, count: int, bumper_style: str, job_registry: dict, env: dict, cache_invalidator: Callable[[str | None], None] | None = None):
    """Generate music bumpers for a show in a background thread."""
    gen_script = PROJECT_ROOT / "mac" / "content_generator" / "music_bumper_generator.py"
    if not gen_script.exists():
        state.add_log(show_id, "music", "music_bumper_generator.py not found", "error")
        return

    cmd = [str(VENV_PYTHON), str(gen_script), "--show", show_id, "--count", str(count)]
    job_id = f"sched-music-{show_id}-{int(time.time())}"
    state.add_log(show_id, "music", f"Generating {count} bumper(s) (job {job_id})")
    state.active_jobs[job_id] = {"show_id": show_id, "type": "music", "count": count,
                                  "started": _station_now().isoformat(), "status": "running"}

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True,
            env={**os.environ, **env},
            cwd=str(PROJECT_ROOT / "mac" / "content_generator"),
            timeout=3600,
        )
        if proc.returncode == 0:
            state.add_log(show_id, "music", f"Bumper generation complete")
            final_status = "completed"
        else:
            details = _summarize_process_failure(proc.stderr, proc.stdout)
            state.add_log(show_id, "music", f"Bumper generation failed: {details}", "error")
            final_status = "failed"
    except Exception as e:
        state.add_log(show_id, "music", f"Error: {e}", "error")
        final_status = "error"
    finally:
        state.active_jobs[job_id]["status"] = final_status
        finished = dict(state.active_jobs.get(job_id, {}))
        if finished:
            finished["job_id"] = job_id
            finished["ended"] = _station_now().isoformat()
            finished["status"] = final_status
            with state._lock:
                state.recent_jobs.insert(0, finished)
                state.recent_jobs = state.recent_jobs[:20]
        state.active_jobs.pop(job_id, None)

    state.record_run(show_id, "music")
    if final_status == "completed" and cache_invalidator:
        cache_invalidator("bumpers")


def _check_and_generate(job_registry: dict):
    """One pass: check inventory for all shows and trigger generation as needed."""
    try:
        data = _load_schedule()
    except Exception as e:
        print(f"[scheduler] Failed to load schedule: {e}")
        return

    shows = data.get("shows", {})
    env = {
        "OLLAMA_URL": os.environ.get("OLLAMA_URL", "http://ollama.area4.net:11434"),
        "OLLAMA_MODEL": os.environ.get("OLLAMA_MODEL", "qwen3.5:4b"),
        "MINIMAX_API_KEY": os.environ.get("MINIMAX_API_KEY", ""),
        "MINIMAX_TOKEN_PLAN_API_KEY": os.environ.get("MINIMAX_TOKEN_PLAN_API_KEY", ""),
        "MINIMAX_MUSIC_MODEL": os.environ.get("MINIMAX_MUSIC_MODEL", "music-2.6"),
        "WRIT_CONSUME_SEGMENTS": os.environ.get("WRIT_CONSUME_SEGMENTS", "1"),
    }

    for show_id, show in shows.items():
        gen_cfg = show.get("generation", {})

        # ── Talk segments ──────────────────────────────────────────
        talk_cfg = {**DEFAULT_TALK_CONFIG, **(gen_cfg.get("talk") or {})}
        if talk_cfg["enabled"]:
            # Skip if a generation job is already running for this show
            already_running = any(
                j.get("show_id") == show_id and j.get("type") == "talk"
                for j in state.active_jobs.values()
            )
            if already_running:
                continue

            inventory = _count_inventory(TALK_DIR, show_id)
            target = talk_cfg["target_inventory"]
            minimum = talk_cfg["min_inventory"]

            if inventory < minimum and _cadence_ok(show_id, "talk", talk_cfg["cadence"]):
                needed = target - inventory
                state.add_log(show_id, "talk",
                    f"Inventory {inventory} < min {minimum} → generating {needed}")
                t = threading.Thread(
                    target=_run_talk_generation,
                    args=(show_id, needed, job_registry, env, _inventory_invalidator),
                    daemon=True,
                )
                t.start()

        # ── Music bumpers ──────────────────────────────────────────
        music_cfg = {**DEFAULT_MUSIC_CONFIG, **(gen_cfg.get("music") or {})}
        if music_cfg["enabled"]:
            already_running = any(
                j.get("show_id") == show_id and j.get("type") == "music"
                for j in state.active_jobs.values()
                )
            if already_running:
                continue

            inventory = _count_inventory(BUMPERS_DIR, show_id)
            target = music_cfg["target_inventory"]
            minimum = music_cfg["min_inventory"]

            if inventory < minimum and _cadence_ok(show_id, "music", music_cfg["cadence"]):
                needed = target - inventory
                bumper_style = show.get("bumper_style", "ambient")
                state.add_log(show_id, "music",
                    f"Bumper inventory {inventory} < min {minimum} → generating {needed}")
                t = threading.Thread(
                    target=_run_music_generation,
                    args=(show_id, needed, bumper_style, job_registry, env, _inventory_invalidator),
                    daemon=True,
                )
                t.start()


def run_scheduler(job_registry: dict, check_interval: int = 300):
    """
    Main scheduler loop. Runs in a daemon thread.

    Args:
        job_registry: Shared dict from admin app for job tracking.
        check_interval: Seconds between inventory checks (default 5 min).
    """
    state.running = True
    print(f"[scheduler] Started. Check interval: {check_interval}s")

    while state.running:
        state.last_check = _station_now()
        try:
            _check_and_generate(job_registry)
        except Exception as e:
            print(f"[scheduler] Unexpected error: {e}")
        time.sleep(check_interval)


def start_scheduler(
    job_registry: dict,
    check_interval: int = 300,
    cache_invalidator: Callable[[str | None], None] | None = None,
) -> threading.Thread:
    """Start the scheduler in a daemon thread. Returns the thread."""
    global _inventory_invalidator
    _inventory_invalidator = cache_invalidator
    t = threading.Thread(
        target=run_scheduler,
        args=(job_registry, check_interval),
        daemon=True,
        name="writ-scheduler",
    )
    t.start()
    return t


def trigger_now(show_id: str, content_type: str, job_registry: dict) -> str:
    """Manually trigger generation for a show immediately, bypassing cadence check."""
    try:
        data = _load_schedule()
    except Exception as e:
        return f"Failed to load schedule: {e}"

    show_key, show = _resolve_show_key(data.get("shows", {}), show_id)
    if not show:
        return f"Show '{show_id}' not found"
    show_id = show_key or show_id

    talk_cfg, music_cfg = _effective_generation_configs(show)
    env = _build_generation_env()

    def _trigger_talk(needed_override: int | None = None) -> str:
        inventory = _count_inventory(TALK_DIR, show_id)
        needed = max(1, needed_override if needed_override is not None else talk_cfg["target_inventory"] - inventory)
        state.add_log(show_id, "talk", "Manual trigger requested")
        t = threading.Thread(
            target=_run_talk_generation,
            args=(show_id, needed, job_registry, env, _inventory_invalidator),
            daemon=True,
        )
        t.start()
        return f"talk ({needed} segments)"

    def _trigger_music(needed_override: int | None = None) -> str:
        inventory = _count_inventory(BUMPERS_DIR, show_id)
        needed = max(1, needed_override if needed_override is not None else music_cfg["target_inventory"] - inventory)
        bumper_style = show.get("bumper_style", "ambient")
        state.add_log(show_id, "music", "Manual trigger requested")
        t = threading.Thread(
            target=_run_music_generation,
            args=(show_id, needed, bumper_style, job_registry, env, _inventory_invalidator),
            daemon=True,
        )
        t.start()
        return f"music ({needed} bumpers)"

    if content_type == "talk":
        return f"Triggered {_trigger_talk(1)} for {show_id}"
    elif content_type == "music":
        return f"Triggered {_trigger_music(1)} for {show_id}"
    elif content_type in {"show", "all"}:
        parts = []
        parts.append(_trigger_talk())
        parts.append(_trigger_music())
        if not parts:
            return f"No enabled generators configured for {show_id}"
        return f"Triggered {', '.join(parts)} for {show_id}"

    return f"Unknown content type: {content_type}"
