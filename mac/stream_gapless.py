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
from pathlib import Path
from datetime import datetime
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
force_segment = False
last_bumper_path: Path | None = None
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
    now = datetime.now().isoformat()
    if "created_at" not in meta:
        # Use file mtime as creation time if not yet set
        meta["created_at"] = datetime.fromtimestamp(audio_path.stat().st_mtime).isoformat()
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
                age_days = (datetime.now() - datetime.fromisoformat(created_str)).days
                if age_days >= lifecycle.max_days:
                    return True
            except Exception:
                pass
        else:
            # No sidecar yet — use file mtime as proxy age
            age_days = (datetime.now() - datetime.fromtimestamp(audio_path.stat().st_mtime)).days
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
    ts = datetime.now().strftime("%H:%M:%S")
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
        "timestamp": datetime.now().isoformat(),
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
    return None



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
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(filepath)],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
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


def get_listener_responses(show_id: str) -> list[Path]:
    """Check for new listener response segments (for mid-queue injection)."""
    show_dir = TALK_SEGMENTS_DIR / show_id
    if not show_dir.exists():
        return []
    return sorted(
        (f for f in show_dir.glob("listener_response_*.wav")),
        key=lambda p: p.stat().st_mtime,
    )


def select_ai_bumper(show_id: str, exclude: set[Path] | None = None,
                     lifecycle: ContentLifecycle | None = None) -> tuple[Path, float, float, str | None, str | None] | None:
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


# =============================================================================
# AUDIO PIPELINE (unchanged from original)
# =============================================================================

def decode_to_pcm(filepath: Path, start_time: float = 0, duration: float = None, is_speech: bool = False) -> subprocess.Popen:
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

    # Fade in/out for music bumpers only
    if not is_speech:
        filters.append("afade=t=in:st=0:d=8")
        if duration is not None and duration > 16:
            fade_out_start = max(0, duration - 8)
            filters.append(f"afade=t=out:st={fade_out_start}:d=8")

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


def pipe_track(filepath: Path, encoder: subprocess.Popen, start_time: float = 0, duration: float = None, is_speech: bool = False) -> bool:
    """Decode a track and pipe PCM to encoder. Returns False if encoder died."""
    global running, skip_current, force_segment

    if not running or encoder.poll() is not None:
        return False

    decoder = None
    try:
        decoder = decode_to_pcm(filepath, start_time, duration, is_speech=is_speech)

        while running and not skip_current:
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
                return False

            cmd = check_command()
            if cmd == "skip":
                log("Skipping...")
                skip_current = True
                break
            elif cmd == "segment":
                log("Will play segment next...")
                force_segment = True

        return True

    except Exception as e:
        log(f"Error piping {filepath.name}: {e}")
        return False
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
    global running, encoder_proc, force_segment, last_bumper_path

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
    start_api_thread(current_track_info, lambda: encoder_proc, get_listener_count)
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
            log(f"  Host: {ctx.host} | Focus: {ctx.topic_focus}")

            # Get talk segments for this show
            talk_queue = get_talk_segments(ctx.show_id, ctx.talk_lifecycle)
            # Listener responses are already sorted to front; shuffle only the rest
            lr_count = sum(1 for s in talk_queue if "listener_response" in s.name)
            if lr_count < len(talk_queue):
                priority = talk_queue[:lr_count]
                rest = talk_queue[lr_count:]
                random.shuffle(rest)
                talk_queue = priority + rest

            if talk_queue:
                log(f"  Talk queue: {len(talk_queue)} segments")
                if lr_count:
                    log(f"  Listener responses queued: {lr_count} (priority)")

                for talk_seg in talk_queue:
                    if not running or encoder_proc.poll() is not None:
                        break

                    # Check if show changed
                    new_ctx = get_program_context(station_schedule)
                    if new_ctx.show_id != ctx.show_id:
                        log(f"Show changed to {new_ctx.show_name} - switching...")
                        break

                    # Play talk segment
                    seg_name = clean_name(talk_seg, is_speech=True)
                    seg_type = _extract_segment_type(talk_seg)
                    log(f"  TALK: {seg_name}")
                    update_now_playing(
                        seg_name, "talk",
                        show_id=ctx.show_id,
                        show_name=ctx.show_name,
                        host=ctx.host,
                        segment_type=seg_type,
                    )

                    if not pipe_track(talk_seg, encoder_proc, is_speech=True):
                        log("Talk pipe failed, reconnecting...")
                        break

                    # Update play count and retire if lifecycle limits exceeded
                    if CONSUME_SEGMENTS:
                        meta = _record_play_lifecycle(talk_seg)
                        plays = meta.get("play_count", 1)
                        if _should_retire(talk_seg, ctx.talk_lifecycle):
                            log(f"    (retired after {plays} play{'s' if plays != 1 else ''})")
                            _retire(talk_seg)
                        else:
                            lc = ctx.talk_lifecycle
                            remaining_plays = (lc.max_plays - plays) if lc.max_plays else "∞"
                            log(f"    (play {plays}, {remaining_plays} plays remaining)")
                    else:
                        _record_play_lifecycle(talk_seg)
                        log(f"    (kept — consume disabled)")

                    record_play(talk_seg, seg_name, "talk", ctx.show_id)

                    # Play 2-3 songs between talk segments (~30% music)
                    if running and encoder_proc.poll() is None:
                        max_tracks = random.randint(3, 4)
                        set_count = 0

                        while (running and encoder_proc.poll() is None
                               and set_count < max_tracks):
                            ai_bumper = select_ai_bumper(ctx.show_id)
                            if not ai_bumper:
                                if set_count == 0:
                                    log("  No AI bumpers available, skipping break")
                                break

                            bpath, bstart, bdur, bcaption, bdisplay = ai_bumper
                            bname = bdisplay or "AI Music"
                            set_count += 1
                            log(f"  MUSIC {set_count}: {bname} ({int(bdur)}s)")
                            if bcaption:
                                log(f"    {bcaption[:70]}...")
                            update_now_playing(
                                bname, "bumper",
                                show_id=ctx.show_id,
                                show_name=ctx.show_name,
                                caption=bcaption,
                            )
                            if not pipe_track(bpath, encoder_proc, bstart, bdur):
                                log("Music pipe failed, continuing...")
                                break

                            record_play(bpath, bname, "ai_bumper", ctx.show_id)
                            if CONSUME_SEGMENTS:
                                bmeta = _record_play_lifecycle(bpath)
                                bplays = bmeta.get("play_count", 1)
                                if _should_retire(bpath, ctx.music_lifecycle):
                                    log(f"    (bumper retired after {bplays} play{'s' if bplays != 1 else ''})")
                                    _retire(bpath)
                                else:
                                    blc = ctx.music_lifecycle
                                    brem = (blc.max_plays - bplays) if blc.max_plays else "∞"
                                    log(f"    (bumper play {bplays}, {brem} remaining)")
                            else:
                                _record_play_lifecycle(bpath)
                            last_bumper_path = bpath

                        if set_count > 0:
                            log(f"  Music set: {set_count} tracks")

                    # Check for new listener responses that arrived mid-queue
                    if running and encoder_proc.poll() is None:
                        fresh = get_listener_responses(ctx.show_id)
                        # Only play ones not already in our queue
                        queued_names = {s.name for s in talk_queue}
                        fresh = [f for f in fresh if f.name not in queued_names]
                        for resp in fresh:
                            if not running or encoder_proc.poll() is not None:
                                break
                            resp_name = clean_name(resp, is_speech=True)
                            log(f"  LISTENER RESPONSE (live): {resp_name}")
                            update_now_playing(
                                resp_name, "talk",
                                show_id=ctx.show_id,
                                show_name=ctx.show_name,
                                host=ctx.host,
                                segment_type="listener_response",
                            )
                            if pipe_track(resp, encoder_proc, is_speech=True):
                                try:
                                    resp.unlink()
                                    log(f"    (consumed)")
                                except Exception:
                                    pass

            else:
                # No talk segments — play AI bumpers if available, otherwise fallback tone
                ai_bumper = select_ai_bumper(ctx.show_id)
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
                    pipe_track(bpath, encoder_proc, start_time=bstart, duration=bdur)
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
                        pipe_track(fallback_tone, encoder_proc)
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
        "deep_dive", "news_analysis", "interview", "panel", "story",
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
