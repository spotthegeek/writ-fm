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


class SchedulerState:
    """Shared state for the scheduler — readable by the API."""

    def __init__(self):
        self._lock = threading.Lock()
        self.running = False
        self.last_check: datetime | None = None
        self.last_run_per_show: dict[str, dict] = {}  # show_id → {talk: dt, music: dt}
        self.log: list[dict] = []  # recent activity log, newest first
        self.active_jobs: dict[str, dict] = {}  # job_id → info

    def add_log(self, show_id: str, content_type: str, msg: str, level: str = "info"):
        entry = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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
            self.last_run_per_show[show_id][content_type] = datetime.now()

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
            }


# Singleton
state = SchedulerState()


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
    return (datetime.now() - last).total_seconds() >= min_gap


def _load_schedule() -> dict:
    with open(SCHEDULE_PATH) as f:
        return yaml.safe_load(f)


def _run_talk_generation(show_id: str, count: int, job_registry: dict, env: dict):
    """Generate talk segments for a show in a background thread."""
    gen_script = PROJECT_ROOT / "mac" / "content_generator" / "talk_generator.py"
    cmd = [str(VENV_PYTHON), str(gen_script), "--show", show_id, "--count", str(count)]
    job_id = f"sched-talk-{show_id}-{int(time.time())}"
    state.add_log(show_id, "talk", f"Generating {count} segment(s) (job {job_id})")
    state.active_jobs[job_id] = {"show_id": show_id, "type": "talk", "count": count,
                                  "started": datetime.now().isoformat(), "status": "running"}

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
            state.active_jobs[job_id]["status"] = "completed"
        else:
            state.add_log(show_id, "talk", f"Generation failed: {proc.stderr[:200]}", "error")
            state.active_jobs[job_id]["status"] = "failed"
    except subprocess.TimeoutExpired:
        state.add_log(show_id, "talk", "Generation timed out", "error")
        state.active_jobs[job_id]["status"] = "timeout"
    except Exception as e:
        state.add_log(show_id, "talk", f"Error: {e}", "error")
        state.active_jobs[job_id]["status"] = "error"
    finally:
        state.active_jobs.pop(job_id, None)

    state.record_run(show_id, "talk")
    if job_id in job_registry:
        job_registry[job_id]["status"] = state.active_jobs.get(job_id, {}).get("status", "done")


def _run_music_generation(show_id: str, count: int, bumper_style: str, job_registry: dict, env: dict):
    """Generate music bumpers for a show in a background thread."""
    gen_script = PROJECT_ROOT / "mac" / "content_generator" / "music_bumper_generator.py"
    if not gen_script.exists():
        state.add_log(show_id, "music", "music_bumper_generator.py not found", "error")
        return

    cmd = [str(VENV_PYTHON), str(gen_script), "--show", show_id, "--count", str(count)]
    job_id = f"sched-music-{show_id}-{int(time.time())}"
    state.add_log(show_id, "music", f"Generating {count} bumper(s) (job {job_id})")
    state.active_jobs[job_id] = {"show_id": show_id, "type": "music", "count": count,
                                  "started": datetime.now().isoformat(), "status": "running"}

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
            state.active_jobs[job_id]["status"] = "completed"
        else:
            state.add_log(show_id, "music", f"Bumper generation failed: {proc.stderr[:200]}", "error")
            state.active_jobs[job_id]["status"] = "failed"
    except Exception as e:
        state.add_log(show_id, "music", f"Error: {e}", "error")
        state.active_jobs[job_id]["status"] = "error"
    finally:
        state.active_jobs.pop(job_id, None)

    state.record_run(show_id, "music")


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
        "OLLAMA_MODEL": os.environ.get("OLLAMA_MODEL", "gemma3:12b"),
        "MINIMAX_API_KEY": os.environ.get("MINIMAX_API_KEY", ""),
        "WRIT_CONSUME_SEGMENTS": os.environ.get("WRIT_CONSUME_SEGMENTS", "1"),
    }

    for show_id, show in shows.items():
        gen_cfg = show.get("generation", {})

        # ── Talk segments ──────────────────────────────────────────
        talk_cfg = {**DEFAULT_TALK_CONFIG, **gen_cfg.get("talk", {})}
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
                    args=(show_id, needed, job_registry, env),
                    daemon=True,
                )
                t.start()

        # ── Music bumpers ──────────────────────────────────────────
        music_cfg = {**DEFAULT_MUSIC_CONFIG, **gen_cfg.get("music", {})}
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
                    args=(show_id, needed, bumper_style, job_registry, env),
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
        state.last_check = datetime.now()
        try:
            _check_and_generate(job_registry)
        except Exception as e:
            print(f"[scheduler] Unexpected error: {e}")
        time.sleep(check_interval)


def start_scheduler(job_registry: dict, check_interval: int = 300) -> threading.Thread:
    """Start the scheduler in a daemon thread. Returns the thread."""
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

    show = data.get("shows", {}).get(show_id)
    if not show:
        return f"Show '{show_id}' not found"

    gen_cfg = show.get("generation", {})
    env = {
        "OLLAMA_URL": os.environ.get("OLLAMA_URL", "http://ollama.area4.net:11434"),
        "OLLAMA_MODEL": os.environ.get("OLLAMA_MODEL", "gemma3:12b"),
        "MINIMAX_API_KEY": os.environ.get("MINIMAX_API_KEY", ""),
        "WRIT_CONSUME_SEGMENTS": os.environ.get("WRIT_CONSUME_SEGMENTS", "1"),
    }

    if content_type == "talk":
        cfg = {**DEFAULT_TALK_CONFIG, **gen_cfg.get("talk", {})}
        inventory = _count_inventory(TALK_DIR, show_id)
        needed = max(1, cfg["target_inventory"] - inventory)
        t = threading.Thread(
            target=_run_talk_generation,
            args=(show_id, needed, job_registry, env),
            daemon=True,
        )
        t.start()
        return f"Triggered talk generation for {show_id} ({needed} segments)"

    elif content_type == "music":
        cfg = {**DEFAULT_MUSIC_CONFIG, **gen_cfg.get("music", {})}
        inventory = _count_inventory(BUMPERS_DIR, show_id)
        needed = max(1, cfg["target_inventory"] - inventory)
        bumper_style = show.get("bumper_style", "ambient")
        t = threading.Thread(
            target=_run_music_generation,
            args=(show_id, needed, bumper_style, job_registry, env),
            daemon=True,
        )
        t.start()
        return f"Triggered music generation for {show_id} ({needed} bumpers)"

    return f"Unknown content type: {content_type}"
