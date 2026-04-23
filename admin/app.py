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
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import scheduler as _sched
from mac.schedule import (
    ScheduleError as StationScheduleError,
    default_playback_sequence_for_show_type,
    load_schedule as validate_station_schedule,
    merge_playback_sequence,
    normalize_playback_sequence,
    playback_sequence_overrides,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
CONFIG_DIR = PROJECT_ROOT / "config"
SCHEDULE_PATH = CONFIG_DIR / "schedule.yaml"
HOSTS_PATH = CONFIG_DIR / "hosts.yaml"
SEGMENT_TYPES_PATH = CONFIG_DIR / "segment_types.yaml"
SHOW_TAXONOMY_PATH = CONFIG_DIR / "show_taxonomy.yaml"
TALK_SEGMENTS_DIR = PROJECT_ROOT / "output" / "talk_segments"
BUMPERS_DIR = PROJECT_ROOT / "output" / "music_bumpers"
SCRIPTS_DIR = PROJECT_ROOT / "output" / "scripts"
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python3"
STREAMER_API = os.environ.get("WRIT_STREAMER_API", "http://localhost:8001")
STATION_SERVICE = os.environ.get("WRIT_STATION_SERVICE", "writ-fm.service")
LEGACY_STREAMER_SERVICE = os.environ.get("WRIT_LEGACY_STREAMER_SERVICE", "writ-fm-streamer.service")

AUDIO_EXTS = {".wav", ".mp3", ".flac"}
JOBS_DIR = PROJECT_ROOT / "output" / "jobs"
INVENTORY_CACHE_TTL = float(os.environ.get("WRIT_INVENTORY_CACHE_TTL", "300"))
_inventory_cache_lock = threading.Lock()
_inventory_cache: dict[str, dict[str, Any]] = {
    "segments": {"ts": 0.0, "value": {}},
    "bumpers": {"ts": 0.0, "value": {}},
}

from mac.voice_samples import (
    KOKORO_VOICES,
    MINIMAX_VOICES,
    ensure_voice_sample,
    ensure_voice_samples,
    sample_media_type,
    sample_path,
    voice_catalog,
)

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
app = FastAPI(title="Station Admin", version="1.0")

@app.on_event("startup")
def _start_scheduler():
    check_interval = int(os.environ.get("WRIT_SCHEDULER_INTERVAL", "300"))
    _sched.start_scheduler(_jobs, check_interval=check_interval, cache_invalidator=_invalidate_inventory_cache)
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


def _validate_schedule_config(data: dict) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=True) as tmp:
        yaml.dump(data, tmp, allow_unicode=True, default_flow_style=False, sort_keys=False)
        tmp.flush()
        validate_station_schedule(Path(tmp.name))


def _validate_timezone_name(timezone_name: str) -> str:
    tz = (timezone_name or "local").strip() or "local"
    if tz not in {"local", "system"}:
        try:
            ZoneInfo(tz)
        except ZoneInfoNotFoundError as exc:
            raise HTTPException(400, f"Unknown timezone: {tz!r}") from exc
    return tz


def _station_tz():
    try:
        schedule = load_schedule()
        tz_name = _validate_timezone_name(schedule.get("timezone", "local"))
        if tz_name in {"local", "system"}:
            return None
        return ZoneInfo(tz_name)
    except Exception:
        return None


def _station_now() -> datetime:
    tz = _station_tz()
    return datetime.now(tz) if tz else datetime.now()


def _station_iso_now() -> str:
    return _station_now().isoformat()


def _station_from_timestamp(ts: float) -> datetime:
    tz = _station_tz()
    return datetime.fromtimestamp(ts, tz) if tz else datetime.fromtimestamp(ts)


def load_hosts_config() -> dict:
    """Load host definitions from config/hosts.yaml."""
    if not HOSTS_PATH.exists():
        return {"hosts": {}}
    with open(HOSTS_PATH) as f:
        return yaml.safe_load(f) or {"hosts": {}}


def save_hosts_config(data: dict):
    with open(HOSTS_PATH, "w") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def load_segment_types_config() -> dict:
    """Load managed segment type definitions from config/segment_types.yaml."""
    if not SEGMENT_TYPES_PATH.exists():
        return {"segment_types": {}}
    with open(SEGMENT_TYPES_PATH) as f:
        return yaml.safe_load(f) or {"segment_types": {}}


def save_segment_types_config(data: dict):
    with open(SEGMENT_TYPES_PATH, "w") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def load_show_taxonomy() -> dict:
    if not SHOW_TAXONOMY_PATH.exists():
        return {
            "show_types": {},
            "topic_focuses": [],
            "bumper_styles": [],
            "source_types": [],
        }
    with open(SHOW_TAXONOMY_PATH) as f:
        return yaml.safe_load(f) or {
            "show_types": {},
            "topic_focuses": [],
            "bumper_styles": [],
            "source_types": [],
        }


def save_show_taxonomy(data: dict):
    with open(SHOW_TAXONOMY_PATH, "w") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _normalise_taxonomy_items(values: list[str] | None) -> list[str]:
    seen = set()
    out = []
    for value in values or []:
        item = str(value).strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _hosts_yaml_to_api(hid: str, h: dict) -> dict:
    """Normalise a raw hosts.yaml entry into the API response shape."""
    return {
        "id": hid,
        "name": h.get("name", hid),
        "bio": h.get("identity", ""),
        "voice_style": h.get("voice_style", ""),
        "philosophy": h.get("philosophy", ""),
        "anti_patterns": h.get("anti_patterns", ""),
        "tts_voice": h.get("tts_voice", "am_michael"),
        "voice_minimax": h.get("voice_minimax", "Deep_Voice_Man"),
        "voice_google": h.get("voice_google", "Kore"),
        "tts_backend": h.get("tts_backend", "kokoro"),
        "topics": h.get("topics", []),
        "speaking_pace_wpm": h.get("speaking_pace_wpm", 130),
        "speaking_pace_wpm_kokoro": h.get("speaking_pace_wpm_kokoro", h.get("speaking_pace_wpm", 130)),
        "speaking_pace_wpm_minimax": h.get("speaking_pace_wpm_minimax", h.get("speaking_pace_wpm", 130)),
        "speaking_pace_wpm_google": h.get("speaking_pace_wpm_google", h.get("speaking_pace_wpm", 130)),
    }


def get_hosts_from_persona() -> dict[str, dict]:
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
                "voice_google": h.get("voice_google", "Kore"),
                "topics": h.get("topics", []),
                "speaking_pace_wpm": h.get("speaking_pace_wpm", 130),
                "speaking_pace_wpm_kokoro": h.get("speaking_pace_wpm_kokoro", h.get("speaking_pace_wpm", 130)),
                "speaking_pace_wpm_minimax": h.get("speaking_pace_wpm_minimax", h.get("speaking_pace_wpm", 130)),
                "speaking_pace_wpm_google": h.get("speaking_pace_wpm_google", h.get("speaking_pace_wpm", 130)),
        }
            for hid, h in persona.HOSTS.items()
        }
    except Exception as e:
        return {}


def get_all_hosts() -> dict[str, dict]:
    """Return the merged host roster with YAML overrides taking precedence."""
    hosts = get_hosts_from_persona()
    yaml_hosts = load_hosts_config().get("hosts", {})
    for hid, host in yaml_hosts.items():
        hosts[hid] = _hosts_yaml_to_api(hid, host)
    return hosts


def _default_host_assignment(host_id: str, role: str = "primary") -> dict:
    roster = get_all_hosts()
    host = roster.get(host_id, {})
    return {
        "id": host_id,
        "role": role,
        "tts_backend": host.get("tts_backend", "kokoro"),
        "voice_kokoro": host.get("tts_voice", "am_michael"),
        "voice_minimax": host.get("voice_minimax", "Deep_Voice_Man"),
        "voice_google": host.get("voice_google", "Kore"),
    }


DEFAULT_SEGMENT_TYPES = {
    "show_intro": {
        "name": "Show Intro",
        "description": "Short opening that sets the mood and welcomes listeners.",
        "word_count_min": 80,
        "word_count_max": 150,
        "multi_voice": False,
        "prompt_template": (
            "Write an 80-150 word opening for the show.\n"
            "Welcome listeners. Set the mood. Hint at what's ahead without being specific.\n"
            "Ground the listener in time and space - what hour is it, what kind of night.\n"
            "Output ONLY the spoken text."
        ),
    },
    "show_outro": {
        "name": "Show Outro",
        "description": "Short closing that leaves the listener with a final thought.",
        "word_count_min": 60,
        "word_count_max": 120,
        "multi_voice": False,
        "prompt_template": (
            "Write a 60-120 word show closing.\n"
            "Thank the listener for staying. Acknowledge the time spent together.\n"
            "Hint at what's next on the station. Leave them with something to carry.\n"
            "Output ONLY the spoken text."
        ),
    },
    "station_id": {
        "name": "Station ID",
        "description": "Very short station identification between longer segments.",
        "word_count_min": 15,
        "word_count_max": 30,
        "multi_voice": False,
        "prompt_template": (
            "Write a short, direct station ID for {show_name} on {station_name}.\n"
            "Use one sentence. Be plain and on-air, not poetic.\n"
            "Mention the show name and station name if it fits naturally.\n"
            "Do not invent imagery or a backstory.\n"
            "Output ONLY the spoken text. No quotes, headers, or explanations."
        ),
    },
    "deep_dive": {
        "name": "Deep Dive",
        "description": "Long-form reflective exploration of a single topic.",
        "word_count_min": 1500,
        "word_count_max": 2500,
        "multi_voice": False,
        "prompt_template": (
            "Write an extended exploration of this topic. Go deep.\n"
            "Build your central idea through stories, examples, tangents.\n"
            "Let one thought lead naturally to another. Circle back to earlier threads.\n"
            "Include specific details: years, names, places when relevant.\n"
            "Structure: open with a hook, develop through 3-4 connected ideas, land somewhere unexpected.\n"
            "Use [pause] for natural rhythm. Output ONLY the spoken words."
        ),
    },
    "news_analysis": {
        "name": "News Analysis",
        "description": "Late-night analysis built from current headlines.",
        "word_count_min": 1500,
        "word_count_max": 2000,
        "multi_voice": False,
        "prompt_template": (
            "Analyze these headlines through a late-night lens.\n"
            "Don't just report - interpret. What patterns do you see? What's being missed?\n"
            "Connect current events to deeper themes. Ask the questions daytime anchors don't.\n"
            "Be thoughtful, not reactive. Skeptical but not cynical.\n\n"
            "HEADLINES:\n"
            "{headlines}\n\n"
            "Use [pause] for natural rhythm. Output ONLY the spoken words."
        ),
    },
    "interview": {
        "name": "Interview",
        "description": "Primary host interviews a guest or recurring character.",
        "word_count_min": 2000,
        "word_count_max": 3000,
        "multi_voice": True,
        "prompt_template": (
            "Write a simulated interview where {primary_host_name} talks with {guest_name}.\n"
            "Format with HOST: and GUEST: markers on separate lines.\n"
            "The guest is a fictional/composite character, not a real living person being impersonated.\n"
            "The conversation should feel natural - interruptions, tangents, moments of surprise.\n"
            "Build to genuine insight or revelation.\n"
            "Use [pause] for natural rhythm. Output ONLY the spoken dialogue."
        ),
    },
    "panel": {
        "name": "Panel",
        "description": "Two voices explore a topic from different angles.",
        "word_count_min": 2000,
        "word_count_max": 3000,
        "multi_voice": True,
        "prompt_template": (
            "Write a discussion between {primary_host_name} and {secondary_host_name} on this topic.\n"
            "Format with HOST_A: and HOST_B: markers on separate lines.\n"
            "They have different perspectives but mutual respect.\n"
            "The conversation should build - start with disagreement, find nuance, reach unexpected common ground.\n"
            "Include moments of genuine surprise and humor.\n"
            "Use [pause] for natural rhythm. Output ONLY the spoken dialogue."
        ),
    },
    "story": {
        "name": "Story",
        "description": "Narrative storytelling segment.",
        "word_count_min": 1500,
        "word_count_max": 2500,
        "multi_voice": False,
        "prompt_template": (
            "Tell a story. It can be true, apocryphal, or mythological - but tell it like it happened.\n"
            "Good stories have specific details: the color of the room, the year, the weather.\n"
            "Build tension. Let the listener wonder where this is going.\n"
            "The ending should reframe everything that came before.\n"
            "Use [pause] for dramatic effect. Output ONLY the spoken words."
        ),
    },
    "reddit_storytelling": {
        "name": "Reddit Storytelling",
        "description": "Read a Reddit post as a story, with light performance cues.",
        "word_count_min": 1200,
        "word_count_max": 2200,
        "multi_voice": False,
        "prompt_template": (
            "Read the Reddit post as a story, not a summary.\n"
            "Stay close to the original wording and arc. Do not add commentary or analysis.\n"
            "Use light performance cues sparingly where they fit the story: [pause], [sigh], [laugh], [chuckle].\n"
            "If the post is already a story, preserve its pacing and tone.\n"
            "Do not discuss comments, external links, or your own reaction.\n"
            "Output ONLY the spoken words."
        ),
    },
    "reddit_post": {
        "name": "Reddit Post",
        "description": "Radio retelling and discussion of a Reddit thread, including comments.",
        "word_count_min": 1400,
        "word_count_max": 2200,
        "multi_voice": False,
        "prompt_template": (
            "Turn this Reddit thread into a compelling on-air segment.\n"
            "Open by grounding the listener in the subreddit and what kind of post this is.\n"
            "Retell the original post clearly and vividly in radio-friendly language.\n"
            "Bring in a handful of revealing, funny, skeptical, or emotionally resonant comments.\n"
            "If the post links to outside material, weave in the useful parts without sounding like you're reading a webpage.\n"
            "Distinguish between the original post, the community reaction, and the host's own interpretation.\n"
            "Output ONLY the spoken words."
        ),
    },
    "youtube": {
        "name": "YouTube",
        "description": "Radio segment built from a YouTube video's audio, captions, and metadata.",
        "word_count_min": 1400,
        "word_count_max": 2400,
        "multi_voice": False,
        "prompt_template": (
            "Turn this YouTube video into a compelling on-air segment.\n"
            "Ground the listener in the channel, the title, and what kind of video this is.\n"
            "Use the transcript, audio-derived notes, and metadata as your primary source material.\n"
            "Summarize the key beats, arguments, or story clearly and vividly.\n"
            "If there is no usable transcript, work from the title, description, chapters, and metadata.\n"
            "Keep the narration radio-friendly. Output ONLY the spoken words."
        ),
    },
    "listener_mailbag": {
        "name": "Listener Mailbag",
        "description": "Invented listener letters and thoughtful responses.",
        "word_count_min": 1500,
        "word_count_max": 2000,
        "multi_voice": False,
        "prompt_template": (
            "Write a segment responding to invented listener messages.\n"
            "Create 2-3 messages from listeners (with first names and cities).\n"
            "Each message should touch on something real - a memory, a question, a feeling.\n"
            "Respond to each with genuine warmth and thoughtfulness.\n"
            "Format: read the message, then respond. Natural transitions between letters.\n"
            "Use [pause] for natural rhythm. Output ONLY the spoken words."
        ),
    },
    "music_essay": {
        "name": "Music Essay",
        "description": "Long-form essay about a song, artist, scene, or musical idea.",
        "word_count_min": 1500,
        "word_count_max": 2500,
        "multi_voice": False,
        "prompt_template": (
            "Write an extended essay about music.\n"
            "This is not a review. It's a love letter, an excavation, a meditation.\n"
            "Pick a specific angle: a single song, a studio, a year, a collaboration, a genre's birth.\n"
            "Use vivid, sensory language. Make the listener hear what you're describing.\n"
            "Be specific with details but universal with feeling.\n"
            "Use [pause] for natural rhythm. Output ONLY the spoken words."
        ),
    },
}


def get_segment_type_definitions() -> dict[str, dict]:
    data = load_segment_types_config().get("segment_types", {})
    merged = {key: dict(value) for key, value in DEFAULT_SEGMENT_TYPES.items()}
    for sid, config in data.items():
        if sid in merged:
            merged[sid] = {**merged[sid], **(config or {})}
        else:
            merged[sid] = config or {}
    return merged


def _segment_type_to_api(segment_type_id: str, config: dict) -> dict:
    return {
        "id": segment_type_id,
        "name": config.get("name", segment_type_id.replace("_", " ").title()),
        "description": config.get("description", ""),
        "word_count_min": config.get("word_count_min", 1500),
        "word_count_max": config.get("word_count_max", 2500),
        "multi_voice": bool(config.get("multi_voice", False)),
        "prompt_template": config.get("prompt_template", ""),
    }


def _show_type_to_api(show_type_id: str, config: dict) -> dict:
    sequence_defaults = config.get("playback_sequence_defaults")
    if not isinstance(sequence_defaults, dict):
        sequence_defaults = default_playback_sequence_for_show_type(show_type_id)
    return {
        "id": show_type_id,
        "name": config.get("name", show_type_id.replace("_", " ").title()),
        "description": config.get("description", ""),
        "uses_topic_focus": bool(config.get("uses_topic_focus", False)),
        "uses_source_rules": bool(config.get("uses_source_rules", False)),
        "uses_bumper_style": bool(config.get("uses_bumper_style", True)),
        "default_segment_types": list(config.get("default_segment_types", [])),
        "playback_sequence_defaults": normalize_playback_sequence(sequence_defaults, show_type_id),
    }


def get_show_type_definitions() -> dict[str, dict]:
    taxonomy = load_show_taxonomy().get("show_types", {})
    sequence_defaults = default_playback_sequence_for_show_type("research")
    defaults = {
        "research": {
            "name": "Research Based",
            "description": "Curated editorial shows driven by topic focus and prompt research.",
            "uses_topic_focus": True,
            "uses_source_rules": True,
            "uses_bumper_style": True,
            "default_segment_types": ["deep_dive", "interview", "story"],
            "playback_sequence_defaults": dict(sequence_defaults),
        },
        "hybrid": {
            "name": "Hybrid",
            "description": "Research-led shows that can also ingest external source rules.",
            "uses_topic_focus": True,
            "uses_source_rules": True,
            "uses_bumper_style": True,
            "default_segment_types": ["deep_dive", "interview", "panel", "reddit_post", "youtube"],
            "playback_sequence_defaults": dict(sequence_defaults),
        },
        "content_ingest": {
            "name": "Content Ingest",
            "description": "Feed-driven shows built from Reddit, YouTube, and web sources.",
            "uses_topic_focus": False,
            "uses_source_rules": True,
            "uses_bumper_style": True,
            "default_segment_types": ["reddit_post", "reddit_storytelling", "youtube"],
            "playback_sequence_defaults": dict(sequence_defaults),
        },
        "music_first": {
            "name": "Music First",
            "description": "Mostly music bumpers and IDs, with short editorial inserts.",
            "uses_topic_focus": False,
            "uses_source_rules": False,
            "uses_bumper_style": True,
            "default_segment_types": ["station_id", "show_intro", "show_outro"],
            "playback_sequence_defaults": dict(sequence_defaults),
        },
        "live_community": {
            "name": "Live / Community",
            "description": "Listener-facing and conversational shows with lighter automation.",
            "uses_topic_focus": False,
            "uses_source_rules": True,
            "uses_bumper_style": True,
            "default_segment_types": ["listener_mailbag", "panel", "interview"],
            "playback_sequence_defaults": dict(sequence_defaults),
        },
        "news_current_events": {
            "name": "News / Current Events",
            "description": "Source-driven current events shows built from the latest feeds and posts.",
            "uses_topic_focus": False,
            "uses_source_rules": True,
            "uses_bumper_style": True,
            "default_segment_types": ["news_analysis", "deep_dive", "panel"],
            "playback_sequence_defaults": dict(sequence_defaults),
        },
        "listener_driven": {
            "name": "Listener Driven",
            "description": "Shows built from listener prompts, calls, mailbag letters, and feedback.",
            "uses_topic_focus": False,
            "uses_source_rules": True,
            "uses_bumper_style": True,
            "default_segment_types": ["listener_mailbag", "listener_response", "interview"],
            "playback_sequence_defaults": dict(sequence_defaults),
        },
    }
    merged = dict(defaults)
    for sid, cfg in taxonomy.items():
        merged[sid] = {**merged.get(sid, {}), **(cfg or {})}
    return merged


def _default_segment_types_for_show_type(show_type: str) -> list[str]:
    cfg = get_show_type_definitions().get(show_type, {})
    defaults = cfg.get("default_segment_types")
    if isinstance(defaults, list) and defaults:
        return [str(s).strip() for s in defaults if str(s).strip()]
    return ["deep_dive"]


def _normalize_source_rule(rule: dict | None) -> dict:
    rule = rule or {}
    rule_type = str(rule.get("type") or rule.get("source_type") or "url").strip().lower() or "url"
    value = str(
        rule.get("value")
        or rule.get("url")
        or rule.get("source_value")
        or rule.get("description")
        or ""
    ).strip()
    lookback = rule.get("lookback_days", 7)
    try:
        lookback = max(1, int(lookback))
    except Exception:
        lookback = 7
    strategy = str(rule.get("selection_strategy") or rule.get("strategy") or "latest").strip().lower() or "latest"
    segment_type = str(rule.get("segment_type") or "").strip()
    return {
        "type": rule_type,
        "value": value,
        "lookback_days": lookback,
        "selection_strategy": strategy,
        "segment_type": segment_type,
    }


def _normalize_source_rules(rules: list[dict] | None) -> list[dict]:
    out = []
    for rule in rules or []:
        if isinstance(rule, dict):
            normalized = _normalize_source_rule(rule)
            if normalized["value"]:
                out.append(normalized)
    return out


def get_taxonomy_api() -> dict[str, Any]:
    taxonomy = load_show_taxonomy()
    show_types = get_show_type_definitions()
    return {
        "show_types": {
            sid: _show_type_to_api(sid, cfg)
            for sid, cfg in show_types.items()
        },
        "topic_focuses": list(taxonomy.get("topic_focuses", [])),
        "bumper_styles": list(taxonomy.get("bumper_styles", [])),
        "source_types": list(taxonomy.get("source_types", [])),
    }


class ShowTaxonomyUpdate(BaseModel):
    topic_focuses: list[str] = []
    bumper_styles: list[str] = []
    show_types: dict[str, Any] | None = None


def _normalize_show_type_taxonomy(show_type_id: str, config: dict[str, Any] | None, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = config or {}
    base = dict(fallback or {})
    if "name" in raw:
        base["name"] = str(raw.get("name", base.get("name", show_type_id))).strip() or base.get("name", show_type_id)
    if "description" in raw:
        base["description"] = str(raw.get("description", base.get("description", ""))).strip()
    if "uses_topic_focus" in raw:
        base["uses_topic_focus"] = bool(raw.get("uses_topic_focus"))
    if "uses_source_rules" in raw:
        base["uses_source_rules"] = bool(raw.get("uses_source_rules"))
    if "uses_bumper_style" in raw:
        base["uses_bumper_style"] = bool(raw.get("uses_bumper_style"))
    if "default_segment_types" in raw:
        base["default_segment_types"] = [str(s).strip() for s in raw.get("default_segment_types", []) if str(s).strip()]
    seq = raw.get("playback_sequence_defaults")
    if isinstance(seq, dict):
        base["playback_sequence_defaults"] = normalize_playback_sequence(seq, show_type_id)
    elif "playback_sequence_defaults" in base:
        base["playback_sequence_defaults"] = normalize_playback_sequence(base["playback_sequence_defaults"], show_type_id)
    else:
        base["playback_sequence_defaults"] = default_playback_sequence_for_show_type(show_type_id)
    return base


@app.put("/api/show-taxonomy")
def update_show_taxonomy(update: ShowTaxonomyUpdate):
    data = load_show_taxonomy()
    data["topic_focuses"] = _normalise_taxonomy_items(update.topic_focuses)
    data["bumper_styles"] = _normalise_taxonomy_items(update.bumper_styles)
    existing_show_types = data.get("show_types", {}) if isinstance(data.get("show_types", {}), dict) else {}
    if update.show_types is not None:
        normalized_show_types = {}
        for sid, cfg in existing_show_types.items():
            if isinstance(sid, str) and sid.strip():
                normalized_show_types[sid] = _normalize_show_type_taxonomy(sid, cfg, existing_show_types.get(sid))
        for sid, cfg in update.show_types.items():
            if isinstance(sid, str) and sid.strip():
                normalized_show_types[sid] = _normalize_show_type_taxonomy(sid, cfg, existing_show_types.get(sid))
        data["show_types"] = normalized_show_types
    save_show_taxonomy(data)
    return get_taxonomy_api()


def get_segment_types_api() -> dict[str, dict]:
    return {
        sid: _segment_type_to_api(sid, config)
        for sid, config in get_segment_type_definitions().items()
    }


def _build_segment_inventory() -> dict[str, list[dict]]:
    """Return per-show segment inventory."""
    inv: dict[str, list[dict]] = {}
    if not TALK_SEGMENTS_DIR.exists():
        return inv
    all_hosts = get_all_hosts()
    schedule_data = load_schedule()
    for show_dir in sorted(TALK_SEGMENTS_DIR.iterdir()):
        if not show_dir.is_dir():
            continue
        host_meta = _show_primary_host_meta(show_dir.name, schedule_data)
        files = []
        for f in sorted(show_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.is_file() and f.suffix.lower() in AUDIO_EXTS:
                plays_meta = _read_plays_meta(f)
                gen_meta = _read_gen_meta(f)
                duration_seconds = _audio_duration_seconds(f, gen_meta)
                created_at = _station_datetime_from_iso(gen_meta.get("created_at") or gen_meta.get("generated_at"))
                if created_at is None:
                    try:
                        created_at = _station_from_timestamp(f.stat().st_mtime)
                    except Exception:
                        created_at = _station_now()
                expiry = _expiry_info(f, show_dir.name, "talk", plays_meta, schedule_data)
                voices = gen_meta.get("voices") if isinstance(gen_meta.get("voices"), dict) else {}
                host_id = str(gen_meta.get("host") or host_meta["host_id"]).strip() or host_meta["host_id"]
                host_entry = all_hosts.get(host_id, {})
                host_name = str(gen_meta.get("host_name") or host_entry.get("name") or host_meta["host_name"])
                backend = str(gen_meta.get("tts_backend") or host_meta["backend"] or "kokoro")
                voice = "" if backend == "youtube_ingest" else str(gen_meta.get("voice") or voices.get("host") or host_meta["voice"])
                speaker_labels = gen_meta.get("speaker_labels") if isinstance(gen_meta.get("speaker_labels"), dict) else {}
                speaker_count = len(speaker_labels) if speaker_labels else max(1, len(voices) if voices else 1)
                voice_details = ", ".join(f"{role}: {value}" for role, value in voices.items() if value) or voice
                files.append({
                    "name": f.name,
                    "show_id": show_dir.name,
                    "size_kb": round(f.stat().st_size / 1024),
                    "duration_seconds": duration_seconds,
                    "duration_label": _format_duration(duration_seconds),
                    "created_at": created_at.isoformat(),
                    "created_label": created_at.strftime("%Y-%m-%d %H:%M"),
                    "modified": _station_from_timestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "path": str(f.relative_to(PROJECT_ROOT)),
                    "play_count": plays_meta.get("play_count", 0),
                    "first_played": plays_meta.get("first_played_at", "")[:16],
                    "last_played": plays_meta.get("last_played_at", "")[:16],
                    **expiry,
                    "host_id": host_id,
                    "host_name": host_name,
                    "voice": voice,
                    "voices": voices,
                    "speaker_labels": speaker_labels,
                    "voice_details": voice_details,
                    "tts_backend": backend,
                    "audio_backend": backend,
                    "backend_origin": _backend_origin_label(backend),
                    "backend_label": _backend_label(backend),
                    "speaker_count": speaker_count,
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
    meta: dict = {}
    if sidecar.exists():
        try:
            meta = json.loads(sidecar.read_text()) or {}
        except Exception:
            meta = {}

    # Fallback: extract timestamp from filename and search output/scripts/
    # Filename pattern: {segment_type}_{topic_slug}_{YYYYMMDD_HHMMSS}.ext
    import re as _re
    m = _re.search(r'(\d{8}_\d{6})', f.stem)
    if m and SCRIPTS_DIR.exists():
        ts = m.group(1)
        for sf in SCRIPTS_DIR.glob(f"talk_*_{ts}.json"):
            try:
                script_meta = json.loads(sf.read_text()) or {}
                if isinstance(meta, dict):
                    # Prefer the richer script metadata when the lightweight
                    # audio sidecar omits voice/backend fields.
                    merged = dict(script_meta)
                    for key, value in meta.items():
                        if value in (None, "", [], {}, ()):
                            continue
                        merged[key] = value
                    return merged
                return script_meta
            except Exception:
                pass

    return meta if isinstance(meta, dict) else {}


def _format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return ""
    total = max(0, int(round(float(seconds))))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _audio_duration_seconds(f: Path, gen_meta: dict | None = None) -> float | None:
    meta = gen_meta or {}
    duration = meta.get("duration_seconds")
    if duration not in (None, ""):
        try:
            return float(duration)
        except Exception:
            pass

    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "csv=p=0",
                str(f),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            value = float(result.stdout.strip())
            try:
                sidecar = f.with_suffix(".json")
                payload: dict[str, Any] = {}
                if sidecar.exists():
                    payload = json.loads(sidecar.read_text()) or {}
                if payload.get("duration_seconds") in (None, ""):
                    payload["duration_seconds"] = value
                    sidecar.write_text(json.dumps(payload, indent=2))
            except Exception:
                pass
            return value
    except Exception:
        pass
    return None


def _station_datetime_from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _show_lifecycle(
    show_id: str,
    content_type: str,
    schedule_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = schedule_data or load_schedule()
    show = data.get("shows", {}).get(show_id, {})
    lifecycle = show.get("content_lifecycle", {}) if isinstance(show.get("content_lifecycle", {}), dict) else {}
    current = lifecycle.get(content_type, {}) if isinstance(lifecycle.get(content_type, {}), dict) else {}
    return {
        "max_plays": current.get("max_plays"),
        "max_days": current.get("max_days"),
    }


def _expiry_info(
    audio_path: Path,
    show_id: str,
    content_type: str,
    plays_meta: dict | None = None,
    schedule_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lifecycle = _show_lifecycle(show_id, content_type, schedule_data)
    max_plays = lifecycle.get("max_plays")
    max_days = lifecycle.get("max_days")
    meta = plays_meta or _read_plays_meta(audio_path)
    play_count = int(meta.get("play_count", 0) or 0)
    created_at = _station_datetime_from_iso(meta.get("created_at") or meta.get("generated_at"))
    if created_at is None:
        try:
            created_at = _station_from_timestamp(audio_path.stat().st_mtime)
        except Exception:
            created_at = _station_now()

    expiry_at = None
    if max_days is not None:
        try:
            expiry_at = created_at + timedelta(days=int(max_days))
        except Exception:
            expiry_at = None

    remaining_plays = None
    if max_plays is not None:
        try:
            remaining_plays = max(0, int(max_plays) - play_count)
        except Exception:
            remaining_plays = None

    parts = []
    if max_plays is not None:
        if remaining_plays == 0:
            parts.append("expires by play count")
        elif remaining_plays is not None:
            parts.append(f"{remaining_plays} plays left")
    if expiry_at is not None:
        parts.append(f"expires {expiry_at.strftime('%Y-%m-%d %H:%M')}")
    if not parts:
        parts.append("never expires")

    return {
        "expires_label": " · ".join(parts),
        "expires_at": expiry_at.isoformat() if expiry_at else "",
        "remaining_plays": remaining_plays if remaining_plays is not None else "",
        "lifecycle_max_plays": max_plays if max_plays is not None else "",
        "lifecycle_max_days": max_days if max_days is not None else "",
    }


def _backend_label(backend: str | None) -> str:
    value = str(backend or "").strip().lower()
    if value.startswith("kokoro"):
        return "Kokoro"
    if value.startswith("minimax"):
        return "MiniMax"
    if value.startswith("google"):
        return "Google Gemini"
    if value == "youtube_ingest":
        return "YouTube ingest"
    if value in {"music-gen", "music_gen", "musicgen"}:
        return "MiniMax music"
    return value or "unknown"


def _backend_origin_label(backend: str | None) -> str:
    value = str(backend or "").strip().lower()
    if value.startswith("kokoro"):
        return "local"
    if value.startswith("minimax"):
        return "cloud"
    if value.startswith("google"):
        return "cloud"
    if value == "youtube_ingest":
        return "source"
    if value in {"music-gen", "music_gen", "musicgen"}:
        return "cloud"
    return "unknown"


def _show_primary_host_meta(show_id: str, schedule_data: dict[str, Any] | None = None) -> dict[str, str]:
    """Resolve the primary host details for a show from the live schedule."""
    try:
        data = schedule_data or load_schedule()
    except Exception:
        data = {}
    show = (data.get("shows") or {}).get(show_id, {}) if isinstance(data, dict) else {}
    hosts = show.get("hosts") or []
    primary = next((h for h in hosts if h.get("role") == "primary"), None)
    if not primary:
        primary = {
            "id": show.get("host", show_id),
            "tts_backend": show.get("tts_backend", "kokoro"),
            "voice_kokoro": (show.get("voices") or {}).get("host", "am_michael"),
            "voice_minimax": "Deep_Voice_Man",
            "voice_google": "Kore",
        }
    roster = get_all_hosts()
    host_id = str(primary.get("id") or show.get("host") or show_id).strip() or show_id
    host_entry = roster.get(host_id, {})
    host_name = host_entry.get("name") or host_id
    backend = str(primary.get("tts_backend") or show.get("tts_backend") or host_entry.get("tts_backend") or "kokoro")
    if backend == "minimax":
        voice = primary.get("voice_minimax") or host_entry.get("voice_minimax") or "Deep_Voice_Man"
    elif backend == "google":
        voice = primary.get("voice_google") or host_entry.get("voice_google") or "Kore"
    else:
        voice = primary.get("voice_kokoro") or (show.get("voices") or {}).get("host") or host_entry.get("tts_voice") or "am_michael"
    return {
        "host_id": host_id,
        "host_name": host_name,
        "backend": backend,
        "voice": str(voice),
    }


def _build_bumper_inventory() -> dict[str, list[dict]]:
    inv: dict[str, list[dict]] = {}
    if not BUMPERS_DIR.exists():
        return inv
    all_hosts = get_all_hosts()
    schedule_data = load_schedule()
    for show_dir in sorted(BUMPERS_DIR.iterdir()):
        if not show_dir.is_dir():
            continue
        host_meta = _show_primary_host_meta(show_dir.name, schedule_data)
        files = []
        for f in sorted(show_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.is_file() and f.suffix.lower() in AUDIO_EXTS:
                plays_meta = _read_plays_meta(f)
                gen_meta = _read_gen_meta(f)
                duration_seconds = _audio_duration_seconds(f, gen_meta)
                created_at = _station_datetime_from_iso(gen_meta.get("created_at") or gen_meta.get("generated_at"))
                if created_at is None:
                    try:
                        created_at = _station_from_timestamp(f.stat().st_mtime)
                    except Exception:
                        created_at = _station_now()
                expiry = _expiry_info(f, show_dir.name, "music", plays_meta, schedule_data)
                host_id = str(gen_meta.get("host") or host_meta["host_id"]).strip() or host_meta["host_id"]
                host_entry = all_hosts.get(host_id, {})
                host_name = str(gen_meta.get("host_name") or host_entry.get("name") or host_meta["host_name"])
                backend = str(gen_meta.get("generation_backend") or gen_meta.get("audio_backend") or "music-gen")
                voice = str(gen_meta.get("voice") or ("vocal" if not gen_meta.get("instrumental", True) else "instrumental"))
                speaker_count = 1 if voice == "vocal" else 0
                voice_details = voice
                files.append({
                    "name": f.name,
                    "show_id": show_dir.name,
                    "size_kb": round(f.stat().st_size / 1024),
                    "duration_seconds": duration_seconds,
                    "duration_label": _format_duration(duration_seconds),
                    "created_at": created_at.isoformat(),
                    "created_label": created_at.strftime("%Y-%m-%d %H:%M"),
                    "modified": _station_from_timestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "play_count": plays_meta.get("play_count", 0),
                    "first_played": plays_meta.get("first_played_at", "")[:16],
                    "last_played": plays_meta.get("last_played_at", "")[:16],
                    **expiry,
                    "host_id": host_id,
                    "host_name": host_name,
                    "voice": voice,
                    "tts_backend": backend,
                    "audio_backend": backend,
                    "backend_origin": _backend_origin_label(backend),
                    "backend_label": _backend_label(backend),
                    "speaker_count": speaker_count,
                    "voice_details": voice_details,
                    "prompt": gen_meta.get("caption", gen_meta.get("topic", "")),
                    "display_name": gen_meta.get("display_name", ""),
                    "generated_at": gen_meta.get("generated_at", ""),
                })
        inv[show_dir.name] = files
    return inv


def _cached_inventory(kind: str) -> dict[str, list[dict]]:
    now = time.monotonic()
    with _inventory_cache_lock:
        cached = _inventory_cache[kind]
        if cached["value"] and (now - float(cached["ts"] or 0.0)) < INVENTORY_CACHE_TTL:
            return cached["value"]
    value = _build_segment_inventory() if kind == "segments" else _build_bumper_inventory()
    with _inventory_cache_lock:
        _inventory_cache[kind] = {"ts": now, "value": value}
    return value


def _invalidate_inventory_cache(kind: str | None = None) -> None:
    with _inventory_cache_lock:
        kinds = [kind] if kind in _inventory_cache else list(_inventory_cache.keys()) if kind is None else []
        for k in kinds:
            _inventory_cache[k] = {"ts": 0.0, "value": {}}


def _streamer_request(path: str, method: str = "GET", payload: dict | None = None, timeout: float = 5.0) -> dict:
    import urllib.request as _ur
    import urllib.error as _ue

    url = f"{STREAMER_API}{path}"
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = _ur.Request(url, data=data, headers=headers, method=method)
    try:
        with _ur.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode() or "{}"
            return json.loads(raw)
    except _ue.HTTPError as e:
        body = e.read().decode() if hasattr(e, "read") else ""
        detail = body or e.reason or str(e)
        raise HTTPException(e.code, f"Streamer request failed: {detail}")
    except Exception as e:
        raise HTTPException(502, f"Streamer request failed: {e}")


# ---------------------------------------------------------------------------
# API: Status
# ---------------------------------------------------------------------------

@app.get("/api/status")
def get_status():
    import urllib.request
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

    segments = _cached_inventory("segments")
    total_segments = sum(len(v) for v in segments.values())

    bumpers = _cached_inventory("bumpers")
    total_bumpers = sum(len(v) for v in bumpers.values())

    return {
        "icecast": icecast_ok,
        "streamer": streamer_ok,
        "now_playing": now_playing,
        "total_segments": total_segments,
        "total_bumpers": total_bumpers,
        "segments_per_show": {k: len(v) for k, v in segments.items()},
        "timestamp": _station_iso_now(),
    }


@app.get("/api/live/status")
def get_live_status():
    status = get_status()
    try:
        queue = _streamer_request("/queue")
    except HTTPException as e:
        queue = {"error": e.detail}
    return {
        **status,
        "queue": queue,
    }


@app.post("/api/live/skip")
def skip_live_current():
    return _streamer_request("/control", method="POST", payload={"action": "skip"})


@app.post("/api/live/control")
def live_control(payload: dict):
    action = str(payload.get("action", "")).strip().lower()
    if not action:
        raise HTTPException(400, "action required")
    return _streamer_request("/control", method="POST", payload=payload)


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
    show_types = get_show_type_definitions()
    s["show_type"] = s.get("show_type") or "research"
    if s["show_type"] not in show_types:
        s["show_type"] = "research"
    all_hosts = get_all_hosts()
    # Normalize hosts to list format
    if "hosts" not in s:
        primary_id = s.get("host", "liminal_operator")
        primary = _default_host_assignment(primary_id, "primary")
        primary["tts_backend"] = s.get("tts_backend", primary["tts_backend"])
        primary["voice_kokoro"] = s.get("voices", {}).get("host", primary["voice_kokoro"])
        s["hosts"] = [primary]
        if "voices" in s and "guest" in s["voices"]:
            guest = _default_host_assignment(primary_id, "guest")
            guest["voice_kokoro"] = s["voices"]["guest"]
            s["hosts"].append(guest)
    else:
        normalized_hosts = []
        for idx, host in enumerate(s.get("hosts", [])):
            host_id = host.get("id", "liminal_operator")
            role = host.get("role", "primary" if idx == 0 else "co-host")
            defaults = _default_host_assignment(host_id, role)
            normalized_hosts.append({
                **defaults,
                **host,
            })
        s["hosts"] = normalized_hosts
    if "research_topic" not in s:
        s["research_topic"] = ""
    if "research_sources" not in s:
        s["research_sources"] = []
    if "source_rules" not in s:
        s["source_rules"] = list(s.get("research_sources", []))
    s["source_rules"] = _normalize_source_rules(s.get("source_rules", []))
    s["research_sources"] = _normalize_source_rules(s.get("research_sources", [])) or list(s.get("research_sources", []))
    if not s.get("segment_types"):
        s["segment_types"] = _default_segment_types_for_show_type(s["show_type"])
    if "guests" not in s:
        s["guests"] = []
    if "content_lifecycle" not in s:
        s["content_lifecycle"] = {}
    s["playback_sequence"] = merge_playback_sequence(
        s["show_type"],
        s.get("playback_sequence") if isinstance(s.get("playback_sequence"), dict) else s.get("sequence") if isinstance(s.get("sequence"), dict) else {},
    )
    for idx, host in enumerate(s["hosts"]):
        host_id = host.get("id")
        roster = all_hosts.get(host_id, {})
        if not host.get("display_name"):
            host["display_name"] = roster.get("name", host_id)
    return s


class ShowUpdate(BaseModel):
    name: str
    description: str
    show_type: str = "research"
    hosts: list[dict]
    topic_focus: str
    research_topic: str = ""
    research_sources: list[dict] = []
    source_rules: list[dict] = []
    guests: list[dict] = []
    segment_types: list[str]
    bumper_style: str
    playback_sequence: dict = {}
    content_lifecycle: dict = {}
    generation: dict = {}


@app.put("/api/shows/{show_id}")
def update_show(show_id: str, update: ShowUpdate):
    data = load_schedule()
    shows = data.get("shows", {})
    if show_id not in shows:
        raise HTTPException(404, f"Show '{show_id}' not found")
    if not update.hosts:
        raise HTTPException(400, "Shows must have at least one host")
    show_types = set(get_show_type_definitions().keys())
    if update.show_type not in show_types:
        raise HTTPException(400, f"Unknown show type '{update.show_type}'")
    roster_ids = set(get_all_hosts().keys())
    segment_type_ids = set(get_segment_types_api().keys())
    for host in update.hosts:
        if host.get("id") not in roster_ids:
            raise HTTPException(400, f"Unknown host '{host.get('id')}'")
    segment_types = update.segment_types or _default_segment_types_for_show_type(update.show_type)
    for segment_type_id in segment_types:
        if segment_type_id not in segment_type_ids:
            raise HTTPException(400, f"Unknown segment type '{segment_type_id}'")
    source_rules = _normalize_source_rules(update.source_rules or update.research_sources)

    show = dict(shows[show_id])
    show["name"] = update.name
    show["description"] = update.description
    show["show_type"] = update.show_type
    show["topic_focus"] = update.topic_focus
    show["research_topic"] = update.research_topic
    show["research_sources"] = source_rules
    show["source_rules"] = source_rules
    show["guests"] = update.guests
    show["segment_types"] = segment_types
    show["bumper_style"] = update.bumper_style
    show["hosts"] = update.hosts
    show["playback_sequence"] = playback_sequence_overrides(update.show_type, update.playback_sequence)
    show["content_lifecycle"] = update.content_lifecycle
    show["generation"] = update.generation

    # Keep legacy fields for backward compat with streamer
    primary = next((h for h in update.hosts if h.get("role") == "primary"), update.hosts[0] if update.hosts else {})
    show["host"] = primary.get("id", "liminal_operator")
    show["tts_backend"] = primary.get("tts_backend", "kokoro")
    if show["tts_backend"] == "minimax":
        legacy_host_voice = primary.get("voice_minimax", "Deep_Voice_Man")
        legacy_guest_voice = "Wise_Woman"
    elif show["tts_backend"] == "google":
        legacy_host_voice = primary.get("voice_google", "Kore")
        legacy_guest_voice = "Puck"
    else:
        legacy_host_voice = primary.get("voice_kokoro", "am_michael")
        legacy_guest_voice = "af_bella"
    voices = {"host": legacy_host_voice}
    for h in update.hosts:
        if h.get("role") in ("guest", "co-host", "secondary"):
            if show["tts_backend"] == "minimax":
                voices["guest"] = h.get("voice_minimax", legacy_guest_voice)
            elif show["tts_backend"] == "google":
                voices["guest"] = h.get("voice_google", legacy_guest_voice)
            else:
                voices["guest"] = h.get("voice_kokoro", legacy_guest_voice)
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
    if not update.hosts:
        raise HTTPException(400, "Shows must have at least one host")
    show_types = set(get_show_type_definitions().keys())
    if update.show_type not in show_types:
        raise HTTPException(400, f"Unknown show type '{update.show_type}'")
    roster_ids = set(get_all_hosts().keys())
    segment_type_ids = set(get_segment_types_api().keys())
    for host in update.hosts:
        if host.get("id") not in roster_ids:
            raise HTTPException(400, f"Unknown host '{host.get('id')}'")
    segment_types = update.segment_types or _default_segment_types_for_show_type(update.show_type)
    for segment_type_id in segment_types:
        if segment_type_id not in segment_type_ids:
            raise HTTPException(400, f"Unknown segment type '{segment_type_id}'")
    source_rules = _normalize_source_rules(update.source_rules or update.research_sources)

    show = {
        "name": update.name,
        "description": update.description,
        "show_type": update.show_type,
        "topic_focus": update.topic_focus,
        "research_topic": update.research_topic,
        "research_sources": source_rules,
        "source_rules": source_rules,
        "guests": update.guests,
        "segment_types": segment_types,
        "bumper_style": update.bumper_style,
        "hosts": update.hosts,
        "playback_sequence": playback_sequence_overrides(update.show_type, update.playback_sequence),
        "content_lifecycle": update.content_lifecycle,
        "generation": update.generation,
    }
    primary = next((h for h in update.hosts if h.get("role") == "primary"), update.hosts[0] if update.hosts else {})
    show["host"] = primary.get("id", "liminal_operator")
    show["tts_backend"] = primary.get("tts_backend", "kokoro")
    if show["tts_backend"] == "minimax":
        show["voices"] = {"host": primary.get("voice_minimax", "Deep_Voice_Man")}
    elif show["tts_backend"] == "google":
        show["voices"] = {"host": primary.get("voice_google", "Kore")}
    else:
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
    data["timezone"] = _validate_timezone_name(update.timezone)
    data["schedule"] = {"base": update.base, "overrides": update.overrides}
    try:
        _validate_schedule_config(data)
    except StationScheduleError as exc:
        raise HTTPException(400, str(exc)) from exc
    save_schedule(data)
    return {"ok": True}


# ---------------------------------------------------------------------------
# API: Station Settings
# ---------------------------------------------------------------------------

@app.get("/api/settings")
def get_settings():
    data = load_schedule()
    return {
        "timezone": data.get("timezone", "local"),
        "station_name": data.get("station_name", "WRIT-FM"),
    }


class SettingsUpdate(BaseModel):
    timezone: str = "local"
    station_name: str = "WRIT-FM"


@app.put("/api/settings")
def update_settings(update: SettingsUpdate):
    data = load_schedule()
    data["timezone"] = _validate_timezone_name(update.timezone)
    data["station_name"] = (update.station_name or "WRIT-FM").strip() or "WRIT-FM"
    save_schedule(data)
    return {"ok": True, "timezone": data["timezone"], "station_name": data["station_name"]}


@app.post("/api/station/refresh")
def refresh_station():
    """Restart the streamer so it reloads the latest schedule/config."""
    try:
        services = [STATION_SERVICE]
        if LEGACY_STREAMER_SERVICE and LEGACY_STREAMER_SERVICE != STATION_SERVICE:
            services.append(LEGACY_STREAMER_SERVICE)
        for service in services:
            result = subprocess.run(
                ["systemctl", "restart", service],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if result.returncode != 0:
                details = (result.stderr or result.stdout or "").strip()
                raise HTTPException(500, f"Failed to restart {service}: {details or 'unknown error'}")
        return {"ok": True, "service": STATION_SERVICE, "legacy_service": LEGACY_STREAMER_SERVICE}
    except Exception as e:
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(500, f"Failed to restart {STATION_SERVICE}: {e}") from e


# ---------------------------------------------------------------------------
# API: Hosts
# ---------------------------------------------------------------------------

@app.get("/api/hosts")
def get_hosts():
    return get_all_hosts()


class HostUpdate(BaseModel):
    name: str
    bio: str = ""
    voice_style: str = ""
    philosophy: str = ""
    anti_patterns: str = ""
    tts_backend: str = "kokoro"
    tts_voice: str = "am_michael"
    voice_minimax: str = "Deep_Voice_Man"
    voice_google: str = "Kore"
    speaking_pace_wpm: int = 130
    speaking_pace_wpm_kokoro: int = 130
    speaking_pace_wpm_minimax: int = 130
    speaking_pace_wpm_google: int = 130
    topics: list[str] = []


def _host_update_to_yaml(update: HostUpdate) -> dict:
    return {
        "name": update.name,
        "identity": update.bio,
        "voice_style": update.voice_style,
        "philosophy": update.philosophy,
        "anti_patterns": update.anti_patterns,
        "tts_backend": update.tts_backend,
        "tts_voice": update.tts_voice,
        "voice_minimax": update.voice_minimax,
        "voice_google": update.voice_google,
        "speaking_pace_wpm": update.speaking_pace_wpm,
        "speaking_pace_wpm_kokoro": update.speaking_pace_wpm_kokoro,
        "speaking_pace_wpm_minimax": update.speaking_pace_wpm_minimax,
        "speaking_pace_wpm_google": update.speaking_pace_wpm_google,
        "topics": update.topics,
    }


@app.post("/api/hosts/{host_id}")
def create_host(host_id: str, update: HostUpdate):
    data = load_hosts_config()
    hosts = data.get("hosts", {})
    if host_id in hosts:
        raise HTTPException(400, f"Host '{host_id}' already exists")
    hosts[host_id] = _host_update_to_yaml(update)
    data["hosts"] = hosts
    save_hosts_config(data)
    return {"ok": True, "host_id": host_id}


@app.put("/api/hosts/{host_id}")
def update_host(host_id: str, update: HostUpdate):
    data = load_hosts_config()
    hosts = data.get("hosts", {})
    if host_id not in hosts:
        raise HTTPException(404, f"Host '{host_id}' not found")
    hosts[host_id] = _host_update_to_yaml(update)
    data["hosts"] = hosts
    save_hosts_config(data)
    return {"ok": True, "host_id": host_id}


@app.delete("/api/hosts/{host_id}")
def delete_host(host_id: str):
    schedule = load_schedule()
    for show_id, show in schedule.get("shows", {}).items():
        for host in show.get("hosts", []):
            if host.get("id") == host_id:
                raise HTTPException(409, f"Host '{host_id}' is still assigned to show '{show_id}'")
        if show.get("host") == host_id:
            raise HTTPException(409, f"Host '{host_id}' is still assigned to show '{show_id}'")
    data = load_hosts_config()
    hosts = data.get("hosts", {})
    if host_id not in hosts:
        raise HTTPException(404, f"Host '{host_id}' not found")
    del hosts[host_id]
    data["hosts"] = hosts
    save_hosts_config(data)
    return {"ok": True}


# ---------------------------------------------------------------------------
# API: TTS Preview
# ---------------------------------------------------------------------------

_PREVIEW_CACHE: dict[tuple, Path] = {}

def _preview_text() -> str:
    station_name = load_schedule().get("station_name", "the station")
    station_name = str(station_name).strip() or "the station"
    return f"This is {station_name}. The frequency between frequencies."


@app.post("/api/tts/preview")
def tts_preview(body: dict, background_tasks: BackgroundTasks):
    """Generate a short TTS sample for voice auditioning.

    Body: { backend: "kokoro"|"minimax"|"google", voice: "...", text?: "..." }
    Returns audio file (WAV for Kokoro/Google, MP3 for MiniMax).
    """
    import importlib.util
    import tempfile

    backend = body.get("backend", "kokoro")
    voice = body.get("voice", "am_michael")
    text = body.get("text", _preview_text())

    # Serve from cache if available
    cache_key = (backend, voice)
    if cache_key in _PREVIEW_CACHE and _PREVIEW_CACHE[cache_key].exists():
        cached = _PREVIEW_CACHE[cache_key]
        mt = "audio/wav" if cached.suffix == ".wav" else "audio/mpeg"
        return FileResponse(str(cached), media_type=mt, filename=f"preview{cached.suffix}")

    if backend == "kokoro":
        spec = importlib.util.spec_from_file_location(
            "kokoro_tts", PROJECT_ROOT / "mac" / "kokoro" / "tts.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        tmp = Path(tempfile.mktemp(suffix=".wav", prefix="writ_preview_"))
        ok = mod.render_speech(text, tmp, voice=voice)
        if not ok or not tmp.exists():
            tmp.unlink(missing_ok=True)
            raise HTTPException(500, "Kokoro TTS preview failed")

        _PREVIEW_CACHE[cache_key] = tmp
        return FileResponse(str(tmp), media_type="audio/wav", filename="preview.wav")

    elif backend == "minimax":
        api_key = os.environ.get("MINIMAX_API_KEY", "")
        if not api_key:
            raise HTTPException(503, "MINIMAX_API_KEY not set")

        spec = importlib.util.spec_from_file_location(
            "minimax_tts", PROJECT_ROOT / "mac" / "minimax_tts.py"
        )
        mod = importlib.util.module_from_spec(spec)
        # Inject env so the module picks up the key at load time
        os.environ.setdefault("MINIMAX_API_KEY", api_key)
        spec.loader.exec_module(mod)

        tmp = Path(tempfile.mktemp(suffix=".mp3", prefix="writ_preview_"))
        ok = mod.generate_speech(text, tmp, voice_id=voice)
        if not ok or not tmp.exists():
            tmp.unlink(missing_ok=True)
            raise HTTPException(500, "MiniMax TTS preview failed")

        _PREVIEW_CACHE[cache_key] = tmp
        return FileResponse(str(tmp), media_type="audio/mpeg", filename="preview.mp3")
    elif backend == "google":
        api_key = os.environ.get("GOOGLE_TTS_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")
        if not api_key:
            raise HTTPException(503, "GOOGLE_TTS_API_KEY or GEMINI_API_KEY not set")

        spec = importlib.util.spec_from_file_location(
            "google_tts", PROJECT_ROOT / "mac" / "google_tts.py"
        )
        mod = importlib.util.module_from_spec(spec)
        os.environ["GOOGLE_TTS_API_KEY"] = api_key
        spec.loader.exec_module(mod)

        tmp = Path(tempfile.mktemp(suffix=".wav", prefix="writ_preview_"))
        ok = mod.generate_speech(text, tmp, voice_id=voice)
        if not ok or not tmp.exists():
            tmp.unlink(missing_ok=True)
            raise HTTPException(500, "Google Gemini TTS preview failed")

        _PREVIEW_CACHE[cache_key] = tmp
        return FileResponse(str(tmp), media_type="audio/wav", filename="preview.wav")

    else:
        raise HTTPException(400, f"Unknown TTS backend: {backend!r}")


# ---------------------------------------------------------------------------
# API: Voice Samples
# ---------------------------------------------------------------------------


@app.get("/api/voice-samples")
def get_voice_samples():
    return voice_catalog()


@app.post("/api/voice-samples/ensure")
def ensure_voice_samples_api(body: dict | None = None):
    body = body or {}
    force = bool(body.get("force", False))
    backends = body.get("backends") or ["kokoro", "minimax", "google"]
    created = ensure_voice_samples(backends=backends, force=force)
    return {"ok": True, "created": created}


@app.get("/api/voice-samples/audio/{backend}/{voice}")
def stream_voice_sample(backend: str, voice: str):
    path = sample_path(backend, voice)
    if not path.exists():
        raise HTTPException(404, f"Voice sample not found for {backend}:{voice}")
    return FileResponse(str(path), media_type=sample_media_type(path))


# ---------------------------------------------------------------------------
# API: Segment Types
# ---------------------------------------------------------------------------

@app.get("/api/segment-types")
def get_segment_types():
    return get_segment_types_api()


@app.get("/api/show-taxonomy")
def get_show_taxonomy():
    return get_taxonomy_api()


class SegmentTypeUpdate(BaseModel):
    name: str
    description: str = ""
    word_count_min: int = 1500
    word_count_max: int = 2500
    multi_voice: bool = False
    prompt_template: str


def _segment_type_update_to_yaml(update: SegmentTypeUpdate) -> dict:
    return {
        "name": update.name,
        "description": update.description,
        "word_count_min": update.word_count_min,
        "word_count_max": update.word_count_max,
        "multi_voice": update.multi_voice,
        "prompt_template": update.prompt_template,
    }


@app.post("/api/segment-types/{segment_type_id}")
def create_segment_type(segment_type_id: str, update: SegmentTypeUpdate):
    if update.word_count_max < update.word_count_min:
        raise HTTPException(400, "word_count_max must be >= word_count_min")
    data = load_segment_types_config()
    segment_types = data.get("segment_types", {})
    if segment_type_id in segment_types:
        raise HTTPException(400, f"Segment type '{segment_type_id}' already exists")
    segment_types[segment_type_id] = _segment_type_update_to_yaml(update)
    data["segment_types"] = segment_types
    save_segment_types_config(data)
    return {"ok": True, "segment_type_id": segment_type_id}


@app.put("/api/segment-types/{segment_type_id}")
def update_segment_type(segment_type_id: str, update: SegmentTypeUpdate):
    if update.word_count_max < update.word_count_min:
        raise HTTPException(400, "word_count_max must be >= word_count_min")
    data = load_segment_types_config()
    segment_types = data.get("segment_types", {})
    segment_types[segment_type_id] = _segment_type_update_to_yaml(update)
    data["segment_types"] = segment_types
    save_segment_types_config(data)
    return {"ok": True, "segment_type_id": segment_type_id}


@app.delete("/api/segment-types/{segment_type_id}")
def delete_segment_type(segment_type_id: str):
    schedule = load_schedule()
    for show_id, show in schedule.get("shows", {}).items():
        if segment_type_id in (show.get("segment_types") or []):
            raise HTTPException(409, f"Segment type '{segment_type_id}' is still assigned to show '{show_id}'")

    data = load_segment_types_config()
    segment_types = data.get("segment_types", {})
    if segment_type_id not in segment_types:
        raise HTTPException(404, f"Segment type '{segment_type_id}' not found")
    del segment_types[segment_type_id]
    data["segment_types"] = segment_types
    save_segment_types_config(data)
    return {"ok": True}


# ---------------------------------------------------------------------------
# API: Library
# ---------------------------------------------------------------------------

def _library_base(kind: str) -> tuple[Path, str]:
    kind = (kind or "").strip().lower()
    if kind in {"segment", "segments", "talk"}:
        return TALK_SEGMENTS_DIR, "talk"
    if kind in {"bumper", "bumpers", "music"}:
        return BUMPERS_DIR, "music"
    raise HTTPException(400, f"Unknown library kind: {kind!r}")


def _library_target(show_id: str, kind: str, filename: str) -> Path:
    base, _ = _library_base(kind)
    return base / show_id / filename


def _unique_library_destination(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem = dest.stem
    suffix = dest.suffix
    parent = dest.parent
    for i in range(1, 1000):
        candidate = parent / f"{stem}_copy{i}{suffix}"
        if not candidate.exists():
            return candidate
    raise HTTPException(409, f"Unable to find a free destination name for {dest.name}")


def _library_copy_sidecars(src: Path, dest: Path, target_show_id: str, fresh_plays: bool = True) -> None:
    audio_sidecar = src.with_suffix(".json")
    if audio_sidecar.exists():
        try:
            meta = json.loads(audio_sidecar.read_text())
        except Exception:
            meta = {}
        meta["show_id"] = target_show_id
        meta["copied_at"] = _station_iso_now()
        dest.with_suffix(".json").write_text(json.dumps(meta, indent=2))

    plays_sidecar = src.parent / (src.name + ".plays.json")
    if plays_sidecar.exists() or fresh_plays:
        try:
            meta = json.loads(plays_sidecar.read_text()) if plays_sidecar.exists() else {}
        except Exception:
            meta = {}
        if fresh_plays:
            meta = {
                "play_count": 0,
                "created_at": _station_iso_now(),
            }
        else:
            meta["play_count"] = int(meta.get("play_count", 0) or 0)
        dest.parent.joinpath(dest.name + ".plays.json").write_text(json.dumps(meta, indent=2))

@app.get("/api/library/segments")
def get_segments():
    return _cached_inventory("segments")


@app.get("/api/library/bumpers")
def get_bumpers():
    return _cached_inventory("bumpers")


@app.delete("/api/library/segments/{show_id}/{filename}")
def delete_segment(show_id: str, filename: str):
    path = TALK_SEGMENTS_DIR / show_id / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    path.unlink()
    _invalidate_inventory_cache("segments")
    return {"ok": True}


@app.delete("/api/library/bumpers/{show_id}/{filename}")
def delete_bumper(show_id: str, filename: str):
    path = BUMPERS_DIR / show_id / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    path.unlink()
    _invalidate_inventory_cache("bumpers")
    return {"ok": True}


@app.get("/api/library/audio/{show_id}/{filename}")
def stream_audio(show_id: str, filename: str, type: str = "segment"):
    base = TALK_SEGMENTS_DIR if type == "segment" else BUMPERS_DIR
    path = base / show_id / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    suffix = path.suffix.lower()
    media_type = {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
    }.get(suffix, "application/octet-stream")
    return FileResponse(str(path), media_type=media_type)


class LibraryTransferRequest(BaseModel):
    target_show_id: str


def _copy_or_move_library_item(kind: str, show_id: str, filename: str, target_show_id: str, move: bool = False) -> dict:
    base, _ = _library_base(kind)
    shows = load_schedule().get("shows", {})
    if target_show_id not in shows:
        raise HTTPException(404, f"Target show '{target_show_id}' not found")

    src = base / show_id / filename
    if not src.exists():
        raise HTTPException(404, "File not found")

    dest_dir = base / target_show_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_library_destination(dest_dir / filename)

    if move:
        shutil.move(str(src), str(dest))
        src_sidecar = src.with_suffix(".json")
        if src_sidecar.exists():
            shutil.move(str(src_sidecar), str(dest.with_suffix(".json")))
        src_plays = src.parent / (src.name + ".plays.json")
        if src_plays.exists():
            shutil.move(str(src_plays), str(dest.parent / (dest.name + ".plays.json")))
    else:
        shutil.copy2(src, dest)
        _library_copy_sidecars(src, dest, target_show_id, fresh_plays=True)

    sidecar = dest.with_suffix(".json")
    if sidecar.exists():
        try:
            meta = json.loads(sidecar.read_text())
            meta["show_id"] = target_show_id
            sidecar.write_text(json.dumps(meta, indent=2))
        except Exception:
            pass
    _invalidate_inventory_cache(base.name == "talk_segments" and "segments" or "bumpers")

    return {"ok": True, "target_show_id": target_show_id, "filename": dest.name}


@app.post("/api/library/{kind}/{show_id}/{filename}/copy")
def copy_library_item(kind: str, show_id: str, filename: str, body: LibraryTransferRequest):
    return _copy_or_move_library_item(kind, show_id, filename, body.target_show_id, move=False)


@app.post("/api/library/{kind}/{show_id}/{filename}/move")
def move_library_item(kind: str, show_id: str, filename: str, body: LibraryTransferRequest):
    return _copy_or_move_library_item(kind, show_id, filename, body.target_show_id, move=True)


@app.post("/api/library/{kind}/{show_id}/{filename}/reset-plays")
def reset_library_plays(kind: str, show_id: str, filename: str):
    base, _ = _library_base(kind)
    path = base / show_id / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    sidecar = path.parent / (path.name + ".plays.json")
    try:
        meta = json.loads(sidecar.read_text()) if sidecar.exists() else {}
    except Exception:
        meta = {}
    meta["play_count"] = 0
    meta.pop("first_played_at", None)
    meta.pop("last_played_at", None)
    if "created_at" not in meta:
        meta["created_at"] = _station_iso_now()
    sidecar.write_text(json.dumps(meta, indent=2))
    _invalidate_inventory_cache("segments" if kind == "segments" else "bumpers")
    return {"ok": True}


# ---------------------------------------------------------------------------
# API: Generation
# ---------------------------------------------------------------------------

TOPIC_FOCUSES = [
    "philosophy", "music_history", "current_events", "culture",
    "soul_music", "night_philosophy", "listeners",
]


@app.get("/api/generate/options")
def get_generate_options():
    segment_types = get_segment_types_api()
    taxonomy = get_taxonomy_api()
    voices = voice_catalog()
    return {
        "segment_types": list(segment_types.keys()),
        "segment_type_defs": segment_types,
        "topic_focuses": taxonomy["topic_focuses"],
        "bumper_styles": taxonomy["bumper_styles"],
        "show_types": taxonomy["show_types"],
        "kokoro_voices": [v["voice"] for v in voices["kokoro"]],
        "minimax_voices": [v["voice"] for v in voices["minimax"]],
        "google_voices": [v["voice"] for v in voices["google"]],
        "kokoro_voice_defs": voices["kokoro"],
        "minimax_voice_defs": voices["minimax"],
        "google_voice_defs": voices["google"],
        "source_types": taxonomy["source_types"],
        "shows": list(load_schedule().get("shows", {}).keys()),
    }


class GenerateRequest(BaseModel):
    show_id: str
    content_type: str = "talk"  # "talk" or "music"
    segment_type: str = "random"
    topic: str = ""
    include_topic: bool = True
    source_type: str = ""      # "", "url", "reddit", "youtube"
    source_value: str = ""
    count: int = 1
    # Override TTS for this run
    tts_backend: str = ""   # "kokoro", "minimax", "google", or "" to use show default
    minimax_long_async: bool = False
    host_voice: str = ""    # override primary host voice
    # Guest for this run
    guest_name: str = ""
    guest_voice_kokoro: str = "af_bella"
    guest_voice_minimax: str = "Wise_Woman"
    guest_voice_google: str = "Puck"
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
        "include_topic": req.include_topic,
        "source_type": req.source_type,
        "source_value": req.source_value,
        "log": [],
        "created_at": _station_iso_now(),
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
    ts = _station_now().strftime("%H:%M:%S")
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
        env["OLLAMA_MODEL"] = env.get("OLLAMA_MODEL", "llama3.2:3b")
        env["MINIMAX_API_KEY"] = env.get("MINIMAX_API_KEY", "")
        env["MINIMAX_TOKEN_PLAN_API_KEY"] = env.get("MINIMAX_TOKEN_PLAN_API_KEY", "")
        env["MINIMAX_MUSIC_MODEL"] = env.get("MINIMAX_MUSIC_MODEL", "music-2.6")
        env["GOOGLE_TTS_API_KEY"] = env.get("GOOGLE_TTS_API_KEY", "") or env.get("GEMINI_API_KEY", "") or env.get("GOOGLE_API_KEY", "")
        env["GOOGLE_TTS_MODEL"] = env.get("GOOGLE_TTS_MODEL", "gemini-3.1-flash-tts-preview")

        # Determine TTS backend
        tts_backend = req.tts_backend or show.get("tts_backend", "kokoro")
        is_youtube_ingest = req.source_type == "youtube" and req.content_type != "music"

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

            resolved_segment_type = req.segment_type
            if req.source_type == "reddit" and resolved_segment_type == "random":
                resolved_segment_type = "reddit_post"
                _log_job(job_id, "Source type 'reddit' selected with random segment type; using 'reddit_post'.")
            elif req.source_type == "youtube" and resolved_segment_type == "random":
                resolved_segment_type = "youtube"
                _log_job(job_id, "Source type 'youtube' selected with random segment type; using 'youtube'.")

            if resolved_segment_type and resolved_segment_type != "random":
                cmd += ["--type", resolved_segment_type]
            if req.include_topic and req.topic and not is_youtube_ingest:
                cmd += ["--topic", req.topic]
            if not req.include_topic and not is_youtube_ingest:
                cmd += ["--no-topic"]
            if req.source_type:
                cmd += ["--source-type", req.source_type]
            if req.source_value:
                cmd += ["--source-value", req.source_value]

            # Pass TTS backend via env
            if not is_youtube_ingest:
                env["WRIT_TTS_BACKEND"] = tts_backend
                if req.minimax_long_async:
                    env["WRIT_MINIMAX_LONG_ASYNC"] = "1"
                if req.host_voice:
                    env["WRIT_HOST_VOICE"] = req.host_voice
                if req.guest_name:
                    env["WRIT_GUEST_NAME"] = req.guest_name
                    env["WRIT_GUEST_VOICE_KOKORO"] = req.guest_voice_kokoro
                    env["WRIT_GUEST_VOICE_MINIMAX"] = req.guest_voice_minimax
                    env["WRIT_GUEST_VOICE_GOOGLE"] = req.guest_voice_google
                    env["WRIT_GUEST_TTS_BACKEND"] = req.guest_tts_backend

            if is_youtube_ingest:
                _log_job(job_id, "YouTube ingest selected; skipping Topic, Hosts & TTS controls.")
            else:
                _log_job(job_id, f"TTS backend: {tts_backend}")
            if req.minimax_long_async and not is_youtube_ingest:
                _log_job(job_id, "MiniMax long-form async: enabled (Kokoro validation first)")
            if req.source_type and req.source_value:
                _log_job(job_id, f"Source: {req.source_type} — {req.source_value[:120]}")

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
            if req.content_type == "music":
                _invalidate_inventory_cache("bumpers")
            else:
                _invalidate_inventory_cache("segments")
            _jobs[job_id]["status"] = "completed"
            _log_job(job_id, f"Generation complete.")
        else:
            _jobs[job_id]["status"] = "failed"
            _log_job(job_id, f"Generation failed (exit {proc.returncode})")

    except Exception as e:
        _log_job(job_id, f"ERROR: {e}")
        _jobs[job_id]["status"] = "failed"

    _jobs[job_id]["completed_at"] = _station_iso_now()
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
        talk_cfg = {**_sched.DEFAULT_TALK_CONFIG, **(gen.get("talk") or {})}
        music_cfg = {**_sched.DEFAULT_MUSIC_CONFIG, **(gen.get("music") or {})}
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
        ollama_model = os.environ.get("OLLAMA_MODEL", "llama3.2:3b")
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


@app.post("/api/scheduler/trigger/{show_id}")
def trigger_generation_show(show_id: str):
    msg = _sched.trigger_now(show_id, "show", _jobs)
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
    return HTMLResponse("<h1>Station Admin</h1><p>index.html not found.</p>")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("WRIT_ADMIN_PORT", "8080"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
