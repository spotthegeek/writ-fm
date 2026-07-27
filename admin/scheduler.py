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
from shared.config_loader import load_station_config, load_station
from shared.settings import minimax_music_model, ollama_model, ollama_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
OUTPUT_DIR = PROJECT_ROOT / "output"
TALK_DIR = OUTPUT_DIR / "talk_segments"
BUMPERS_DIR = OUTPUT_DIR / "music_bumpers"
SOURCE_CACHE_DIR = OUTPUT_DIR / "source_cache"
SCRIPTS_DIR = OUTPUT_DIR / "scripts"
JOBS_DIR = OUTPUT_DIR / "jobs"
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python3"
AUDIO_EXTS = {".wav", ".mp3", ".flac"}

# Cached source media: yt-dlp downloads that talk_generator has already copied into
# a segment. 1.1 unlinks these at copy time; this TTL is the backstop for anything
# that predates it or whose generation died between download and copy.
SOURCE_CACHE_TTL_DAYS = int(os.environ.get("WRIT_SOURCE_CACHE_TTL_DAYS", "30"))
JOBS_TTL_DAYS = int(os.environ.get("WRIT_JOBS_TTL_DAYS", "30"))

# Script records. Safe to expire only since 3.5 moved the used-source ledger into
# output/state/, and only while the TTL stays at or above the re-air window: the
# index is rebuilt from a scan of this directory if it is ever lost, so a shorter
# TTL would silently un-use a source that is still inside its window.
SOURCE_REUSE_DAYS = max(0, int(os.environ.get("WRIT_SOURCE_REUSE_DAYS", "90")))
SCRIPTS_TTL_DAYS = max(int(os.environ.get("WRIT_SCRIPTS_TTL_DAYS", "180")), SOURCE_REUSE_DAYS)
CACHED_MEDIA_EXTS = {".mp3", ".m4a", ".webm", ".opus", ".wav", ".part", ".ytdl"}

CADENCE_SECONDS = {
    "continuous": 0,        # generate as soon as inventory drops
    "hourly":     3600,
    "daily":      86400,
    "weekly":     604800,
    "monthly":    2592000,
}

# A broken source is worth retrying soon, then progressively less often. The
# ladder advances on each consecutive failure and resets on the first success,
# so a transient Ollama timeout costs 30 minutes while a genuinely broken source
# stops asking every half hour forever.
FAILURE_BACKOFF_STEPS = (1800, 3600, 14400, 43200)  # 30 min → 1 h → 4 h → 12 h
FAILURE_BACKOFF_SECONDS = FAILURE_BACKOFF_STEPS[0]  # first step; kept for callers

# "Cold" is a source with nothing new, which is not a fault. Retrying it hard
# buys nothing — a subreddit does not age in five minutes — so it gets its own
# long, quiet ladder. Scarcity overrides it: see _check_and_generate.
COLD_BACKOFF_STEPS = (14400, 43200, 86400)  # 4 h → 12 h → 24 h

# At or below this many talk segments a show is genuinely scarce and may reach
# into the source's archive. Above it, a dry recency window is reported cold and
# nothing is generated: the segments already on disk keep airing, which sounds
# better than a decade-old thread read as though it were this week. Station-wide
# by operator decision — the per-show knob is `archive_fallback`, below.
ARCHIVE_SCARCITY_FLOOR = int(os.environ.get("WRIT_ARCHIVE_SCARCITY_FLOOR", "3"))

# Substrings the generator prints when a source is legitimately out of new
# material. Written in Session B to be classifiable; classify on them rather
# than re-deriving the state.
COLD_OUTPUT_MARKERS = (
    "including the archive",         # No unused posts found for r/X (N scanned, including the archive)
    "including the back catalogue",  # No unused YouTube entries found (50 scanned, ...)
    "archive held back",             # above the scarcity floor, so the archive was not consulted
)


def _output_looks_cold(lines: list[str]) -> bool:
    """Whether a failed generation ran out of material rather than breaking."""
    return any(marker in line for line in lines for marker in COLD_OUTPUT_MARKERS)


def _backoff_seconds(kind: str, streak: int) -> int:
    """How long to wait after `streak` consecutive events of this kind."""
    steps = COLD_BACKOFF_STEPS if kind == "cold" else FAILURE_BACKOFF_STEPS
    return steps[min(max(streak, 1), len(steps)) - 1]

DEFAULT_TALK_CONFIG = {
    "enabled": False,
    "min_inventory": 5,
    "target_inventory": 15,
    "cadence": "continuous",
    # Whether this show may air material from its source's back catalogue once
    # it hits the scarcity floor. On for shows whose material is timeless
    # (r/nosleep, r/talesfromtechsupport); a show where a three-year-old thread
    # is stale in substance rather than only in framing can set this false and
    # go cold instead.
    "archive_fallback": True,
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
        self.last_cold_per_show: dict[str, dict] = {}  # show_id → {talk: dt, music: dt}
        # show_id → {talk|music: {"kind": "failed"|"cold", "streak": int, "at": dt}}
        self.backoff_per_show: dict[str, dict] = {}
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

    def _advance_backoff(self, show_id: str, content_type: str, kind: str) -> int:
        """Move this show/type one rung up its ladder and return the wait in seconds.

        A change of kind restarts the streak: a source that was failing and is
        now merely cold has had its problem fixed, and vice versa."""
        with self._lock:
            slot = self.backoff_per_show.setdefault(show_id, {})
            previous = slot.get(content_type) or {}
            streak = int(previous.get("streak", 0)) + 1 if previous.get("kind") == kind else 1
            slot[content_type] = {"kind": kind, "streak": streak, "at": _station_now()}
        return _backoff_seconds(kind, streak)

    def record_failure(self, show_id: str, content_type: str) -> int:
        """Record a broken generation. Returns the backoff now in force."""
        with self._lock:
            if show_id not in self.last_failure_per_show:
                self.last_failure_per_show[show_id] = {}
            self.last_failure_per_show[show_id][content_type] = _station_now()
        return self._advance_backoff(show_id, content_type, "failed")

    def record_cold(self, show_id: str, content_type: str) -> int:
        """Record a source with nothing new. Returns the backoff now in force.

        Deliberately does *not* touch last_failure_per_show — cold is a normal
        state, and two months of it reading as failure is what hid the
        starvation in the first place."""
        with self._lock:
            if show_id not in self.last_cold_per_show:
                self.last_cold_per_show[show_id] = {}
            self.last_cold_per_show[show_id][content_type] = _station_now()
        return self._advance_backoff(show_id, content_type, "cold")

    def record_success(self, show_id: str, content_type: str):
        """Clear the ladder. A source that produced something is healthy again."""
        with self._lock:
            self.backoff_per_show.get(show_id, {}).pop(content_type, None)

    def last_run(self, show_id: str, content_type: str) -> datetime | None:
        with self._lock:
            return self.last_run_per_show.get(show_id, {}).get(content_type)

    def last_failure(self, show_id: str, content_type: str) -> datetime | None:
        with self._lock:
            return self.last_failure_per_show.get(show_id, {}).get(content_type)

    def last_cold(self, show_id: str, content_type: str) -> datetime | None:
        with self._lock:
            return self.last_cold_per_show.get(show_id, {}).get(content_type)

    def backoff_state(self, show_id: str, content_type: str) -> tuple[str, int]:
        """(kind, seconds remaining). kind is "" when nothing is in force."""
        with self._lock:
            entry = (self.backoff_per_show.get(show_id, {}) or {}).get(content_type)
        if not entry:
            return "", 0
        kind = str(entry.get("kind") or "failed")
        wait = _backoff_seconds(kind, int(entry.get("streak", 1)))
        elapsed = (_station_now() - entry["at"]).total_seconds()
        return (kind, int(wait - elapsed)) if elapsed < wait else ("", 0)

    def in_failure_backoff(self, show_id: str, content_type: str) -> bool:
        return bool(self.backoff_state(show_id, content_type)[0])

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
                "last_cold_per_show": {
                    sid: {ct: dt.isoformat() for ct, dt in colds.items()}
                    for sid, colds in self.last_cold_per_show.items()
                },
                "backoff_per_show": {
                    sid: {
                        ct: {**entry, "at": entry["at"].isoformat()}
                        for ct, entry in slots.items()
                    }
                    for sid, slots in self.backoff_per_show.items()
                },
                "active_jobs": dict(self.active_jobs),
                "recent_jobs": list(self.recent_jobs[:20]),
            }


# Singleton
state = SchedulerState()
_inventory_invalidator: Callable[[str | None], None] | None = None


_STRUCTURAL_SEGMENT_PREFIXES = (
    "station_id_", "show_intro_", "show_outro_", "news_briefing_",
)

def _count_inventory(directory: Path, show_id: str) -> int:
    d = directory / show_id
    if not d.exists():
        return 0
    return sum(
        1 for f in d.iterdir()
        if f.is_file()
        and f.suffix.lower() in AUDIO_EXTS
        and not f.name.startswith(_STRUCTURAL_SEGMENT_PREFIXES)
    )


def _cadence_ok(show_id: str, content_type: str, cadence: str, time_after: str | None = None) -> bool:
    """Return True if the cadence allows generation now.

    For daily/weekly/etc. cadences with a time_after value (e.g. "05:30"),
    uses a calendar-day check: allowed once per day after that local time.
    Without time_after, falls back to a rolling minimum-gap check.
    """
    min_gap = CADENCE_SECONDS.get(cadence, 0)
    if min_gap == 0:
        return True

    now = _station_now()

    if cadence == "daily" and time_after:
        try:
            hh, mm = (int(x) for x in time_after.split(":"))
        except Exception:
            hh, mm = 0, 0
        if now.hour < hh or (now.hour == hh and now.minute < mm):
            return False  # too early in the day
        last = state.last_run(show_id, content_type)
        if last is None:
            return True
        return last.date() < now.date()

    last = state.last_run(show_id, content_type)
    if last is None:
        return True
    return (now - last).total_seconds() >= min_gap


def _load_schedule() -> dict:
    return load_station_config(CONFIG_DIR)


def _station_tz():
    try:
        data = load_station(CONFIG_DIR)
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
        "KOKORO_SERVICE_URL": os.environ.get("KOKORO_SERVICE_URL", ""),
    }
    hf_token = os.environ.get("HF_TOKEN", "")
    if hf_token:
        env["HF_TOKEN"] = hf_token
    return env


def _run_talk_generation(show_id: str, count: int, job_registry: dict, env: dict, cache_invalidator: Callable[[str | None], None] | None = None, job_id: str | None = None, allow_archive: bool = True):
    """Generate talk segments for a show in a background thread.

    `allow_archive` is the 3.10 gate: False tells the generator that this show is
    above the scarcity floor and should report cold rather than reach into the
    source's back catalogue. It defaults True so a hand-run generation, where the
    operator has explicitly asked for content, still gets everything available."""
    gen_script = PROJECT_ROOT / "station" / "content_generator" / "talk_generator.py"
    cmd = [str(VENV_PYTHON), str(gen_script), "--show", show_id, "--count", str(count)]
    env = {**env, "WRIT_ALLOW_ARCHIVE": "1" if allow_archive else "0"}
    if job_id is None:
        job_id = f"sched-talk-{show_id}-{int(time.time())}"
    state.add_log(show_id, "talk", f"Generating {count} segment(s) (job {job_id})", job_id=job_id)
    state.active_jobs[job_id] = {"show_id": show_id, "type": "talk", "count": count,
                                  "started": _station_now().isoformat(), "status": "running"}

    output_lines: list[str] = []

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
                output_lines.append(line)
                _jlog(line)
        proc.wait(timeout=1800)
        if proc.returncode == 0:
            state.add_log(show_id, "talk", f"Generation complete ({count} requested)", job_id=job_id)
            final_status = "completed"
        elif _output_looks_cold(output_lines):
            # Not a fault: the sources have nothing this show has not already
            # aired. Logged as its own state so a starving show is visible as
            # starving instead of hiding inside the failure count.
            state.add_log(show_id, "talk", "Sources cold — nothing new to generate", "warn", job_id=job_id)
            final_status = "cold"
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
    if final_status == "completed":
        state.record_success(show_id, "talk")
    elif final_status == "cold":
        wait = state.record_cold(show_id, "talk")
        _jlog(f"Sources cold; next attempt in {wait // 3600}h{(wait % 3600) // 60:02d}m.")
    elif final_status in ("failed", "error", "timeout"):
        wait = state.record_failure(show_id, "talk")
        _jlog(f"Backing off {wait // 60} min before retrying.")
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
    if final_status == "completed":
        state.record_success(show_id, "music")
    elif final_status in ("failed", "error", "timeout"):
        wait = state.record_failure(show_id, "music")
        _jlog(f"Backing off {wait // 60} min before retrying.")
    if job_id in job_registry:
        job_registry[job_id]["status"] = final_status
        job_registry[job_id]["completed_at"] = _station_now().isoformat()
    if final_status == "completed" and cache_invalidator:
        cache_invalidator("bumpers")


def _segment_generated_at(f: Path, now: datetime) -> datetime:
    """Generation time of a segment, from its sidecar, falling back to mtime."""
    sidecar = f.with_suffix(".json")
    if sidecar.exists():
        try:
            import json as _json
            generated_at = _json.loads(sidecar.read_text()).get("generated_at")
            if generated_at:
                dt = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=now.tzinfo)
                return dt.astimezone(now.tzinfo)
        except Exception:
            pass
    return datetime.fromtimestamp(f.stat().st_mtime, tz=now.tzinfo)


def _segment_times(show_id: str, now: datetime) -> list[tuple[datetime, Path]]:
    """All talk segments for a show as (generated_at, audio_path), oldest first."""
    show_dir = TALK_DIR / show_id
    if not show_dir.exists():
        return []
    items = [
        (_segment_generated_at(f, now), f)
        for f in show_dir.iterdir()
        if f.is_file() and f.suffix.lower() in AUDIO_EXTS
    ]
    items.sort(key=lambda pair: pair[0])
    return items


def _delete_segment(f: Path) -> None:
    """Remove an audio segment along with its sidecars."""
    for path in (f, f.with_suffix(".json"), f.with_suffix(".plays.json")):
        if path.exists():
            try:
                path.unlink()
            except Exception:
                pass


def _cleanup_expired_segments(show_id: str, max_days: int, floor: int | None = None) -> int:
    """Delete talk segments (audio + sidecar .json + .plays.json) older than max_days,
    never taking the show below the scarcity floor. Returns audio files deleted.

    The floor is half of the 3.10/3.11 pair and only works with the other half.
    3.10 declines to top a show up from the archive while it is above the floor;
    without this, expiry would walk the show straight down through the floor to
    zero while the generator stood back. Keeping the newest `floor` segments means
    an over-floor show holds steady on slightly older material — which is what a
    listener wants — instead of going quiet or airing a 2014 thread.

    Expiry is still what creates the inventory gap that triggers generation, so
    this deliberately keeps expiring everything above the floor."""
    if floor is None:
        floor = ARCHIVE_SCARCITY_FLOOR
    now = _station_now()
    cutoff = timedelta(days=max_days)
    segments = _segment_times(show_id, now)  # oldest first
    # Newest `floor` segments are exempt; everything older is fair game.
    protected = {path for _, path in segments[max(0, len(segments) - floor):]} if floor > 0 else set()
    deleted = 0
    for generated_at, f in segments:
        if f in protected:
            continue
        if now - generated_at > cutoff:
            _delete_segment(f)
            deleted += 1
    return deleted


def _janitor_sweep(dry_run: bool = False) -> dict:
    """Enforce retention on everything under output/ that nothing else owns.

    Segment expiry is handled per-show by _cleanup_expired_segments (age of the
    content). This is the complement: files that no longer belong to anything —
    cache copies already consumed, sidecars whose audio is gone, stale job records.
    Without it, `output/` only ever grows; it reached 99% of root in July 2026.

    output/scripts/ is swept since 3.5 moved the dedupe ledger to output/state/ —
    but only the `talk_*.json` records, and only past a TTL that cannot be set
    below the re-air window. The `.<show>_source_rotation.json` files in the same
    directory are live rotation state, not script records, and are never touched
    (the glob excludes them; do not widen it to `*.json`).

    Returns a per-category {files, bytes} report. dry_run reports without deleting.
    """
    now = time.time()
    report: dict[str, dict] = {}

    def _sweep(category: str, paths) -> None:
        files = 0
        freed = 0
        for path in paths:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if not dry_run:
                try:
                    path.unlink()
                except OSError:
                    continue
            files += 1
            freed += size
        if files:
            report[category] = {"files": files, "bytes": freed}

    # 1. Cached source media past its TTL, measured on last touch. The .vtt
    #    transcripts and info.json stay — they are small and are the record of
    #    what was fetched.
    if SOURCE_CACHE_DIR.exists():
        cutoff = now - SOURCE_CACHE_TTL_DAYS * 86400
        stale = []
        for path in SOURCE_CACHE_DIR.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in CACHED_MEDIA_EXTS:
                continue
            try:
                if max(path.stat().st_mtime, path.stat().st_atime) < cutoff:
                    stale.append(path)
            except OSError:
                continue
        _sweep("source_cache", stale)

    # 2. Sidecars whose audio no longer exists. Scoped to the two segment trees so
    #    it can never reach output/scripts/ or the .<show>_source_rotation.json
    #    rotation state that lives there.
    orphans = []
    for root in (TALK_DIR, BUMPERS_DIR):
        if not root.exists():
            continue
        for path in root.rglob("*.json"):
            name = path.name
            if name.endswith(".plays.json"):
                audio = path.with_name(name[: -len(".plays.json")])
                if not audio.exists():
                    orphans.append(path)
            else:
                stem = path.with_suffix("")
                if not any(stem.with_suffix(ext).exists() for ext in AUDIO_EXTS):
                    orphans.append(path)
    _sweep("orphan_sidecars", orphans)

    # 3. Script records past their TTL. `talk_*.json` only — the rotation-state
    #    dotfiles alongside them are live state, and there is a forked pair
    #    (.youtube-ai_ / .youtube_ai_), so do not assume one file per show.
    if SCRIPTS_DIR.exists() and SCRIPTS_TTL_DAYS > 0:
        cutoff = now - SCRIPTS_TTL_DAYS * 86400
        stale_scripts = []
        for path in SCRIPTS_DIR.glob("talk_*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    stale_scripts.append(path)
            except OSError:
                continue
        _sweep("scripts", stale_scripts)

    # 4. Job records past their TTL.
    if JOBS_DIR.exists():
        cutoff = now - JOBS_TTL_DAYS * 86400
        stale_jobs = []
        for path in JOBS_DIR.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    stale_jobs.append(path)
            except OSError:
                continue
        _sweep("jobs", stale_jobs)

    # 5. Empty segment directories belonging to shows that are no longer in the
    #    schedule. briefing_daily still had one months after it left shows.yaml.
    #    Two guards, because a directory is cheap and re-creating one is not:
    #    the show must be absent from the config, and the directory must be empty.
    if not dry_run:
        try:
            live_shows = set(_load_schedule().get("shows", {}))
        except Exception:
            live_shows = None  # config unreadable — leave everything alone
        removed_shows = 0
        if live_shows:
            for root in (TALK_DIR, BUMPERS_DIR):
                if not root.exists():
                    continue
                for path in root.iterdir():
                    if not path.is_dir() or path.name in live_shows:
                        continue
                    try:
                        path.rmdir()  # fails harmlessly unless the directory is empty
                        removed_shows += 1
                    except OSError:
                        continue
        if removed_shows:
            report["retired_show_dirs"] = {"files": removed_shows, "bytes": 0}

    # 6. Directories left empty by the sweep above.
    if SOURCE_CACHE_DIR.exists() and not dry_run:
        removed = 0
        for path in sorted(SOURCE_CACHE_DIR.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir():
                try:
                    path.rmdir()
                    removed += 1
                except OSError:
                    pass
        if removed:
            report["empty_dirs"] = {"files": removed, "bytes": 0}

    return report


def _format_janitor_report(report: dict) -> str:
    parts = [
        f"{cat} {vals['files']} file(s) / {vals['bytes'] / 1048576:.1f} MB"
        for cat, vals in report.items()
    ]
    total = sum(v["bytes"] for v in report.values())
    return f"{', '.join(parts)} — {total / 1048576:.1f} MB total"


def disk_usage_report() -> dict:
    """Free space and an output/ breakdown, for the admin disk gauge (task 1.3)."""
    import shutil as _shutil

    try:
        usage = _shutil.disk_usage(str(PROJECT_ROOT))
    except OSError:
        return {}

    breakdown = {}
    if OUTPUT_DIR.exists():
        for child in sorted(OUTPUT_DIR.iterdir()):
            if not child.is_dir():
                continue
            total = 0
            for path in child.rglob("*"):
                try:
                    if path.is_file():
                        total += path.stat().st_size
                except OSError:
                    continue
            breakdown[child.name] = total

    free_gb = usage.free / 1073741824
    return {
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "percent_used": round(usage.used / usage.total * 100, 1) if usage.total else 0,
        "output_bytes": sum(breakdown.values()),
        "output_breakdown": breakdown,
        # Under 10 GB the next few generation jobs are at risk; under 2 GB the
        # segment writer starts failing and Icecast drains the queue silently.
        "state": "critical" if free_gb < 2 else "warning" if free_gb < 10 else "ok",
    }


def _stagger_expiry_count(
    generated: list[datetime],
    now: datetime,
    *,
    max_days: int,
    target: int,
    minimum: int,
) -> int:
    """How many of the oldest segments to expire early to break up a same-day cluster.

    A show whose whole catalogue was generated on one day also expires on one day:
    listeners get the same batch for `max_days`, then all of it swaps at once.
    Dropping one day's slice early lets the normal top-up refill a slice per day,
    so generation and expiry spread across the window instead of pulsing.

    Returns 0 when there is nothing useful to do.
    """
    inventory = len(generated)
    if inventory < 2 or max_days < 2 or target <= minimum:
        return 0
    # Already spread across the window — leave it alone.
    if len({dt.date() for dt in generated}) >= max_days:
        return 0
    # Everything was made today. Expiring now would only be replaced by more of
    # today's date, so wait until the cluster is at least a day old.
    if (now - min(generated)).days < 1:
        return 0
    # Below the minimum the normal top-up already fires; no need to force it.
    if inventory <= minimum:
        return 0
    slice_size = max(1, (target + max_days - 1) // max_days)
    # Take at least a day's slice, and enough to reach the minimum so the top-up
    # runs on this same pass. Never empty the show.
    return min(max(slice_size, inventory - minimum), inventory - 1)


STAGGER_STATE_FILE = PROJECT_ROOT / "output" / ".stagger_last_run"


def _stagger_ran_today(today) -> bool:
    """Has the stagger pass already run today? Persisted, because the admin service
    restarts often and an in-memory flag would expire a fresh slice every restart."""
    try:
        return STAGGER_STATE_FILE.read_text().strip() == today.isoformat()
    except Exception:
        return False


def _record_stagger_run(today) -> None:
    try:
        STAGGER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STAGGER_STATE_FILE.write_text(today.isoformat())
    except Exception as e:
        print(f"[scheduler] Stagger: could not record run date: {e}")


def _stagger_segment_ages() -> int:
    """Expire a day's slice early for shows whose stock is bunched on one day."""
    try:
        data = _load_schedule()
    except Exception as e:
        print(f"[scheduler] Stagger: failed to load schedule: {e}")
        return 0
    now = _station_now()
    total = 0
    for show_id, show in (data.get("shows") or {}).items():
        talk_cfg = {**DEFAULT_TALK_CONFIG, **((show.get("generation") or {}).get("talk") or {})}
        if not talk_cfg["enabled"]:
            continue
        max_days = ((show.get("content_lifecycle") or {}).get("talk") or {}).get("max_days")
        if not max_days:
            continue
        items = _segment_times(show_id, now)
        n = _stagger_expiry_count(
            [t for t, _ in items],
            now,
            max_days=int(max_days),
            target=int(talk_cfg["target_inventory"]),
            minimum=int(talk_cfg["min_inventory"]),
        )
        if not n:
            continue
        for _, f in items[:n]:
            _delete_segment(f)
        total += n
        print(
            f"[scheduler] Stagger: expired {n} oldest segment(s) for {show_id} "
            f"to spread generation over {max_days} day(s)"
        )
    return total


def _newest_segment_time(show_dir: Path) -> datetime | None:
    """Return the most recent generation datetime from audio files in show_dir."""
    if not show_dir.exists():
        return None
    tz = _station_tz()
    best: datetime | None = None
    for f in show_dir.iterdir():
        if not f.is_file() or f.suffix.lower() not in AUDIO_EXTS:
            continue
        dt: datetime | None = None
        sidecar = f.with_suffix(".json")
        if sidecar.exists():
            try:
                import json as _json
                meta = _json.loads(sidecar.read_text())
                gen_at = meta.get("generated_at")
                if gen_at:
                    dt = datetime.fromisoformat(gen_at.replace("Z", "+00:00"))
            except Exception:
                pass
        if dt is None:
            mtime = f.stat().st_mtime
            dt = datetime.fromtimestamp(mtime, tz) if tz else datetime.fromtimestamp(mtime)
        # Normalise to station-aware datetime for comparison
        if dt.tzinfo is None and tz:
            dt = dt.replace(tzinfo=tz)
        elif dt.tzinfo is not None and tz:
            dt = dt.astimezone(tz)
        if best is None or dt > best:
            best = dt
    return best


def _seed_last_run_from_fs() -> None:
    """Seed last_run_per_show from the newest on-disk segments so restarts don't storm.

    Without this, every daily/weekly show fires immediately after a restart
    because last_run_per_show is empty and _cadence_ok returns True for all of them.
    """
    try:
        data = _load_schedule()
    except Exception:
        return
    for show_id in data.get("shows", {}):
        talk_time = _newest_segment_time(TALK_DIR / show_id)
        if talk_time:
            with state._lock:
                state.last_run_per_show.setdefault(show_id, {})["talk"] = talk_time
        music_time = _newest_segment_time(BUMPERS_DIR / show_id)
        if music_time:
            with state._lock:
                state.last_run_per_show.setdefault(show_id, {})["music"] = music_time


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
            # Skip if a generation job is already running for this show
            already_running = any(
                j.get("show_id") == show_id and j.get("type") == "talk"
                for j in state.active_jobs.values()
            )
            if already_running:
                continue

            inventory = _count_inventory(TALK_DIR, show_id)
            scarce = inventory <= ARCHIVE_SCARCITY_FLOOR

            backoff_kind, backoff_left = state.backoff_state(show_id, "talk")
            if backoff_kind:
                # A cold source stays quiet — until the show falls to the
                # scarcity floor, which is a new condition and the one case
                # where reaching into the archive is the right answer.
                if not (backoff_kind == "cold" and scarce):
                    continue
                state.add_log(show_id, "talk",
                    f"Inventory {inventory} at the scarcity floor — retrying a cold source "
                    f"{backoff_left // 60} min early, archive open", "warn")

            target = int(talk_cfg["target_inventory"])
            minimum = int(talk_cfg["min_inventory"])
            cadence = talk_cfg.get("cadence", "continuous")
            time_after = talk_cfg.get("time_after") or None

            if cadence == "continuous":
                # Top up once inventory reaches the minimum. This is `<=`, not `<`:
                # a show sitting exactly ON its minimum with nothing expiring can
                # never fall below it, and used to stall there forever.
                should_run = inventory <= minimum and inventory < target
                needed = target - inventory
            else:
                # Time-based (daily/weekly/…): cadence drives generation, not inventory.
                # Always produce at least 1; never exceed target.
                should_run = _cadence_ok(show_id, "talk", cadence, time_after)
                needed = max(1, target - inventory)

            if should_run and needed > 0:
                # 3.10: the archive is a reserve. It opens only at the scarcity
                # floor, and only for shows that allow it at all.
                allow_archive = scarce and bool(talk_cfg.get("archive_fallback", True))
                reserve = "" if allow_archive else ", archive held back"
                state.add_log(show_id, "talk",
                    f"Inventory {inventory}, cadence={cadence} → generating {needed}{reserve}")
                t = threading.Thread(
                    target=_run_talk_generation,
                    args=(show_id, needed, job_registry, env, _inventory_invalidator, None, allow_archive),
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
            cadence = music_cfg.get("cadence", "continuous")

            if cadence == "continuous":
                # `<=` for the same reason as talk: exactly-at-minimum is a stall.
                should_run = inventory <= minimum and inventory < target
                needed = target - inventory
            else:
                should_run = _cadence_ok(show_id, "music", cadence)
                needed = max(1, target - inventory)

            if should_run and needed > 0:
                bumper_style = show.get("bumper_style", "ambient")
                state.add_log(show_id, "music",
                    f"Bumper inventory {inventory}, cadence={cadence} → generating {needed}")
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
    _seed_last_run_from_fs()
    last_cleanup = 0.0
    last_stagger_day = None

    while state.running:
        state.last_check = _station_now()

        # Once per station-day, before the inventory check, so anything expired
        # early is topped back up on this same pass.
        today = state.last_check.date()
        if last_stagger_day != today and not _stagger_ran_today(today):
            last_stagger_day = today
            try:
                _stagger_segment_ages()
                _record_stagger_run(today)
            except Exception as e:
                print(f"[scheduler] Stagger error: {e}")

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

            try:
                report = _janitor_sweep()
                if report:
                    print(f"[scheduler] Janitor: {_format_janitor_report(report)}")
            except Exception as e:
                print(f"[scheduler] Janitor error: {e}")

            try:
                disk = disk_usage_report()
                if disk.get("state") in {"warning", "critical"}:
                    print(
                        f"[scheduler] DISK {disk['state'].upper()}: "
                        f"{disk['free_bytes'] / 1073741824:.1f} GB free "
                        f"({disk['percent_used']}% used)"
                    )
            except Exception as e:
                print(f"[scheduler] Disk check error: {e}")

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
