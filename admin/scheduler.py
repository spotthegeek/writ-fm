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
from shared.settings import minimax_music_model, ollama_model, ollama_url

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

FAILURE_BACKOFF_SECONDS = 1800  # 30 min cooldown after a failed generation

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


def _effective_generation_configs(show: dict) -> tuple[dict, dict]:
    """Merge explicit generation config with conservative defaults."""
    gen_cfg = show.get("generation") or {}

    raw_talk = gen_cfg.get("talk") or {}
    raw_music = gen_cfg.get("music") or {}

    talk_cfg = {**DEFAULT_TALK_CONFIG, **raw_talk}

    music_cfg = {**DEFAULT_MUSIC_CONFIG, **raw_music}

    return talk_cfg, music_cfg


class SchedulerState:
    """Shared state for the scheduler — readable by the API."""

    def __init__(self):
        self._lock = threading.Lock()
        self.running = False
        self.last_check: datetime | None = None
        self.last_run_per_show: dict[str, dict] = {}  # show_id → {talk: dt, music: dt}
        self.last_failure_per_show: dict[str, dict] = {}  # show_id → {talk: dt, music: dt}
        self.log: list[dict] = []  # recent activity log, newest first
        self.active_jobs: dict[str, dict] = {}  # job_id → info
        self.recent_jobs: list[dict] = []  # recent job history, newest first

    def add_log(self, show_id: str, content_type: str, msg: str, level: str = "info", job_id: str | None = None):
        entry = {
            "ts": _station_now().strftime("%Y-%m-%d %H:%M:%S"),
            "show_id": show_id,
            "type": content_type,
            "msg": msg,
            "level": level,
        }
        if job_id:
            entry["job_id"] = job_id
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

    def record_failure(self, show_id: str, content_type: str):
        with self._lock:
            if show_id not in self.last_failure_per_show:
                self.last_failure_per_show[show_id] = {}
            self.last_failure_per_show[show_id][content_type] = _station_now()

    def last_run(self, show_id: str, content_type: str) -> datetime | None:
        with self._lock:
            return self.last_run_per_show.get(show_id, {}).get(content_type)

    def last_failure(self, show_id: str, content_type: str) -> datetime | None:
        with self._lock:
            return self.last_failure_per_show.get(show_id, {}).get(content_type)

    def in_failure_backoff(self, show_id: str, content_type: str) -> bool:
        fail_time = self.last_failure(show_id, content_type)
        if fail_time is None:
            return False
        return (_station_now() - fail_time).total_seconds() < FAILURE_BACKOFF_SECONDS

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "last_check": self.last_check.isoformat() if self.last_check else None,
                "last_run_per_show": {
                    sid: {ct: dt.isoformat() for ct, dt in runs.items()}
                    for sid, runs in self.last_run_per_show.items()
                },
                "last_failure_per_show": {
                    sid: {ct: dt.isoformat() for ct, dt in fails.items()}
                    for sid, fails in self.last_failure_per_show.items()
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
    env = {
        "OLLAMA_URL": ollama_url(),
        "OLLAMA_MODEL": ollama_model(),
        "MINIMAX_API_KEY": os.environ.get("MINIMAX_API_KEY", ""),
        "MINIMAX_TOKEN_PLAN_API_KEY": os.environ.get("MINIMAX_TOKEN_PLAN_API_KEY", ""),
        "MINIMAX_MUSIC_MODEL": minimax_music_model(),
        "WRIT_CONSUME_SEGMENTS": os.environ.get("WRIT_CONSUME_SEGMENTS", "1"),
    }
    hf_token = os.environ.get("HF_TOKEN", "")
    if hf_token:
        env["HF_TOKEN"] = hf_token
    return env


def _run_talk_generation(show_id: str, count: int, job_registry: dict, env: dict, cache_invalidator: Callable[[str | None], None] | None = None, job_id: str | None = None):
    """Generate talk segments for a show in a background thread."""
    gen_script = PROJECT_ROOT / "station" / "content_generator" / "talk_generator.py"
    cmd = [str(VENV_PYTHON), str(gen_script), "--show", show_id, "--count", str(count)]
    if job_id is None:
        job_id = f"sched-talk-{show_id}-{int(time.time())}"
    state.add_log(show_id, "talk", f"Generating {count} segment(s) (job {job_id})", job_id=job_id)
    state.active_jobs[job_id] = {"show_id": show_id, "type": "talk", "count": count,
                                  "started": _station_now().isoformat(), "status": "running"}

    def _jlog(msg: str):
        ts = _station_now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        if job_id in job_registry:
            job_registry[job_id]["log"].append(line)
        print(line)

    if job_id not in job_registry:
        job_registry[job_id] = {
            "id": job_id, "show_id": show_id, "content_type": "talk",
            "segment_type": "scheduled", "source": "scheduler",
            "status": "running", "log": [],
            "created_at": _station_now().isoformat(), "completed_at": None,
        }
    _jlog(f"Starting: show={show_id} count={count}")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, **env},
            cwd=str(PROJECT_ROOT / "station" / "content_generator"),
        )
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                _jlog(line)
        proc.wait(timeout=1800)
        if proc.returncode == 0:
            state.add_log(show_id, "talk", f"Generation complete ({count} requested)", job_id=job_id)
            final_status = "completed"
        else:
            state.add_log(show_id, "talk", f"Generation failed (exit {proc.returncode})", "error", job_id=job_id)
            final_status = "failed"
    except subprocess.TimeoutExpired:
        proc.kill()
        state.add_log(show_id, "talk", "Generation timed out", "error", job_id=job_id)
        final_status = "timeout"
    except Exception as e:
        state.add_log(show_id, "talk", f"Error: {e}", "error", job_id=job_id)
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

    _jlog(f"Generation {'complete' if final_status == 'completed' else final_status}.")
    state.record_run(show_id, "talk")
    if final_status in ("failed", "error", "timeout"):
        state.record_failure(show_id, "talk")
    if job_id in job_registry:
        job_registry[job_id]["status"] = final_status
        job_registry[job_id]["completed_at"] = _station_now().isoformat()
    if final_status == "completed" and cache_invalidator:
        cache_invalidator("segments")


def _run_music_generation(show_id: str, count: int, bumper_style: str, job_registry: dict, env: dict, cache_invalidator: Callable[[str | None], None] | None = None, job_id: str | None = None):
    """Generate music bumpers for a show in a background thread."""
    gen_script = PROJECT_ROOT / "station" / "content_generator" / "music_bumper_generator.py"
    if not gen_script.exists():
        state.add_log(show_id, "music", "music_bumper_generator.py not found", "error")
        return

    cmd = [str(VENV_PYTHON), str(gen_script), "--show", show_id, "--count", str(count)]
    if job_id is None:
        job_id = f"sched-music-{show_id}-{int(time.time())}"
    state.add_log(show_id, "music", f"Generating {count} bumper(s) (job {job_id})", job_id=job_id)
    state.active_jobs[job_id] = {"show_id": show_id, "type": "music", "count": count,
                                  "started": _station_now().isoformat(), "status": "running"}

    def _jlog(msg: str):
        ts = _station_now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        if job_id in job_registry:
            job_registry[job_id]["log"].append(line)
        print(line)

    if job_id not in job_registry:
        job_registry[job_id] = {
            "id": job_id, "show_id": show_id, "content_type": "music",
            "segment_type": "scheduled", "source": "scheduler",
            "status": "running", "log": [],
            "created_at": _station_now().isoformat(), "completed_at": None,
        }
    _jlog(f"Starting: show={show_id} count={count}")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, **env},
            cwd=str(PROJECT_ROOT / "station" / "content_generator"),
        )
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                _jlog(line)
        proc.wait(timeout=3600)
        if proc.returncode == 0:
            state.add_log(show_id, "music", "Bumper generation complete", job_id=job_id)
            final_status = "completed"
        else:
            state.add_log(show_id, "music", f"Bumper generation failed (exit {proc.returncode})", "error", job_id=job_id)
            final_status = "failed"
    except subprocess.TimeoutExpired:
        proc.kill()
        state.add_log(show_id, "music", "Bumper generation timed out", "error", job_id=job_id)
        final_status = "timeout"
    except Exception as e:
        state.add_log(show_id, "music", f"Error: {e}", "error", job_id=job_id)
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

    _jlog(f"Generation {'complete' if final_status == 'completed' else final_status}.")
    state.record_run(show_id, "music")
    if final_status in ("failed", "error", "timeout"):
        state.record_failure(show_id, "music")
    if job_id in job_registry:
        job_registry[job_id]["status"] = final_status
        job_registry[job_id]["completed_at"] = _station_now().isoformat()
    if final_status == "completed" and cache_invalidator:
        cache_invalidator("bumpers")


BRIEFING_SHOW_IDS = ["briefing_ai", "briefing_crypto", "briefing_tech", "briefing_news_aus"]


def _cleanup_expired_segments(show_id: str, max_days: int) -> int:
    """Delete talk segments (audio + sidecar .json + .plays.json) older than max_days.
    Returns count of audio files deleted."""
    show_dir = TALK_DIR / show_id
    if not show_dir.exists():
        return 0
    cutoff = timedelta(days=max_days)
    now = datetime.now()
    deleted = 0
    for f in list(show_dir.iterdir()):
        if not f.is_file() or f.suffix.lower() not in AUDIO_EXTS:
            continue
        sidecar = f.with_suffix(".json")
        age = None
        if sidecar.exists():
            try:
                import json as _json
                meta = _json.loads(sidecar.read_text())
                generated_at = meta.get("generated_at")
                if generated_at:
                    age = now - datetime.fromisoformat(generated_at.replace("Z", "+00:00").split("+")[0])
            except Exception:
                pass
        if age is None:
            age = now - datetime.fromtimestamp(f.stat().st_mtime)
        if age > cutoff:
            for path in (f, sidecar, f.with_suffix(".plays.json")):
                if path.exists():
                    try:
                        path.unlink()
                    except Exception:
                        pass
            deleted += 1
    return deleted


def _briefing_daily_has_deps() -> bool:
    """Check that at least 2 of 4 category briefings exist with mtime < 26 hours."""
    now = datetime.now()
    cutoff = timedelta(hours=26)
    found = 0
    for show_id in BRIEFING_SHOW_IDS:
        show_dir = TALK_DIR / show_id
        if not show_dir.exists():
            continue
        candidates = list(show_dir.glob("news_briefing_*.json"))
        if not candidates:
            candidates = list(show_dir.glob("*.json"))
        for jf in candidates:
            age = now - datetime.fromtimestamp(jf.stat().st_mtime)
            if age < cutoff:
                found += 1
                break
    return found >= 2


def _check_and_generate(job_registry: dict):
    """One pass: check inventory for all shows and trigger generation as needed."""
    try:
        data = _load_schedule()
    except Exception as e:
        print(f"[scheduler] Failed to load schedule: {e}")
        return

    shows = data.get("shows", {})
    env = _build_generation_env()

    for show_id, show in shows.items():
        gen_cfg = show.get("generation", {})

        # ── Talk segments ──────────────────────────────────────────
        talk_cfg = {**DEFAULT_TALK_CONFIG, **(gen_cfg.get("talk") or {})}
        if talk_cfg["enabled"]:
            if state.in_failure_backoff(show_id, "talk"):
                continue

            # Skip if a generation job is already running for this show
            already_running = any(
                j.get("show_id") == show_id and j.get("type") == "talk"
                for j in state.active_jobs.values()
            )
            if already_running:
                continue

            if show_id == "briefing_daily" and not _briefing_daily_has_deps():
                state.add_log(show_id, "talk", "Waiting for category briefings (need ≥2 of 4 recent)")
                continue

            inventory = _count_inventory(TALK_DIR, show_id)
            target = int(talk_cfg["target_inventory"])
            minimum = int(talk_cfg["min_inventory"])

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
            if state.in_failure_backoff(show_id, "music"):
                continue

            already_running = any(
                j.get("show_id") == show_id and j.get("type") == "music"
                for j in state.active_jobs.values()
                )
            if already_running:
                continue

            inventory = _count_inventory(BUMPERS_DIR, show_id)
            target = int(music_cfg["target_inventory"])
            minimum = int(music_cfg["min_inventory"])

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
    last_cleanup = 0.0

    while state.running:
        state.last_check = _station_now()
        try:
            _check_and_generate(job_registry)
        except Exception as e:
            print(f"[scheduler] Unexpected error: {e}")

        now_ts = time.time()
        if now_ts - last_cleanup >= 3600:
            last_cleanup = now_ts
            try:
                data = _load_schedule()
                for show_id, show in data.get("shows", {}).items():
                    max_days = (show.get("content_lifecycle") or {}).get("talk", {}).get("max_days")
                    if max_days:
                        n = _cleanup_expired_segments(show_id, int(max_days))
                        if n:
                            print(f"[scheduler] Cleaned up {n} expired segment(s) for {show_id}")
            except Exception as e:
                print(f"[scheduler] Cleanup error: {e}")

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


def trigger_now(show_id: str, content_type: str, job_registry: dict) -> dict:
    """Manually trigger generation for a show immediately, bypassing cadence check.

    Returns a dict with keys: message, job_id (or job_ids for "all").
    """
    try:
        data = _load_schedule()
    except Exception as e:
        return {"message": f"Failed to load schedule: {e}", "job_id": None}

    show_key, show = _resolve_show_key(data.get("shows", {}), show_id)
    if not show:
        return {"message": f"Show '{show_id}' not found", "job_id": None}
    show_id = show_key or show_id

    talk_cfg, music_cfg = _effective_generation_configs(show)
    env = _build_generation_env()

    def _trigger_talk(needed_override: int | None = None) -> tuple[str, str]:
        inventory = _count_inventory(TALK_DIR, show_id)
        needed = max(1, needed_override if needed_override is not None else int(talk_cfg["target_inventory"]) - inventory)
        jid = f"sched-talk-{show_id}-{int(time.time())}"
        job_registry[jid] = {
            "id": jid, "show_id": show_id, "content_type": "talk",
            "segment_type": "scheduled", "source": "scheduler",
            "status": "running", "log": [],
            "created_at": _station_now().isoformat(), "completed_at": None,
        }
        state.add_log(show_id, "talk", "Manual trigger requested", job_id=jid)
        t = threading.Thread(
            target=_run_talk_generation,
            args=(show_id, needed, job_registry, env, _inventory_invalidator, jid),
            daemon=True,
        )
        t.start()
        return f"talk ({needed} segments)", jid

    def _trigger_music(needed_override: int | None = None) -> tuple[str, str]:
        inventory = _count_inventory(BUMPERS_DIR, show_id)
        needed = max(1, needed_override if needed_override is not None else int(music_cfg["target_inventory"]) - inventory)
        bumper_style = show.get("bumper_style", "ambient")
        jid = f"sched-music-{show_id}-{int(time.time())}"
        job_registry[jid] = {
            "id": jid, "show_id": show_id, "content_type": "music",
            "segment_type": "scheduled", "source": "scheduler",
            "status": "running", "log": [],
            "created_at": _station_now().isoformat(), "completed_at": None,
        }
        state.add_log(show_id, "music", "Manual trigger requested", job_id=jid)
        t = threading.Thread(
            target=_run_music_generation,
            args=(show_id, needed, bumper_style, job_registry, env, _inventory_invalidator, jid),
            daemon=True,
        )
        t.start()
        return f"music ({needed} bumpers)", jid

    if content_type == "talk":
        desc, jid = _trigger_talk(1)
        return {"message": f"Triggered {desc} for {show_id}", "job_id": jid}
    elif content_type == "music":
        desc, jid = _trigger_music(1)
        return {"message": f"Triggered {desc} for {show_id}", "job_id": jid}
    elif content_type in {"show", "all"}:
        t_desc, t_jid = _trigger_talk()
        m_desc, m_jid = _trigger_music()
        return {"message": f"Triggered {t_desc}, {m_desc} for {show_id}", "job_ids": [t_jid, m_jid], "job_id": t_jid}

    return {"message": f"Unknown content type: {content_type}", "job_id": None}
