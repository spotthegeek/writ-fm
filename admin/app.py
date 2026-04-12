#!/usr/bin/env python3
"""
WRIT-FM Admin Interface

FastAPI-based admin web interface for managing shows, schedule,
content generation, and the segment library.

Port: 8080
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import scheduler as _sched

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
SCHEDULE_PATH = CONFIG_DIR / "schedule.yaml"
TALK_SEGMENTS_DIR = PROJECT_ROOT / "output" / "talk_segments"
BUMPERS_DIR = PROJECT_ROOT / "output" / "music_bumpers"
SCRIPTS_DIR = PROJECT_ROOT / "output" / "scripts"
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python3"
STREAMER_API = os.environ.get("WRIT_STREAMER_API", "http://localhost:8001")

AUDIO_EXTS = {".wav", ".mp3", ".flac"}
JOBS_DIR = PROJECT_ROOT / "output" / "jobs"

# ---------------------------------------------------------------------------
# Persistent job store
# ---------------------------------------------------------------------------

def _save_job(job: dict) -> None:
    """Write a completed/failed job to disk so it survives restarts."""
    try:
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        (JOBS_DIR / f"{job['id']}.json").write_text(json.dumps(job, indent=2))
    except Exception:
        pass


def _load_jobs() -> dict[str, dict]:
    """Load persisted jobs from disk, newest first."""
    jobs: dict[str, dict] = {}
    if not JOBS_DIR.exists():
        return jobs
    for f in sorted(JOBS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.suffix == ".json":
            try:
                job = json.loads(f.read_text())
                jobs[job["id"]] = job
            except Exception:
                pass
    return jobs


_jobs: dict[str, dict] = _load_jobs()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="WRIT-FM Admin", version="1.0")

@app.on_event("startup")
def _start_scheduler():
    check_interval = int(os.environ.get("WRIT_SCHEDULER_INTERVAL", "300"))
    _sched.start_scheduler(_jobs, check_interval=check_interval)
    print(f"[admin] Scheduler started (interval={check_interval}s)")

STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_schedule() -> dict:
    with open(SCHEDULE_PATH) as f:
        return yaml.safe_load(f)


def save_schedule(data: dict):
    with open(SCHEDULE_PATH, "w") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def get_hosts_from_persona() -> dict:
    """Read host definitions from persona.py."""
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "mac" / "content_generator"))
        import importlib
        import persona
        importlib.reload(persona)
        return {
            hid: {
                "id": hid,
                "name": h.get("name", hid),
                "tts_voice": h.get("tts_voice", "am_michael"),
                "topics": h.get("topics", []),
                "speaking_pace_wpm": h.get("speaking_pace_wpm", 130),
            }
            for hid, h in persona.HOSTS.items()
        }
    except Exception as e:
        return {}


def segment_inventory() -> dict[str, list[dict]]:
    """Return per-show segment inventory."""
    inv: dict[str, list[dict]] = {}
    if not TALK_SEGMENTS_DIR.exists():
        return inv
    for show_dir in sorted(TALK_SEGMENTS_DIR.iterdir()):
        if not show_dir.is_dir():
            continue
        files = []
        for f in sorted(show_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.is_file() and f.suffix.lower() in AUDIO_EXTS:
                plays_meta = _read_plays_meta(f)
                gen_meta = _read_gen_meta(f)
                files.append({
                    "name": f.name,
                    "show_id": show_dir.name,
                    "size_kb": round(f.stat().st_size / 1024),
                    "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "path": str(f.relative_to(PROJECT_ROOT)),
                    "play_count": plays_meta.get("play_count", 0),
                    "first_played": plays_meta.get("first_played_at", "")[:16],
                    "last_played": plays_meta.get("last_played_at", "")[:16],
                    "prompt": gen_meta.get("topic", ""),
                    "segment_type": gen_meta.get("type", ""),
                    "word_count": gen_meta.get("word_count", 0),
                    "generated_at": gen_meta.get("generated_at", ""),
                })
        inv[show_dir.name] = files
    return inv


def _read_plays_meta(f: Path) -> dict:
    sidecar = f.parent / (f.name + ".plays.json")
    if not sidecar.exists():
        return {}
    try:
        return json.loads(sidecar.read_text())
    except Exception:
        return {}


def _read_gen_meta(f: Path) -> dict:
    """Read generation metadata for an audio file.

    Priority:
    1. .json sidecar next to the audio file (new files)
    2. Matching entry in output/scripts/ by timestamp (legacy files)
    """
    sidecar = f.with_suffix(".json")
    if sidecar.exists():
        try:
            return json.loads(sidecar.read_text())
        except Exception:
            pass

    # Fallback: extract timestamp from filename and search output/scripts/
    # Filename pattern: {segment_type}_{topic_slug}_{YYYYMMDD_HHMMSS}.ext
    import re as _re
    m = _re.search(r'(\d{8}_\d{6})', f.stem)
    if m and SCRIPTS_DIR.exists():
        ts = m.group(1)
        for sf in SCRIPTS_DIR.glob(f"talk_*_{ts}.json"):
            try:
                return json.loads(sf.read_text())
            except Exception:
                pass

    return {}


def bumper_inventory() -> dict[str, list[dict]]:
    inv: dict[str, list[dict]] = {}
    if not BUMPERS_DIR.exists():
        return inv
    for show_dir in sorted(BUMPERS_DIR.iterdir()):
        if not show_dir.is_dir():
            continue
        files = []
        for f in sorted(show_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.is_file() and f.suffix.lower() in AUDIO_EXTS:
                plays_meta = _read_plays_meta(f)
                gen_meta = _read_gen_meta(f)
                files.append({
                    "name": f.name,
                    "show_id": show_dir.name,
                    "size_kb": round(f.stat().st_size / 1024),
                    "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "play_count": plays_meta.get("play_count", 0),
                    "first_played": plays_meta.get("first_played_at", "")[:16],
                    "last_played": plays_meta.get("last_played_at", "")[:16],
                    "prompt": gen_meta.get("caption", gen_meta.get("topic", "")),
                    "display_name": gen_meta.get("display_name", ""),
                    "generated_at": gen_meta.get("generated_at", ""),
                })
        inv[show_dir.name] = files
    return inv


# ---------------------------------------------------------------------------
# API: Status
# ---------------------------------------------------------------------------

@app.get("/api/status")
def get_status():
    import urllib.request, urllib.error
    streamer_ok = False
    now_playing = {}
    try:
        with urllib.request.urlopen(f"{STREAMER_API}/now-playing", timeout=2) as r:
            now_playing = json.loads(r.read())
            streamer_ok = True
    except Exception:
        pass

    icecast_ok = False
    try:
        with urllib.request.urlopen("http://localhost:8000/status-json.xsl", timeout=2) as r:
            icecast_ok = r.status == 200
    except Exception:
        pass

    segments = segment_inventory()
    total_segments = sum(len(v) for v in segments.values())

    bumpers = bumper_inventory()
    total_bumpers = sum(len(v) for v in bumpers.values())

    return {
        "icecast": icecast_ok,
        "streamer": streamer_ok,
        "now_playing": now_playing,
        "total_segments": total_segments,
        "total_bumpers": total_bumpers,
        "segments_per_show": {k: len(v) for k, v in segments.items()},
        "timestamp": datetime.now().isoformat(),
    }


# ---------------------------------------------------------------------------
# API: Shows
# ---------------------------------------------------------------------------

@app.get("/api/shows")
def get_shows():
    data = load_schedule()
    shows = data.get("shows", {})
    # Normalize: ensure each show has the extended fields
    result = {}
    for show_id, show in shows.items():
        result[show_id] = _normalize_show(show_id, show)
    return result


def _normalize_show(show_id: str, show: dict) -> dict:
    """Fill in defaults for optional/new fields."""
    s = dict(show)
    s["id"] = show_id
    # Normalize hosts to list format
    if "hosts" not in s:
        s["hosts"] = [{
            "id": s.get("host", "liminal_operator"),
            "role": "primary",
            "voice_kokoro": s.get("voices", {}).get("host", "am_michael"),
            "voice_minimax": "Deep_Voice_Man",
            "tts_backend": s.get("tts_backend", "kokoro"),
        }]
        if "voices" in s and "guest" in s["voices"]:
            s["hosts"].append({
                "id": "guest",
                "role": "guest",
                "voice_kokoro": s["voices"]["guest"],
                "voice_minimax": "Wise_Woman",
                "tts_backend": "kokoro",
            })
    if "research_topic" not in s:
        s["research_topic"] = ""
    if "research_sources" not in s:
        s["research_sources"] = []
    if "guests" not in s:
        s["guests"] = []
    return s


class ShowUpdate(BaseModel):
    name: str
    description: str
    hosts: list[dict]
    topic_focus: str
    research_topic: str = ""
    research_sources: list[dict] = []
    guests: list[dict] = []
    segment_types: list[str]
    bumper_style: str


@app.put("/api/shows/{show_id}")
def update_show(show_id: str, update: ShowUpdate):
    data = load_schedule()
    shows = data.get("shows", {})
    if show_id not in shows:
        raise HTTPException(404, f"Show '{show_id}' not found")

    show = dict(shows[show_id])
    show["name"] = update.name
    show["description"] = update.description
    show["topic_focus"] = update.topic_focus
    show["research_topic"] = update.research_topic
    show["research_sources"] = update.research_sources
    show["guests"] = update.guests
    show["segment_types"] = update.segment_types
    show["bumper_style"] = update.bumper_style
    show["hosts"] = update.hosts

    # Keep legacy fields for backward compat with streamer
    primary = next((h for h in update.hosts if h.get("role") == "primary"), update.hosts[0] if update.hosts else {})
    show["host"] = primary.get("id", "liminal_operator")
    show["tts_backend"] = primary.get("tts_backend", "kokoro")
    voices = {"host": primary.get("voice_kokoro", "am_michael")}
    for h in update.hosts:
        if h.get("role") in ("guest", "co-host", "secondary"):
            voices["guest"] = h.get("voice_kokoro", "af_bella")
    show["voices"] = voices

    shows[show_id] = show
    data["shows"] = shows
    save_schedule(data)
    return {"ok": True, "show_id": show_id}


@app.post("/api/shows")
def create_show(show_id: str, update: ShowUpdate):
    data = load_schedule()
    shows = data.get("shows", {})
    if show_id in shows:
        raise HTTPException(400, f"Show '{show_id}' already exists")

    show = {
        "name": update.name,
        "description": update.description,
        "topic_focus": update.topic_focus,
        "research_topic": update.research_topic,
        "research_sources": update.research_sources,
        "guests": update.guests,
        "segment_types": update.segment_types,
        "bumper_style": update.bumper_style,
        "hosts": update.hosts,
    }
    primary = next((h for h in update.hosts if h.get("role") == "primary"), update.hosts[0] if update.hosts else {})
    show["host"] = primary.get("id", "liminal_operator")
    show["tts_backend"] = primary.get("tts_backend", "kokoro")
    show["voices"] = {"host": primary.get("voice_kokoro", "am_michael")}

    shows[show_id] = show
    data["shows"] = shows
    save_schedule(data)
    return {"ok": True, "show_id": show_id}


@app.delete("/api/shows/{show_id}")
def delete_show(show_id: str):
    data = load_schedule()
    shows = data.get("shows", {})
    if show_id not in shows:
        raise HTTPException(404, f"Show '{show_id}' not found")
    del shows[show_id]
    data["shows"] = shows
    save_schedule(data)
    return {"ok": True}


# ---------------------------------------------------------------------------
# API: Schedule
# ---------------------------------------------------------------------------

@app.get("/api/schedule")
def get_schedule():
    data = load_schedule()
    return {
        "timezone": data.get("timezone", "local"),
        "base": data.get("schedule", {}).get("base", []),
        "overrides": data.get("schedule", {}).get("overrides", []),
    }


class ScheduleUpdate(BaseModel):
    timezone: str = "local"
    base: list[dict]
    overrides: list[dict] = []


@app.put("/api/schedule")
def update_schedule(update: ScheduleUpdate):
    data = load_schedule()
    data["timezone"] = update.timezone
    data["schedule"] = {"base": update.base, "overrides": update.overrides}
    save_schedule(data)
    return {"ok": True}


# ---------------------------------------------------------------------------
# API: Hosts
# ---------------------------------------------------------------------------

@app.get("/api/hosts")
def get_hosts():
    return get_hosts_from_persona()


# ---------------------------------------------------------------------------
# API: Library
# ---------------------------------------------------------------------------

@app.get("/api/library/segments")
def get_segments():
    return segment_inventory()


@app.get("/api/library/bumpers")
def get_bumpers():
    return bumper_inventory()


@app.delete("/api/library/segments/{show_id}/{filename}")
def delete_segment(show_id: str, filename: str):
    path = TALK_SEGMENTS_DIR / show_id / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    path.unlink()
    return {"ok": True}


@app.delete("/api/library/bumpers/{show_id}/{filename}")
def delete_bumper(show_id: str, filename: str):
    path = BUMPERS_DIR / show_id / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    path.unlink()
    return {"ok": True}


@app.get("/api/library/audio/{show_id}/{filename}")
def stream_audio(show_id: str, filename: str, type: str = "segment"):
    base = TALK_SEGMENTS_DIR if type == "segment" else BUMPERS_DIR
    path = base / show_id / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(str(path))


# ---------------------------------------------------------------------------
# API: Generation
# ---------------------------------------------------------------------------

SEGMENT_TYPES = [
    "show_intro", "show_outro", "station_id",
    "deep_dive", "news_analysis", "interview", "panel",
    "story", "listener_mailbag", "music_essay",
]

TOPIC_FOCUSES = [
    "philosophy", "music_history", "current_events", "culture",
    "soul_music", "night_philosophy", "listeners",
]

KOKORO_VOICES = [
    "am_michael", "am_onyx", "am_fenrir", "am_echo", "am_eric",
    "am_liam", "am_adam", "am_puck",
    "af_heart", "af_bella", "af_sky", "af_nova", "af_sarah",
    "af_nicole", "af_jessica", "af_alloy",
    "bm_daniel", "bm_george", "bm_fable", "bm_lewis",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
]

MINIMAX_VOICES = [
    "Deep_Voice_Man", "Wise_Woman", "Calm_Woman", "Friendly_Person",
    "Confident_Man", "Elegant_Man", "Patient_Man", "Casual_Guy",
    "Inspirational_Girl", "Lively_Girl",
]


@app.get("/api/generate/options")
def get_generate_options():
    return {
        "segment_types": SEGMENT_TYPES,
        "topic_focuses": TOPIC_FOCUSES,
        "kokoro_voices": KOKORO_VOICES,
        "minimax_voices": MINIMAX_VOICES,
        "shows": list(load_schedule().get("shows", {}).keys()),
    }


class GenerateRequest(BaseModel):
    show_id: str
    content_type: str = "talk"  # "talk" or "music"
    segment_type: str = "random"
    topic: str = ""
    count: int = 1
    # Override TTS for this run
    tts_backend: str = ""   # "kokoro", "minimax", or "" to use show default
    host_voice: str = ""    # override primary host voice
    # Guest for this run
    guest_name: str = ""
    guest_voice_kokoro: str = "af_bella"
    guest_voice_minimax: str = "Wise_Woman"
    guest_tts_backend: str = "kokoro"


@app.post("/api/generate")
def start_generation(req: GenerateRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {
        "id": job_id,
        "status": "pending",
        "show_id": req.show_id,
        "content_type": req.content_type,
        "segment_type": req.segment_type,
        "topic": req.topic,
        "log": [],
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
    }
    background_tasks.add_task(_run_generation_job, job_id, req)
    return {"job_id": job_id, "status": "started"}


@app.get("/api/generate/jobs")
def list_jobs():
    return list(reversed(list(_jobs.values())))


@app.get("/api/generate/jobs/{job_id}")
def get_job(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(404, "Job not found")
    return _jobs[job_id]


def _log_job(job_id: str, msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    _jobs[job_id]["log"].append(line)
    print(line)  # also to systemd journal


def _run_generation_job(job_id: str, req: GenerateRequest):
    """Run a generation job in a background thread."""
    _jobs[job_id]["status"] = "running"
    _log_job(job_id, f"Starting generation: show={req.show_id} type={req.segment_type} count={req.count}")

    try:
        data = load_schedule()
        show = data.get("shows", {}).get(req.show_id)
        if not show:
            _log_job(job_id, f"ERROR: Show '{req.show_id}' not found")
            _jobs[job_id]["status"] = "failed"
            return

        # Build env for the generation subprocess
        env = dict(os.environ)
        env["OLLAMA_URL"] = env.get("OLLAMA_URL", "http://ollama.area4.net:11434")
        env["OLLAMA_MODEL"] = env.get("OLLAMA_MODEL", "gemma3:12b")
        env["MINIMAX_API_KEY"] = env.get("MINIMAX_API_KEY", "")

        # Determine TTS backend
        tts_backend = req.tts_backend or show.get("tts_backend", "kokoro")

        # Build command
        if req.content_type == "music":
            gen_script = PROJECT_ROOT / "mac" / "content_generator" / "music_bumper_generator.py"
            if not gen_script.exists():
                _log_job(job_id, "ERROR: music_bumper_generator.py not found")
                _jobs[job_id]["status"] = "failed"
                return
            cmd = [str(VENV_PYTHON), str(gen_script),
                   "--show", req.show_id,
                   "--count", str(req.count)]
            if req.topic:  # reuse topic field as custom caption/style prompt
                cmd += ["--caption", req.topic]
            if req.tts_backend == "vocal":  # reuse tts_backend field as vocal flag
                cmd += ["--vocal"]
            _log_job(job_id, f"Generating {req.count} music bumper(s) for {req.show_id}"
                     + (f" — custom: {req.topic[:60]}" if req.topic else " — random from pool"))
        else:
            gen_script = PROJECT_ROOT / "mac" / "content_generator" / "talk_generator.py"
            cmd = [str(VENV_PYTHON), str(gen_script),
                   "--show", req.show_id,
                   "--count", str(req.count)]

            if req.segment_type and req.segment_type != "random":
                cmd += ["--type", req.segment_type]
            if req.topic:
                cmd += ["--topic", req.topic]

            # Pass TTS backend via env
            env["WRIT_TTS_BACKEND"] = tts_backend
            if req.host_voice:
                env["WRIT_HOST_VOICE"] = req.host_voice
            if req.guest_name:
                env["WRIT_GUEST_NAME"] = req.guest_name
                env["WRIT_GUEST_VOICE_KOKORO"] = req.guest_voice_kokoro
                env["WRIT_GUEST_VOICE_MINIMAX"] = req.guest_voice_minimax
                env["WRIT_GUEST_TTS_BACKEND"] = req.guest_tts_backend

            _log_job(job_id, f"TTS backend: {tts_backend}")

        _log_job(job_id, f"Running: {' '.join(cmd[-6:])}")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            cwd=str(PROJECT_ROOT / "mac" / "content_generator"),
        )

        for line in proc.stdout:
            line = line.rstrip()
            if line:
                _log_job(job_id, line)

        proc.wait()
        if proc.returncode == 0:
            _jobs[job_id]["status"] = "completed"
            _log_job(job_id, f"Generation complete.")
        else:
            _jobs[job_id]["status"] = "failed"
            _log_job(job_id, f"Generation failed (exit {proc.returncode})")

    except Exception as e:
        _log_job(job_id, f"ERROR: {e}")
        _jobs[job_id]["status"] = "failed"

    _jobs[job_id]["completed_at"] = datetime.now().isoformat()
    _save_job(_jobs[job_id])


# ---------------------------------------------------------------------------
# API: Scheduler
# ---------------------------------------------------------------------------

@app.get("/api/scheduler/status")
def get_scheduler_status():
    inv_talk = {}
    inv_music = {}
    if TALK_SEGMENTS_DIR.exists():
        for d in TALK_SEGMENTS_DIR.iterdir():
            if d.is_dir():
                inv_talk[d.name] = sum(1 for f in d.iterdir()
                                       if f.is_file() and f.suffix.lower() in AUDIO_EXTS)
    if BUMPERS_DIR.exists():
        for d in BUMPERS_DIR.iterdir():
            if d.is_dir():
                inv_music[d.name] = sum(1 for f in d.iterdir()
                                        if f.is_file() and f.suffix.lower() in AUDIO_EXTS)

    data = load_schedule()
    shows = data.get("shows", {})
    show_status = {}
    for show_id, show in shows.items():
        gen = show.get("generation", {})
        talk_cfg = {**_sched.DEFAULT_TALK_CONFIG, **gen.get("talk", {})}
        music_cfg = {**_sched.DEFAULT_MUSIC_CONFIG, **gen.get("music", {})}
        last = _sched.state.last_run_per_show.get(show_id, {})
        show_status[show_id] = {
            "talk": {
                **talk_cfg,
                "inventory": inv_talk.get(show_id, 0),
                "last_run": last.get("talk", _sched.state.last_run(show_id, "talk")) and
                            _sched.state.last_run(show_id, "talk").isoformat(),
            },
            "music": {
                **music_cfg,
                "inventory": inv_music.get(show_id, 0),
                "last_run": _sched.state.last_run(show_id, "music") and
                            _sched.state.last_run(show_id, "music").isoformat(),
            },
        }

    return {
        **_sched.state.snapshot(),
        "shows": show_status,
        "active_jobs": list(_sched.state.active_jobs.values()),
    }


@app.get("/api/scheduler/log")
def get_scheduler_log(limit: int = 50):
    return _sched.state.get_log(limit)


@app.get("/api/activity/log")
def get_activity_log(limit: int = 100):
    """Combined activity log: manual jobs + scheduler activity + scripts history, newest first."""
    entries = []

    # 1. Manual generation jobs (persisted across restarts)
    for job in _jobs.values():
        ts = job.get("completed_at") or job.get("created_at", "")
        ct = job.get("content_type", "talk")
        seg_type = job.get("segment_type", "") or ""
        entries.append({
            "ts": ts[:19].replace("T", " "),
            "show_id": job.get("show_id", ""),
            "type": ct,
            "msg": f"{'Music bumper' if ct=='music' else seg_type or 'talk'} — {job.get('status','')}",
            "level": "error" if job.get("status") == "failed" else "info",
            "source": "manual",
        })

    # 2. Scheduler activity (in-memory, current session only)
    for entry in _sched.state.get_log(limit):
        entries.append({**entry, "source": "scheduler"})

    # 3. Historical segments from output/scripts/ (fills gap before job persistence was added)
    persisted_ts = {e["ts"] for e in entries if e["source"] == "manual"}
    if SCRIPTS_DIR.exists():
        for sf in sorted(SCRIPTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if sf.suffix != ".json":
                continue
            try:
                m = json.loads(sf.read_text())
                ts = m.get("generated_at", "")[:19].replace("T", " ")
                if ts and ts not in persisted_ts:
                    entries.append({
                        "ts": ts,
                        "show_id": m.get("show_id", ""),
                        "type": "talk",
                        "msg": f"{m.get('type','')} — {m.get('word_count',0)}w — {m.get('topic','')[:60]}",
                        "level": "info",
                        "source": "history",
                    })
            except Exception:
                pass

    # Sort newest first
    entries.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return entries[:limit]


@app.post("/api/generate/expand-topic")
def expand_topic(body: dict):
    """Use the LLM to expand a short topic hint into a rich generation prompt."""
    hint = body.get("hint", "").strip()
    show_id = body.get("show_id", "")
    if not hint:
        raise HTTPException(400, "hint required")

    show = load_schedule().get("shows", {}).get(show_id, {})
    show_name = show.get("name", "a radio show")
    topic_focus = show.get("topic_focus", "")

    prompt = (
        f"You are a radio producer for '{show_name}', a show focused on {topic_focus}. "
        f"A presenter gave you this rough topic note: \"{hint}\"\n\n"
        f"Rewrite it as a single rich, specific, evocative topic prompt (2-4 sentences) "
        f"that a writer could use to immediately start scripting a compelling radio segment. "
        f"Include angles, tensions, specific examples or names if helpful. "
        f"Output ONLY the expanded topic prompt — no preamble, no quotes, no commentary."
    )

    try:
        ollama_url = os.environ.get("OLLAMA_URL", "http://ollama.area4.net:11434")
        ollama_model = os.environ.get("OLLAMA_MODEL", "gemma3:12b")
        import urllib.request as _ur
        payload = json.dumps({"model": ollama_model, "prompt": prompt, "stream": False}).encode()
        req = _ur.Request(f"{ollama_url}/api/generate", data=payload,
                          headers={"Content-Type": "application/json"}, method="POST")
        with _ur.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
        expanded = data.get("response", "").strip()
        if not expanded:
            raise ValueError("empty response")
        return {"expanded": expanded}
    except Exception as e:
        raise HTTPException(500, f"LLM expansion failed: {e}")


@app.post("/api/scheduler/trigger/{show_id}/{content_type}")
def trigger_generation(show_id: str, content_type: str):
    if content_type not in ("talk", "music"):
        raise HTTPException(400, "content_type must be 'talk' or 'music'")
    msg = _sched.trigger_now(show_id, content_type, _jobs)
    return {"ok": True, "message": msg}


class GenerationConfig(BaseModel):
    talk: dict = {}
    music: dict = {}


@app.put("/api/scheduler/config/{show_id}")
def update_generation_config(show_id: str, config: GenerationConfig):
    data = load_schedule()
    shows = data.get("shows", {})
    if show_id not in shows:
        raise HTTPException(404, f"Show '{show_id}' not found")
    shows[show_id]["generation"] = {
        "talk": config.talk,
        "music": config.music,
    }
    data["shows"] = shows
    save_schedule(data)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Serve admin UI
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def admin_ui():
    html_path = Path(__file__).parent / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return HTMLResponse("<h1>WRIT-FM Admin</h1><p>index.html not found.</p>")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("WRIT_ADMIN_PORT", "8080"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
