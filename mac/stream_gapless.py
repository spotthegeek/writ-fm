#!/usr/bin/env python3
"""
WRIT-FM Gapless Streamer (Talk-First Edition)

Streams talk segments with music bumpers to Icecast.
Uses a single ffmpeg encoder fed by continuous PCM from decoded audio.

Flow: talk segment -> music bumper (60-120s) -> talk segment -> ...
"""

import subprocess
import random
import signal
import sys
import os
import re
import json
import time
import urllib.request
import threading
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass, field

# Import play history tracker
try:
    from play_history import get_history
    HISTORY_ENABLED = True
except ImportError:
    HISTORY_ENABLED = False

try:
    from schedule import load_schedule, StationSchedule
    SCHEDULE_ENABLED = True
except ImportError:
    SCHEDULE_ENABLED = False

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "mac"))
from time_utils import station_now, station_from_timestamp, station_iso_now
import api_server as live_api

# Directories
TALK_SEGMENTS_DIR = PROJECT_ROOT / "output" / "talk_segments"
AI_BUMPERS_DIR = PROJECT_ROOT / "output" / "music_bumpers"

# Weekly schedule
DEFAULT_SCHEDULE_PATH = PROJECT_ROOT / "config" / "schedule.yaml"
SCHEDULE_PATH = Path(os.environ.get("WRIT_SCHEDULE_PATH", str(DEFAULT_SCHEDULE_PATH))).expanduser()

# Icecast config
ICECAST_HOST = os.environ.get("ICECAST_HOST", "localhost")
ICECAST_PORT = int(os.environ.get("ICECAST_PORT", "8000"))
ICECAST_MOUNT = os.environ.get("ICECAST_MOUNT", "/stream")
ICECAST_USER = os.environ.get("ICECAST_USER", "source")
ICECAST_PASS = os.environ.get("ICECAST_PASS", "hackme")

# Set WRIT_CONSUME_SEGMENTS=0 to keep talk segments on disk after playing (useful for testing).
CONSUME_SEGMENTS = os.environ.get("WRIT_CONSUME_SEGMENTS", "1").strip() not in ("0", "false", "no")
ICECAST_URL = f"icecast://{ICECAST_USER}:{ICECAST_PASS}@{ICECAST_HOST}:{ICECAST_PORT}{ICECAST_MOUNT}"
ICECAST_STATUS_URL = os.environ.get(
    "ICECAST_STATUS_URL",
    f"http://{ICECAST_HOST}:{ICECAST_PORT}/status-json.xsl",
)

# =============================================================================
# RUNTIME STATE
# =============================================================================

running = True
encoder_proc = None
skip_current = False
skip_was_requested = False
force_segment = False
last_bumper_path: Path | None = None
_duration_cache_lock = threading.Lock()
_duration_cache: dict[str, float | None] = {}
_live_queue_lock = threading.Lock()
_live_queue_state: dict = {
    "show_id": None,
    "show_name": None,
    "host": None,
    "current_item": None,
    "upcoming": [],
    "display_queue": [],
    "queue_length": 0,
    "updated_at": None,
}
current_track_info: dict = {
    "track": None,
    "type": None,
    "host": None,
    "segment_type": None,
    "show_id": None,
    "show": None,
    "listeners": 0,
}

# Command file
COMMAND_FILE = Path(
    os.environ.get("WRIT_COMMAND_FILE", str(PROJECT_ROOT / "command.txt"))
).expanduser()

# Now playing JSON
DEFAULT_NOW_PLAYING = PROJECT_ROOT / "output" / "now_playing.json"
NOW_PLAYING_PATHS = [DEFAULT_NOW_PLAYING]

env_now_playing = os.environ.get("WRIT_NOW_PLAYING_PATHS")
if env_now_playing:
    NOW_PLAYING_PATHS = [
        Path(p).expanduser() for p in env_now_playing.split(os.pathsep) if p
    ]
else:
    public_repo_path = (
        Path.home() / "GitHub" / "keltokhy.github.io" / "public" / "now_playing.json"
    )
    if public_repo_path.parent.exists():
        NOW_PLAYING_PATHS.append(public_repo_path)

NOW_PLAYING_PATHS = list(dict.fromkeys(NOW_PLAYING_PATHS))


# =============================================================================
# PROGRAM CONTEXT
# =============================================================================

@dataclass
class ContentLifecycle:
    """Play-count and age limits for retiring content."""
    max_plays: int | None = None   # None = unlimited
    max_days: int | None = None    # None = unlimited

    @classmethod
    def from_dict(cls, d: dict) -> "ContentLifecycle":
        return cls(
            max_plays=d.get("max_plays"),
            max_days=d.get("max_days"),
        )

    def is_unlimited(self) -> bool:
        return self.max_plays is None and self.max_days is None


def _plays_sidecar(audio_path: Path) -> Path:
    """Return the path of the play-count sidecar file for an audio file."""
    return audio_path.parent / (audio_path.name + ".plays.json")


def _read_plays(audio_path: Path) -> dict:
    """Read play metadata sidecar. Returns empty dict if missing or corrupt."""
    p = _plays_sidecar(audio_path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _record_play_lifecycle(audio_path: Path) -> dict:
    """Increment play count in sidecar. Returns updated metadata."""
    p = _plays_sidecar(audio_path)
    meta = _read_plays(audio_path)
    meta["play_count"] = meta.get("play_count", 0) + 1
    now = station_iso_now()
    if "created_at" not in meta:
        # Use file mtime as creation time if not yet set
        meta["created_at"] = station_from_timestamp(audio_path.stat().st_mtime).isoformat()
    if "first_played_at" not in meta:
        meta["first_played_at"] = now
    meta["last_played_at"] = now
    try:
        p.write_text(json.dumps(meta, indent=2))
    except Exception:
        pass
    return meta


def _should_retire(audio_path: Path, lifecycle: ContentLifecycle) -> bool:
    """Return True if this file has exceeded its play count or age limit."""
    if lifecycle.is_unlimited():
        return False
    meta = _read_plays(audio_path)
    if lifecycle.max_plays is not None:
        if meta.get("play_count", 0) >= lifecycle.max_plays:
            return True
    if lifecycle.max_days is not None:
        created_str = meta.get("created_at")
        if created_str:
            try:
                age_days = (station_now() - datetime.fromisoformat(created_str)).days
                if age_days >= lifecycle.max_days:
                    return True
            except Exception:
                pass
        else:
            # No sidecar yet — use file mtime as proxy age
            age_days = (station_now() - station_from_timestamp(audio_path.stat().st_mtime)).days
            if age_days >= lifecycle.max_days:
                return True
    return False


def _retire(audio_path: Path):
    """Delete an audio file and its sidecar."""
    try:
        audio_path.unlink()
    except Exception:
        pass
    sidecar = _plays_sidecar(audio_path)
    if sidecar.exists():
        try:
            sidecar.unlink()
        except Exception:
            pass


def _load_lifecycle(show_id: str, content_type: str) -> ContentLifecycle:
    """Load lifecycle config for a show/content-type from schedule.yaml."""
    try:
        import yaml
        with open(SCHEDULE_PATH) as f:
            data = yaml.safe_load(f)
        show = data.get("shows", {}).get(show_id, {})
        lc = show.get("content_lifecycle", {}).get(content_type, {})
        return ContentLifecycle.from_dict(lc)
    except Exception:
        return ContentLifecycle()


@dataclass
class ProgramContext:
    show_id: str
    show_name: str
    show_description: str
    host: str
    topic_focus: str
    segment_types: list[str]
    bumper_style: str
    voices: dict[str, str] = field(default_factory=dict)
    playback_sequence: dict[str, object] = field(default_factory=dict)
    talk_lifecycle: ContentLifecycle = field(default_factory=ContentLifecycle)
    music_lifecycle: ContentLifecycle = field(default_factory=ContentLifecycle)


def get_program_context(station_schedule=None) -> ProgramContext:
    """Resolve the current show/program from the schedule."""
    if station_schedule is None:
        raise RuntimeError("Station schedule is required")

    resolved = station_schedule.resolve()
    return ProgramContext(
        show_id=resolved.show_id,
        show_name=resolved.name,
        show_description=resolved.description,
        host=resolved.host,
        topic_focus=resolved.topic_focus,
        segment_types=resolved.segment_types,
        bumper_style=resolved.bumper_style,
        voices=dict(resolved.voices),
        playback_sequence=dict(resolved.playback_sequence),
        talk_lifecycle=_load_lifecycle(resolved.show_id, "talk"),
        music_lifecycle=_load_lifecycle(resolved.show_id, "music"),
    )


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================


def record_play(filepath: Path, name: str, vibe: str, show_id: str):
    """Record a track play in the history database."""
    if HISTORY_ENABLED:
        try:
            get_history().record_play(
                filepath=str(filepath),
                track_name=name,
                vibe=vibe,
                time_period=show_id,
                listeners=get_listener_count(),
            )
        except Exception:
            pass


def signal_handler(signum, frame):
    global running, encoder_proc
    signame = {2: "SIGINT", 15: "SIGTERM", 1: "SIGHUP"}.get(signum, f"SIG{signum}")
    log(f"Shutting down (received {signame})...")
    running = False
    _kill_encoder(encoder_proc)
    _release_instance_lock()
    sys.exit(0)


def log(msg: str):
    ts = station_now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


LISTENER_CACHE_SECONDS = 15
_last_listener_count = 0
_last_listener_check = 0.0


def get_listener_count() -> int:
    """Fetch listener count with a short cache."""
    global _last_listener_count, _last_listener_check
    now = time.time()
    if now - _last_listener_check < LISTENER_CACHE_SECONDS:
        return _last_listener_count

    _last_listener_check = now
    try:
        with urllib.request.urlopen(ICECAST_STATUS_URL, timeout=1.5) as resp:
            data = json.load(resp)
        source = data.get("icestats", {}).get("source", {})
        _last_listener_count = int(source.get("listeners", 0) or 0)
    except Exception:
        pass

    return _last_listener_count


def write_json_atomic(path: Path, payload: dict) -> None:
    """Write JSON atomically to avoid partial reads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload))
    tmp_path.replace(path)


def update_now_playing(
    track: str,
    track_type: str,
    show_id: str | None = None,
    show_name: str | None = None,
    host: str | None = None,
    segment_type: str | None = None,
    caption: str | None = None,
):
    """Update current track info in-memory and write to disk for external sync."""
    new_info = {
        "track": track,
        "type": track_type,
        "host": host,
        "segment_type": segment_type,
        "show_id": show_id,
        "show": show_name,
        "timestamp": station_iso_now(),
        "listeners": get_listener_count(),
    }
    if caption is not None:
        new_info["ai_generated"] = True
        new_info["caption"] = caption
    # Atomic-ish update: overwrite all keys at once (no clear() gap)
    current_track_info.update(new_info)
    for k in list(current_track_info):
        if k not in new_info:
            del current_track_info[k]
    for path in NOW_PLAYING_PATHS:
        try:
            write_json_atomic(path, current_track_info)
        except Exception:
            pass


def check_command() -> str | None:
    """Check for pending command."""
    try:
        if COMMAND_FILE.exists() and (cmd := COMMAND_FILE.read_text().strip()):
            COMMAND_FILE.write_text("")
            return cmd
    except Exception:
        pass
    try:
        cmd = live_api.pop_live_command({"skip", "segment"})
        if isinstance(cmd, dict):
            action = str(cmd.get("action", "")).strip().lower()
            if action:
                return action
    except Exception:
        pass
    return None


def _queue_item_duration(filepath: Path) -> float:
    duration = get_track_duration(filepath)
    return float(duration) if duration else 0.0


def _queue_item_snapshot(
    filepath: Path | None,
    index: int,
    total: int,
    started_at: datetime | None = None,
    *,
    kind: str = "speech",
    actionable: bool = True,
    label_override: str | None = None,
    name_override: str | None = None,
    segment_type_override: str | None = None,
    filename_override: str | None = None,
    path_override: str | None = None,
    duration_seconds_override: float | None = None,
) -> dict:
    duration = duration_seconds_override
    if duration is None and filepath is not None:
        duration = _queue_item_duration(filepath)
    snapshot = {
        "index": index,
        "total": total,
        "kind": kind,
        "actionable": actionable,
        "label": label_override if label_override is not None else (
            "Speaking segment" if kind == "speech" else
            "Intro" if kind == "intro" else
            "Outro" if kind == "outro" else
            "Station ID" if kind == "station_id" else
            "Music bumper slot" if kind == "bumper_slot" else
            kind.replace("_", " ").title()
        ),
        "filename": filename_override if filename_override is not None else (filepath.name if filepath is not None else None),
        "path": path_override if path_override is not None else (str(filepath) if filepath is not None else None),
        "name": name_override if name_override is not None else (clean_name(filepath, is_speech=True) if filepath is not None else "Planned item"),
        "segment_type": segment_type_override if segment_type_override is not None else (_extract_segment_type(filepath) if filepath is not None else kind),
        "duration_seconds": round(duration, 2) if duration else None,
    }
    if started_at is not None:
        snapshot["started_at"] = started_at.isoformat()
        if duration:
            snapshot["estimated_end_at"] = (started_at + timedelta(seconds=duration)).isoformat()
    return snapshot


def _placeholder_queue_item(
    label: str,
    index: int,
    total: int,
    *,
    segment_type: str,
    kind: str,
    duration_seconds: float | None = None,
) -> dict:
    return _queue_item_snapshot(
        None,
        index,
        total,
        kind=kind,
        actionable=False,
        label_override=label,
        name_override=label,
        segment_type_override=segment_type,
        duration_seconds_override=duration_seconds,
    )


def _build_display_queue(
    intro_seg: Path | None,
    main_segments: list[Path],
    outro_seg: Path | None,
    station_pool: list[Path],
    sequence: dict[str, object],
) -> list[dict]:
    display: list[dict] = []
    bumper_count = max(1, int(sequence.get("bumper_count_between_talk_segments", 1)))
    station_every = max(1, int(sequence.get("station_id_every_n_speaking_segments", 3)))
    station_before = bool(sequence.get("station_id_before_bumper", True))
    bumper_min_seconds = float(sequence.get("bumper_min_seconds", 15))
    bumper_max_seconds = float(sequence.get("bumper_max_seconds", bumper_min_seconds))

    station_idx = 0
    idx = 0

    if sequence.get("intro_enabled"):
        if intro_seg:
            display.append(_queue_item_snapshot(intro_seg, idx, 0, kind="intro", actionable=False, label_override="Show Intro"))
        else:
            display.append(
                _placeholder_queue_item(
                    "Show Intro (missing)",
                    idx,
                    0,
                    segment_type=str(sequence.get("intro_segment_type", "show_intro")),
                    kind="intro",
                )
            )
        idx += 1

    for speak_idx, seg in enumerate(main_segments, start=1):
        display.append(_queue_item_snapshot(seg, idx, 0, kind="speech", actionable=True, label_override="Speaking segment"))
        idx += 1
        should_continue = speak_idx < len(main_segments)
        if not should_continue:
            continue

        station_due = bool(station_pool) and (speak_idx % station_every == 0)
        if station_due and station_before:
            station_seg = station_pool[station_idx % len(station_pool)] if station_pool else None
            if station_seg:
                display.append(_queue_item_snapshot(station_seg, idx, 0, kind="station_id", actionable=False, label_override="Station ID"))
            else:
                display.append(
                    _placeholder_queue_item(
                        "Station ID (missing)",
                        idx,
                        0,
                        segment_type=str(sequence.get("station_id_segment_type", "station_id")),
                        kind="station_id",
                    )
                )
            idx += 1
            station_idx += 1

        for bumper_slot in range(bumper_count):
            label = "Music bumper" if bumper_count == 1 else f"Music bumper {bumper_slot + 1}"
            display.append(
                _placeholder_queue_item(
                    label,
                    idx,
                    0,
                    segment_type="bumper",
                    kind="bumper_slot",
                    duration_seconds=bumper_max_seconds if bumper_max_seconds else bumper_min_seconds,
                )
            )
            idx += 1

        if station_due and not station_before:
            station_seg = station_pool[station_idx % len(station_pool)] if station_pool else None
            if station_seg:
                display.append(_queue_item_snapshot(station_seg, idx, 0, kind="station_id", actionable=False, label_override="Station ID"))
            else:
                display.append(
                    _placeholder_queue_item(
                        "Station ID (missing)",
                        idx,
                        0,
                        segment_type=str(sequence.get("station_id_segment_type", "station_id")),
                        kind="station_id",
                    )
                )
            idx += 1
            station_idx += 1

    if sequence.get("outro_enabled"):
        if outro_seg:
            display.append(_queue_item_snapshot(outro_seg, idx, 0, kind="outro", actionable=False, label_override="Show Outro"))
        else:
            display.append(
                _placeholder_queue_item(
                    "Show Outro (missing)",
                    idx,
                    0,
                    segment_type=str(sequence.get("outro_segment_type", "show_outro")),
                    kind="outro",
                )
            )

    total = len(display)
    for item_index, item in enumerate(display):
        item["index"] = item_index
        item["total"] = total
    return display


def _resolve_queue_index(talk_queue: list[Path], ref: str, start_index: int = 0) -> int | None:
    if not ref:
        return None
    ref_name = Path(ref).name
    for idx in range(start_index, len(talk_queue)):
        item = talk_queue[idx]
        try:
            rel = str(item.relative_to(PROJECT_ROOT))
        except Exception:
            rel = ""
        if str(item) == ref or rel == ref or item.name == ref_name:
            return idx
    return None


def _apply_live_command_to_queue(command: dict, talk_queue: list[Path], current_index: int) -> int:
    action = str(command.get("action", "")).strip().lower()
    ref = str(command.get("path") or command.get("filename") or "").strip()
    if not action:
        return current_index
    upcoming_start = min(len(talk_queue), current_index + 1)

    if action in {"play_next", "insert_next", "enqueue_next", "add_next"}:
        target = _resolve_queue_index(talk_queue, ref, upcoming_start)
        if target is not None:
            item = talk_queue.pop(target)
            if target < upcoming_start:
                upcoming_start -= 1
            talk_queue.insert(upcoming_start, item)
            return current_index
        if ref:
            candidate = Path(ref)
            if not candidate.is_absolute():
                candidate = (PROJECT_ROOT / candidate).resolve()
            if candidate.exists():
                talk_queue.insert(upcoming_start, candidate)
        return current_index

    if action in {"move_up", "move_down", "move_top", "move_bottom", "remove"}:
        target = _resolve_queue_index(talk_queue, ref, upcoming_start)
        if target is None:
            return current_index
        if action == "remove":
            talk_queue.pop(target)
            return current_index
        if action == "move_up" and target > upcoming_start:
            talk_queue[target - 1], talk_queue[target] = talk_queue[target], talk_queue[target - 1]
        elif action == "move_down" and target < len(talk_queue) - 1:
            talk_queue[target + 1], talk_queue[target] = talk_queue[target], talk_queue[target + 1]
        elif action == "move_top" and target > upcoming_start:
            item = talk_queue.pop(target)
            talk_queue.insert(upcoming_start, item)
        elif action == "move_bottom" and target < len(talk_queue) - 1:
            item = talk_queue.pop(target)
            talk_queue.append(item)
        return current_index

    return current_index


def _drain_live_commands() -> list[dict]:
    commands = []
    movable = {"play_next", "insert_next", "enqueue_next", "add_next", "move_up", "move_down", "move_top", "move_bottom", "remove"}
    while True:
        try:
            cmd = live_api.pop_live_command(movable)
        except Exception:
            break
        if not isinstance(cmd, dict):
            break
        commands.append(cmd)
    return commands


def _publish_live_queue_state(
    show_id: str,
    show_name: str,
    host: str,
    talk_queue: list[Path],
    current_index: int | None = None,
    current_started_at: datetime | None = None,
    display_queue: list[dict] | None = None,
) -> None:
    upcoming = []
    current_item = None
    total = len(talk_queue)
    if current_index is not None and 0 <= current_index < total:
        current_start = current_started_at or station_now()
        current_item = _queue_item_snapshot(talk_queue[current_index], current_index, total, current_start)
        cursor = current_start
        for idx, item in enumerate(talk_queue[current_index + 1:current_index + 11], start=current_index + 1):
            upcoming.append(_queue_item_snapshot(item, idx, total, cursor))
            cursor = cursor + timedelta(seconds=_queue_item_duration(item))
    else:
        cursor = station_now()
        for idx, item in enumerate(talk_queue[:10]):
            upcoming.append(_queue_item_snapshot(item, idx, total, cursor))
            cursor = cursor + timedelta(seconds=_queue_item_duration(item))
    snapshot = {
        "show_id": show_id,
        "show_name": show_name,
        "host": host,
        "current_item": current_item,
        "upcoming": upcoming,
        "display_queue": list(display_queue or []),
        "queue_length": total,
        "updated_at": station_iso_now(),
    }
    with _live_queue_lock:
        _live_queue_state.clear()
        _live_queue_state.update(snapshot)


def get_live_queue_state() -> dict:
    with _live_queue_lock:
        state = dict(_live_queue_state)
        state["upcoming"] = list(_live_queue_state.get("upcoming", []))
        state["display_queue"] = list(_live_queue_state.get("display_queue", []))
        if _live_queue_state.get("current_item"):
            state["current_item"] = dict(_live_queue_state["current_item"])
        return state



def clean_name(filepath: Path, is_speech: bool = False) -> str:
    name = filepath.stem

    if is_speech:
        segment_types = {
            "listener_response": "Listener Mail",
            "deep_dive": "Deep Dive",
            "news_analysis": "Signal Report",
            "interview": "The Interview",
            "panel": "Crosswire",
            "story": "Story Hour",
            "reddit_storytelling": "Reddit Storytelling",
            "reddit_post": "Reddit Thread",
            "youtube": "YouTube",
            "listener_mailbag": "Listener Hours",
            "music_essay": "Sonic Essay",
            "station_id": "WRIT-FM",
            "show_intro": "Show Opening",
            "show_outro": "Show Closing",
            # Legacy types
            "long_talk": "The Operator Speaks",
            "music_history": "Sonic Archaeology",
            "late_night": "Late Night Transmission",
            "monologue": "Midnight Musings",
            "dedication": "For the Night Owls",
            "weather": "Conditions Unknown",
            "news": "Signals from Elsewhere",
            "poetry": "Verse from the Void",
        }
        for key, friendly in segment_types.items():
            if key in name.lower():
                return friendly
        return "Transmission"

    patterns = [
        r'\s*\(Official.*?\)', r'\s*\[Official.*?\]',
        r'\s*\(Full Album.*?\)', r'\s*\[Full Album.*?\]',
        r'\s*\(HD\)', r'\s*\[HD\]', r'\s*\(Audio\)', r'\s*\[Audio\]',
        r'\s*\(Lyrics\)', r'\s*\[Lyrics\]', r'\s*\(Visualizer\)',
        r'\s*\|.*$', r'\s*\u29f9.*$', r'_seg\d+_\d+$',
    ]
    for p in patterns:
        name = re.sub(p, '', name, flags=re.IGNORECASE)
    return name.strip()


def get_track_duration(filepath: Path) -> float | None:
    """Get track duration in seconds using ffprobe."""
    cache_key = str(filepath)
    with _duration_cache_lock:
        if cache_key in _duration_cache:
            return _duration_cache[cache_key]
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(filepath)],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            duration = float(result.stdout.strip())
            with _duration_cache_lock:
                _duration_cache[cache_key] = duration
            return duration
    except Exception:
        pass
    with _duration_cache_lock:
        _duration_cache[cache_key] = None
    return None


# =============================================================================
# TALK SEGMENT MANAGEMENT
# =============================================================================

def get_talk_segments(show_id: str, lifecycle: ContentLifecycle | None = None) -> list[Path]:
    """Load pre-generated talk segments for a show.

    Listener responses are sorted to the front so they air first.
    Files that exceed their lifecycle limits are retired (deleted) immediately.
    """
    show_dir = TALK_SEGMENTS_DIR / show_id
    if not show_dir.exists():
        return []

    if lifecycle is None:
        lifecycle = _load_lifecycle(show_id, "talk")

    audio_exts = {".wav", ".mp3", ".flac"}
    all_files = sorted(
        (f for f in show_dir.iterdir() if f.is_file() and f.suffix.lower() in audio_exts),
        key=lambda p: p.stat().st_mtime,
    )

    segments = []
    for f in all_files:
        if _should_retire(f, lifecycle):
            plays = _read_plays(f).get("play_count", 0)
            log(f"  Retiring {f.name} (played {plays}×, lifecycle exceeded)")
            _retire(f)
        else:
            segments.append(f)

    # Prioritize listener responses — they should air before other talk segments
    listener_responses = [s for s in segments if "listener_response" in s.name]
    other_segments = [s for s in segments if "listener_response" not in s.name]
    return listener_responses + other_segments


def _segment_type_pool(segments: list[Path]) -> dict[str, list[Path]]:
    pools: dict[str, list[Path]] = {}
    for seg in segments:
        pools.setdefault(_extract_segment_type(seg), []).append(seg)
    return pools


def _pick_segment_from_pool(pool: list[Path], *, prefer_latest: bool = True) -> Path | None:
    if not pool:
        return None
    if len(pool) == 1:
        return pool[0]
    if prefer_latest:
        return max(pool, key=lambda p: p.stat().st_mtime)
    return random.choice(pool)


def get_listener_responses(show_id: str) -> list[Path]:
    """Check for new listener response segments (for mid-queue injection)."""
    show_dir = TALK_SEGMENTS_DIR / show_id
    if not show_dir.exists():
        return []
    return sorted(
        (f for f in show_dir.glob("listener_response_*.wav")),
        key=lambda p: p.stat().st_mtime,
    )


def select_ai_bumper(
    show_id: str,
    exclude: set[Path] | None = None,
    lifecycle: ContentLifecycle | None = None,
    *,
    max_seconds: float | None = None,
) -> tuple[Path, float, float, str | None, str | None] | None:
    """Pick a pre-generated AI music bumper for the current show.

    Returns (path, start_time, duration, caption, display_name) or None if unavailable.
    """
    show_dir = AI_BUMPERS_DIR / show_id
    if not show_dir.exists():
        return None

    if lifecycle is None:
        lifecycle = _load_lifecycle(show_id, "music")

    # Retire any files that have exceeded their lifecycle before building candidates
    raw_files = [f for f in show_dir.iterdir()
                 if f.is_file() and f.suffix.lower() in {".flac", ".mp3", ".wav"}]
    audio_files = []
    for f in raw_files:
        if _should_retire(f, lifecycle):
            plays = _read_plays(f).get("play_count", 0)
            log(f"  Retiring bumper {f.name} (played {plays}×)")
            _retire(f)
        else:
            audio_files.append(f)

    if not audio_files:
        return None

    # Filter recently played if history available
    candidates = audio_files
    if HISTORY_ENABLED:
        try:
            history = get_history()
            fresh = history.filter_recent(audio_files, hours=4)
            if fresh:
                candidates = fresh
        except Exception:
            pass

    # Exclude tracks already played in this set + the last bumper played
    skip = set(exclude) if exclude else set()
    if last_bumper_path is not None:
        skip.add(last_bumper_path)
    if skip:
        candidates = [c for c in candidates if c not in skip]
        if not candidates:
            return None

    track = random.choice(candidates)
    duration = get_track_duration(track)
    if max_seconds is not None:
        if duration is None:
            duration = max_seconds
        else:
            duration = min(duration, max_seconds)

    caption = None
    display_name = None
    meta_path = track.with_suffix(".json")
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            caption = meta.get("caption")
            display_name = meta.get("display_name")
        except Exception:
            pass

    return (track, 0.0, duration or 90.0, caption, display_name)


def _sequence_defaults() -> dict[str, object]:
    return {
        "intro_enabled": True,
        "intro_segment_type": "show_intro",
        "outro_enabled": True,
        "outro_segment_type": "show_outro",
        "station_id_enabled": True,
        "station_id_segment_type": "station_id",
        "station_id_every_n_speaking_segments": 3,
        "station_id_before_bumper": True,
        "bumper_count_between_talk_segments": 1,
        "bumper_min_seconds": 15,
        "bumper_max_seconds": 30,
        "bumper_fade_seconds": 4,
    }


def _normalized_sequence(sequence: dict | None) -> dict[str, object]:
    defaults = _sequence_defaults()
    raw = sequence if isinstance(sequence, dict) else {}
    out = dict(defaults)
    for key, value in raw.items():
        if key in {"intro_enabled", "outro_enabled", "station_id_enabled", "station_id_before_bumper"}:
            out[key] = bool(value)
        elif key in {
            "intro_segment_type",
            "outro_segment_type",
            "station_id_segment_type",
        }:
            value = str(value).strip()
            if value:
                out[key] = value
        elif key in {
            "station_id_every_n_speaking_segments",
            "bumper_count_between_talk_segments",
            "bumper_min_seconds",
            "bumper_max_seconds",
            "bumper_fade_seconds",
        }:
            try:
                out[key] = max(0 if "fade" in key else 1, int(value))
            except Exception:
                pass
    if int(out["bumper_max_seconds"]) < int(out["bumper_min_seconds"]):
        out["bumper_max_seconds"] = out["bumper_min_seconds"]
    if int(out["bumper_count_between_talk_segments"]) < 1:
        out["bumper_count_between_talk_segments"] = 1
    if int(out["station_id_every_n_speaking_segments"]) < 1:
        out["station_id_every_n_speaking_segments"] = 1
    return out


# =============================================================================
# AUDIO PIPELINE (unchanged from original)
# =============================================================================

def decode_to_pcm(
    filepath: Path,
    start_time: float = 0,
    duration: float = None,
    is_speech: bool = False,
    fade_seconds: float = 0.0,
) -> subprocess.Popen:
    """Decode audio file to raw PCM, output to stdout."""
    cmd = ["ffmpeg", "-v", "warning"]

    if start_time > 0:
        cmd.extend(["-ss", str(start_time)])

    cmd.extend(["-i", str(filepath)])

    if duration is not None:
        cmd.extend(["-t", str(duration)])

    # Speech gets louder normalization (-14 LUFS vs -16 for music)
    if is_speech:
        filters = ["loudnorm=I=-14:TP=-1.5:LRA=7"]
    else:
        filters = ["loudnorm=I=-16:TP=-1.5:LRA=11"]

    if fade_seconds > 0:
        total_duration = duration
        if total_duration is None:
            total_duration = get_track_duration(filepath)
        try:
            fade = min(float(fade_seconds), max(0.0, float(total_duration) / 2 if total_duration else float(fade_seconds)))
        except Exception:
            fade = float(fade_seconds)
        if fade > 0:
            filters.append(f"afade=t=in:st=0:d={fade}")
            if total_duration is not None and total_duration > fade:
                fade_out_start = max(0.0, float(total_duration) - fade)
                filters.append(f"afade=t=out:st={fade_out_start}:d={fade}")

    filters.append("aresample=44100")

    cmd.extend([
        "-vn",
        "-af", ",".join(filters),
        "-f", "s16le",
        "-acodec", "pcm_s16le",
        "-ar", "44100",
        "-ac", "2",
        "-"
    ])

    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL
    )


def start_encoder() -> subprocess.Popen:
    """Start persistent ffmpeg encoder to Icecast."""
    return subprocess.Popen(
        [
            "ffmpeg", "-v", "warning",
            "-re",
            "-f", "s16le",
            "-ar", "44100",
            "-ac", "2",
            "-i", "-",
            "-acodec", "libmp3lame",
            "-b:a", "96k",
            "-content_type", "audio/mpeg",
            "-ice_name", "WRIT-FM",
            "-ice_description", "The frequency between frequencies",
            "-ice_genre", "Talk Radio",
            "-f", "mp3",
            ICECAST_URL
        ],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE
    )


def wait_for_encoder_ready(encoder: subprocess.Popen, timeout: float = 2.0) -> bool:
    """Wait briefly to ensure encoder connected."""
    time.sleep(0.3)
    if encoder.poll() is not None:
        try:
            stderr = encoder.stderr.read().decode() if encoder.stderr else ""
            if stderr:
                log(f"Encoder error: {stderr[:200]}")
        except Exception:
            pass
        return False
    return True


def pipe_track(
    filepath: Path,
    encoder: subprocess.Popen,
    start_time: float = 0,
    duration: float = None,
    is_speech: bool = False,
    fade_seconds: float = 0.0,
    expected_show_id: str | None = None,
    station_schedule=None,
) -> str:
    """Decode a track and pipe PCM to encoder.

    Returns:
        ok, skip, cutover, or failed.
    """
    global running, skip_current, skip_was_requested, force_segment

    if not running or encoder.poll() is not None:
        return "failed"

    decoder = None
    next_schedule_check_at = 0.0
    next_command_check_at = 0.0
    try:
        decoder = decode_to_pcm(filepath, start_time, duration, is_speech=is_speech, fade_seconds=fade_seconds)

        while running and not skip_current:
            now = time.monotonic()
            if expected_show_id and station_schedule is not None and now >= next_schedule_check_at:
                next_schedule_check_at = now + 1.0
                try:
                    new_ctx = get_program_context(station_schedule)
                    if new_ctx.show_id != expected_show_id:
                        log(f"Schedule changed to {new_ctx.show_name} - cutting over now...")
                        return "cutover"
                except Exception:
                    pass
            chunk = decoder.stdout.read(8192)
            if not chunk:
                break
            try:
                encoder.stdin.write(chunk)
                encoder.stdin.flush()
            except BrokenPipeError:
                try:
                    stderr = encoder.stderr.read().decode() if encoder.stderr else ""
                    if stderr:
                        log(f"Encoder pipe broke: {stderr[:200]}")
                except Exception:
                    pass
                return "failed"

            if now >= next_command_check_at:
                next_command_check_at = now + 0.2
                cmd = check_command()
                if cmd == "skip":
                    log("Skipping...")
                    skip_current = True
                    skip_was_requested = True
                    break
                elif cmd == "segment":
                    log("Will play segment next...")
                    force_segment = True

        if skip_was_requested:
            return "skip"
        return "ok"

    except Exception as e:
        log(f"Error piping {filepath.name}: {e}")
        return "failed"
    finally:
        if decoder:
            try:
                decoder.kill()
                decoder.wait(timeout=1)
            except Exception:
                pass
        if skip_current:
            skip_current = False


# =============================================================================
# MAIN LOOP - TALK FIRST
# =============================================================================

def _kill_encoder(proc: subprocess.Popen | None) -> None:
    """Terminate an encoder process and close its stdin to release the Icecast mount."""
    if proc is None:
        return
    try:
        if proc.stdin:
            proc.stdin.close()
    except Exception:
        pass
    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass


import fcntl as _fcntl

# PID lock file — ensures only one streamer owns the Icecast mount at a time.
# We hold an exclusive flock on this file for the entire lifetime of the process.
# A newer instance trying to acquire the lock will block until this process exits,
# guaranteeing clean handoff rather than racing kills.
_PIDLOCK_PATH = Path("/run/writ-fm.pid")
_pidlock_fd = None


def _acquire_instance_lock() -> bool:
    """Acquire exclusive ownership of the PID lockfile.

    Blocks until any prior instance releases the lock (i.e. exits cleanly).
    Returns True on success. Does NOT block indefinitely — the old instance
    has TimeoutStopSec=30 to exit; if the lock is not acquired within 35 s we
    bail so systemd can retry.
    """
    global _pidlock_fd
    import time as _time

    deadline = _time.monotonic() + 35
    try:
        fd = open(_PIDLOCK_PATH, "w")
    except Exception as e:
        log(f"Warning: cannot open PID lock file: {e}")
        return True  # Non-fatal — proceed without the lock

    # Non-blocking first attempt; if it fails, busy-wait with retries.
    while True:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            # Success — write our PID and keep the fd open to hold the lock
            fd.write(str(os.getpid()) + "\n")
            fd.flush()
            _pidlock_fd = fd
            return True
        except BlockingIOError:
            if _time.monotonic() >= deadline:
                log("Warning: PID lock timeout — another instance may still be running")
                fd.close()
                return False
            _time.sleep(0.2)
        except Exception as e:
            log(f"Warning: PID lock error: {e}")
            fd.close()
            return True  # Non-fatal


def _release_instance_lock() -> None:
    global _pidlock_fd
    if _pidlock_fd is not None:
        try:
            _fcntl.flock(_pidlock_fd, _fcntl.LOCK_UN)
            _pidlock_fd.close()
        except Exception:
            pass
        _pidlock_fd = None


def _evict_orphaned_encoder() -> None:
    """Kill any ffmpeg process still holding our Icecast mount from a crashed run.

    We only evict orphaned ENCODERS (ffmpeg), never competing Python instances.
    Mutual process-killing between two Python instances creates a death loop;
    the PID lockfile (above) is the correct mechanism for instance exclusivity.
    """
    import signal as _signal
    mount_marker = f"@{ICECAST_HOST}:{ICECAST_PORT}{ICECAST_MOUNT}"
    killed_any = False
    try:
        result = subprocess.run(
            ["pgrep", "-f", mount_marker],
            capture_output=True, text=True
        )
        for p in result.stdout.split():
            pid = int(p)
            if pid == os.getpid():
                continue
            try:
                os.kill(pid, _signal.SIGTERM)
                log(f"Evicted stale Icecast source (pid {pid})")
                killed_any = True
            except ProcessLookupError:
                pass
    except Exception:
        pass
    if killed_any:
        time.sleep(2)  # Wait for Icecast to register the disconnection


def run():
    global running, encoder_proc, force_segment, last_bumper_path, skip_was_requested

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    log("=== WRIT-FM Talk Radio Streamer ===")
    log(f"Talk segments: {TALK_SEGMENTS_DIR}")
    log(f"AI bumpers: {AI_BUMPERS_DIR}")
    log(f"Streaming to: {ICECAST_URL}")

    # Block until any previously running instance exits cleanly.
    # This prevents two simultaneous starts (e.g. systemd double-start on
    # reset-failed) from racing over the Icecast mount.
    _acquire_instance_lock()

    # Kill any orphaned encoder ffmpeg left over from an unclean shutdown
    _evict_orphaned_encoder()

    # Load schedule
    if not SCHEDULE_ENABLED:
        raise RuntimeError("Schedule module is unavailable")
    if not SCHEDULE_PATH.exists():
        raise FileNotFoundError(f"Schedule file not found: {SCHEDULE_PATH}")

    station_schedule = load_schedule(SCHEDULE_PATH)
    log(f"Loaded schedule with {len(station_schedule.shows)} shows")

    # Start embedded API server
    from api_server import start_api_thread
    start_api_thread(current_track_info, lambda: encoder_proc, get_listener_count, queue_getter=get_live_queue_state)
    log("API server started on port 8001")

    # Count talk segments
    _seg_exts = {".wav", ".mp3", ".flac"}
    talk_count = 0
    if TALK_SEGMENTS_DIR.exists():
        for show_dir in TALK_SEGMENTS_DIR.iterdir():
            if show_dir.is_dir():
                c = sum(1 for f in show_dir.iterdir() if f.is_file() and f.suffix.lower() in _seg_exts)
                talk_count += c
                if c > 0:
                    log(f"  {show_dir.name}: {c} segments")
    log(f"Total talk segments: {talk_count}")

    # Count AI music bumpers
    bumper_count = 0
    if AI_BUMPERS_DIR.exists():
        audio_exts = {".flac", ".mp3", ".wav"}
        for show_dir in AI_BUMPERS_DIR.iterdir():
            if show_dir.is_dir():
                c = sum(1 for f in show_dir.iterdir() if f.suffix.lower() in audio_exts)
                bumper_count += c
    if bumper_count > 0:
        log(f"AI music bumpers: {bumper_count} (will use instead of local music)")
    else:
        log("AI music bumpers: none")

    while running:
        # Explicitly kill previous encoder before starting a new one.
        # Without this, the old process keeps the Icecast mount and the new
        # one gets 403 Forbidden on every reconnect attempt.
        _kill_encoder(encoder_proc)
        encoder_proc = None

        log("Starting encoder...")
        encoder_proc = start_encoder()

        if not wait_for_encoder_ready(encoder_proc):
            log("Encoder failed to connect, retrying in 10s...")
            time.sleep(10)
            continue

        log("Encoder connected to Icecast")

        while running and encoder_proc.poll() is None:
            # Get current program context
            ctx = get_program_context(station_schedule)

            log(f"Show: {ctx.show_name} ({ctx.show_id})")
            log(f"  Host: {ctx.host}")

            # Get talk segments for this show and split out show-level specials.
            sequence = _normalized_sequence(ctx.playback_sequence)
            intro_type = str(sequence["intro_segment_type"])
            outro_type = str(sequence["outro_segment_type"])
            station_type = str(sequence["station_id_segment_type"])
            special_types = {t for t in {intro_type, outro_type, station_type} if t}

            all_talk_segments = get_talk_segments(ctx.show_id, ctx.talk_lifecycle)
            pools = _segment_type_pool(all_talk_segments)
            intro_seg = _pick_segment_from_pool(pools.get(intro_type, []), prefer_latest=True) if sequence["intro_enabled"] else None
            outro_seg = _pick_segment_from_pool(pools.get(outro_type, []), prefer_latest=True) if sequence["outro_enabled"] else None
            station_pool = list(pools.get(station_type, [])) if sequence["station_id_enabled"] else []

            main_segments = [s for s in all_talk_segments if _extract_segment_type(s) not in special_types]
            lr_count = sum(1 for s in main_segments if "listener_response" in s.name)
            if lr_count < len(main_segments):
                priority = main_segments[:lr_count]
                rest = main_segments[lr_count:]
                random.shuffle(rest)
                main_segments = priority + rest
            display_queue = _build_display_queue(intro_seg, main_segments, outro_seg, station_pool, sequence)

            if main_segments or intro_seg or outro_seg:
                log(f"  Talk queue: {len(main_segments)} speaking segments")
                if intro_seg:
                    log(f"  Intro available: {clean_name(intro_seg, is_speech=True)}")
                if outro_seg:
                    log(f"  Outro available: {clean_name(outro_seg, is_speech=True)}")
                if station_pool and sequence["station_id_enabled"]:
                    log(f"  Station IDs queued: {len(station_pool)}")
                if lr_count:
                    log(f"  Listener responses queued: {lr_count} (priority)")

                bumper_fade = float(sequence["bumper_fade_seconds"])
                bumper_min_seconds = float(sequence["bumper_min_seconds"])
                bumper_max_seconds = float(sequence["bumper_max_seconds"])
                bumper_count = max(1, int(sequence["bumper_count_between_talk_segments"]))
                station_every = max(1, int(sequence["station_id_every_n_speaking_segments"]))
                station_before = bool(sequence["station_id_before_bumper"])

                def _play_speech_item(track: Path, item_type: str, *, label: str) -> bool:
                    seg_name = clean_name(track, is_speech=True)
                    log(f"  {label}: {seg_name}")
                    update_now_playing(
                        seg_name,
                        "talk",
                        show_id=ctx.show_id,
                        show_name=ctx.show_name,
                        host=ctx.host,
                        segment_type=item_type,
                    )
                    status = pipe_track(
                        track,
                        encoder_proc,
                        is_speech=True,
                        expected_show_id=ctx.show_id,
                        station_schedule=station_schedule,
                    )
                    if status == "cutover":
                        return "cutover"
                    if status == "failed":
                        log(f"{label} pipe failed, reconnecting...")
                        return "failed"
                    if skip_was_requested:
                        log(f"  {label} skipped by live control")
                        return "skip"
                    if CONSUME_SEGMENTS:
                        meta = _record_play_lifecycle(track)
                        plays = meta.get("play_count", 1)
                        if _should_retire(track, ctx.talk_lifecycle):
                            log(f"    (retired after {plays} play{'s' if plays != 1 else ''})")
                            _retire(track)
                        else:
                            lc = ctx.talk_lifecycle
                            remaining_plays = (lc.max_plays - plays) if lc.max_plays else "∞"
                            log(f"    (play {plays}, {remaining_plays} plays remaining)")
                    else:
                        _record_play_lifecycle(track)
                        log("    (kept — consume disabled)")
                    record_play(track, seg_name, "talk", ctx.show_id)
                    return "ok"

                def _play_bumper_item(track: Path, duration: float, caption: str | None, display_name: str | None) -> bool:
                    bname = display_name or "AI Music"
                    log(f"  MUSIC: {bname} ({int(duration)}s)")
                    if caption:
                        log(f"    {caption[:70]}...")
                    update_now_playing(
                        bname,
                        "bumper",
                        show_id=ctx.show_id,
                        show_name=ctx.show_name,
                        caption=caption,
                    )
                    status = pipe_track(
                        track,
                        encoder_proc,
                        duration=duration,
                        fade_seconds=bumper_fade,
                        expected_show_id=ctx.show_id,
                        station_schedule=station_schedule,
                    )
                    if status == "cutover":
                        return "cutover"
                    if status == "failed":
                        log("Music pipe failed, continuing...")
                        return "failed"
                    if skip_was_requested:
                        log("  Bumper skipped by live control")
                        return "skip"
                    record_play(track, bname, "ai_bumper", ctx.show_id)
                    if CONSUME_SEGMENTS:
                        bmeta = _record_play_lifecycle(track)
                        bplays = bmeta.get("play_count", 1)
                        if _should_retire(track, ctx.music_lifecycle):
                            log(f"    (bumper retired after {bplays} play{'s' if bplays != 1 else ''})")
                            _retire(track)
                        else:
                            blc = ctx.music_lifecycle
                            brem = (blc.max_plays - bplays) if blc.max_plays else "∞"
                            log(f"    (bumper play {bplays}, {brem} remaining)")
                    else:
                        _record_play_lifecycle(track)
                    return "ok"

                def _play_special_end(track: Path, item_type: str, label: str) -> bool:
                    return _play_speech_item(track, item_type, label=label)

                speak_index = 0
                cutover_to_next_show = False
                if intro_seg:
                    intro_status = _play_speech_item(intro_seg, intro_type, label="INTRO")
                    if intro_status == "cutover":
                        cutover_to_next_show = True
                    elif intro_status == "failed":
                        break
                    if skip_was_requested:
                        skip_was_requested = False
                    intro_seg = None

                if cutover_to_next_show:
                    log("Cutover complete, refreshing for the next show...")
                    continue

                _publish_live_queue_state(ctx.show_id, ctx.show_name, ctx.host, main_segments, 0, display_queue=display_queue)
                while running and encoder_proc.poll() is None and speak_index < len(main_segments):
                    for cmd in _drain_live_commands():
                        speak_index = _apply_live_command_to_queue(cmd, main_segments, speak_index)
                    _publish_live_queue_state(ctx.show_id, ctx.show_name, ctx.host, main_segments, speak_index, display_queue=display_queue)
                    talk_seg = main_segments[speak_index]
                    if not running or encoder_proc.poll() is not None:
                        break

                    new_ctx = get_program_context(station_schedule)
                    if new_ctx.show_id != ctx.show_id:
                        log(f"Show changed to {new_ctx.show_name} - switching...")
                        cutover_to_next_show = True
                        break

                    talk_status = _play_speech_item(talk_seg, _extract_segment_type(talk_seg), label="TALK")
                    if talk_status == "cutover":
                        cutover_to_next_show = True
                        break
                    if talk_status == "failed":
                        break
                    if skip_was_requested:
                        skip_was_requested = False
                        speak_index += 1
                        _publish_live_queue_state(ctx.show_id, ctx.show_name, ctx.host, main_segments, speak_index, display_queue=display_queue)
                        continue

                    speak_index += 1
                    _publish_live_queue_state(ctx.show_id, ctx.show_name, ctx.host, main_segments, speak_index, display_queue=display_queue)

                    if not running or encoder_proc.poll() is not None:
                        break

                    should_play_bumper = speak_index < len(main_segments)
                    station_due = sequence["station_id_enabled"] and (speak_index % station_every == 0)

                    if station_due and station_before and should_play_bumper and station_pool:
                        station_track = _pick_segment_from_pool(station_pool, prefer_latest=False)
                        if station_track:
                            station_status = _play_speech_item(station_track, station_type, label="STATION ID")
                            if station_status == "cutover":
                                cutover_to_next_show = True
                                break
                            if station_status == "failed":
                                break
                        if skip_was_requested:
                            skip_was_requested = False

                    if should_play_bumper:
                        set_count = 0
                        while running and encoder_proc.poll() is None and set_count < bumper_count:
                            ai_bumper = select_ai_bumper(
                                ctx.show_id,
                                max_seconds=bumper_max_seconds,
                            )
                            if not ai_bumper:
                                if set_count == 0:
                                    log("  No AI bumpers available, skipping break")
                                break

                            bpath, bstart, bdur, bcaption, bdisplay = ai_bumper
                            set_count += 1
                            bumper_status = _play_bumper_item(bpath, bdur, bcaption, bdisplay)
                            if bumper_status == "cutover":
                                cutover_to_next_show = True
                                break
                            if bumper_status == "failed":
                                break
                            if skip_was_requested:
                                skip_was_requested = False
                                break
                            last_bumper_path = bpath

                        if set_count > 0:
                            log(f"  Music set: {set_count} track{'s' if set_count != 1 else ''}")

                        if station_due and not station_before and station_pool:
                            station_track = _pick_segment_from_pool(station_pool, prefer_latest=False)
                            if station_track:
                                station_status = _play_speech_item(station_track, station_type, label="STATION ID")
                                if station_status == "cutover":
                                    cutover_to_next_show = True
                                    break
                                if station_status == "failed":
                                    break
                            if skip_was_requested:
                                skip_was_requested = False

                if cutover_to_next_show:
                    log("Cutover complete, refreshing for the next show...")
                    continue

                if outro_seg and running and encoder_proc.poll() is None:
                    outro_status = _play_special_end(outro_seg, outro_type, "OUTRO")
                    if outro_status == "cutover":
                        cutover_to_next_show = True
                    elif outro_status == "failed":
                        break
                    if skip_was_requested:
                        skip_was_requested = False

                if cutover_to_next_show:
                    log("Cutover complete, refreshing for the next show...")
                    continue

            else:
                # No talk segments — play AI bumpers if available, otherwise fallback tone
                ai_bumper = select_ai_bumper(ctx.show_id, max_seconds=float(sequence["bumper_max_seconds"]))
                if ai_bumper:
                    bpath, bstart, bdur, bcaption, bdisplay = ai_bumper
                    bname = bdisplay or clean_name(bpath)
                    log(f"  No talk segments — playing bumper: {bname}")
                    update_now_playing(
                        bname, "bumper",
                        show_id=ctx.show_id,
                        show_name=ctx.show_name,
                        host=None,
                        caption=bcaption,
                    )
                    bumper_status = pipe_track(
                        bpath,
                        encoder_proc,
                        start_time=bstart,
                        duration=bdur,
                        fade_seconds=float(sequence["bumper_fade_seconds"]),
                        expected_show_id=ctx.show_id,
                        station_schedule=station_schedule,
                    )
                    if bumper_status == "cutover":
                        log("Cutover complete, refreshing for the next show...")
                        continue
                    if skip_was_requested:
                        log("  Bumper skipped by live control")
                        skip_was_requested = False
                        continue
                    record_play(bpath, bname, "ai_bumper", ctx.show_id)
                    last_bumper_path = bpath
                else:
                    log(f"  No talk segments for {ctx.show_id}; piping fallback tone to maintain stream")
                    fallback_tone = Path("/root/writ-fm/output/fallback_tone.wav")
                    if fallback_tone.exists():
                        update_now_playing(
                            "Holding Pattern", "bumper",
                            show_id=ctx.show_id,
                            show_name=ctx.show_name,
                            caption="Signal lost... awaiting data.",
                        )
                        tone_status = pipe_track(
                            fallback_tone,
                            encoder_proc,
                            expected_show_id=ctx.show_id,
                            station_schedule=station_schedule,
                        )
                        if tone_status == "cutover":
                            log("Cutover complete, refreshing for the next show...")
                            continue
                    else:
                        time.sleep(10)

            if running and encoder_proc.poll() is None:
                log("Queue complete, refreshing...")

        if running:
            log("Encoder died, restarting...")
            time.sleep(2)

    log("=== Stream stopped ===")


def _extract_segment_type(filepath: Path) -> str:
    """Extract segment type from filename."""
    name = filepath.name.lower()
    types = [
        "listener_response",  # Priority: real listener messages
        "deep_dive", "news_analysis", "interview", "panel", "story", "reddit_storytelling", "reddit_post", "youtube",
        "listener_mailbag", "music_essay", "station_id", "show_intro", "show_outro",
        # Legacy
        "long_talk", "monologue", "late_night", "music_history",
        "dedication", "weather", "news", "poetry",
    ]
    for t in types:
        if t in name:
            return t
    return "talk"


if __name__ == "__main__":
    run()
