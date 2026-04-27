#!/usr/bin/env python3
"""
WRIT-FM On-Demand Content Module

Shared module used by both the streamer API (port 8001) and admin API (port 8080).
Handles:
  - SQLite state store (resume position + listened status)
  - Audiobookshelf API client
  - Local upload inventory
  - Inbox watcher (drop-in directory for agents)
  - Config read/write for config/ondemand.yaml
"""

import json
import os
import shutil
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "ondemand.yaml"
ONDEMAND_DIR = PROJECT_ROOT / "output" / "ondemand"
UPLOADS_DIR = ONDEMAND_DIR / "uploads"
INBOX_DIR = ONDEMAND_DIR / "inbox"
SEGMENTS_DIR = PROJECT_ROOT / "output" / "talk_segments"
DB_PATH = Path.home() / ".writ" / "ondemand.db"

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
SEGMENT_AUDIO_EXTS = (".wav", ".mp3")
INVENTORY_CACHE_TTL = 60  # seconds


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG: dict = {
    "abs": {
        "base_url": "",
        "libraries": [],
    },
    "upload_sources": [
        {"id": "news", "name": "News"},
        {"id": "misc", "name": "Misc"},
    ],
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return _DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_PATH) as f:
            data = yaml.safe_load(f) or {}
        # Merge missing top-level keys from defaults
        for k, v in _DEFAULT_CONFIG.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return _DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)


# ---------------------------------------------------------------------------
# Item dataclass
# ---------------------------------------------------------------------------

@dataclass
class Item:
    id: str                    # "abs:<libItemId>" or "upload:<uuid-or-filename>"
    source: str                # "abs:<LibraryId>" or "upload:<sourceId>"
    source_name: str           # Human name e.g. "Comedy", "News"
    title: str
    subtitle: str = ""
    duration_seconds: float = 0.0
    mime_type: str = "audio/mpeg"
    created_at: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# SQLite state store
# ---------------------------------------------------------------------------

class OnDemandStore:
    """Persist resume position and listened status per item."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ondemand_state (
                    item_id    TEXT PRIMARY KEY,
                    position_s REAL NOT NULL DEFAULT 0,
                    duration_s REAL,
                    listened   INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.commit()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def get_state(self, item_id: str) -> dict:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM ondemand_state WHERE item_id = ?", (item_id,)
            ).fetchone()
        if row:
            return dict(row)
        return {"item_id": item_id, "position_s": 0.0, "duration_s": None, "listened": 0}

    def get_all_states(self) -> dict[str, dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM ondemand_state").fetchall()
        return {row["item_id"]: dict(row) for row in rows}

    def set_position(self, item_id: str, position_s: float, duration_s: float | None = None) -> None:
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO ondemand_state (item_id, position_s, duration_s, listened, updated_at)
                    VALUES (?, ?, ?, 0, ?)
                    ON CONFLICT(item_id) DO UPDATE SET
                        position_s = excluded.position_s,
                        duration_s = COALESCE(excluded.duration_s, duration_s),
                        updated_at = excluded.updated_at
                """, (item_id, position_s, duration_s, self._now()))
                conn.commit()

    def mark_listened(self, item_id: str) -> None:
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO ondemand_state (item_id, position_s, duration_s, listened, updated_at)
                    VALUES (?, 0, NULL, 1, ?)
                    ON CONFLICT(item_id) DO UPDATE SET
                        listened = 1,
                        updated_at = excluded.updated_at
                """, (item_id, self._now()))
                conn.commit()

    def unmark_listened(self, item_id: str) -> None:
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO ondemand_state (item_id, position_s, duration_s, listened, updated_at)
                    VALUES (?, 0, NULL, 0, ?)
                    ON CONFLICT(item_id) DO UPDATE SET
                        listened = 0,
                        position_s = 0,
                        updated_at = excluded.updated_at
                """, (item_id, self._now()))
                conn.commit()


# Singleton used by both admin and streamer (within the same process)
_store: OnDemandStore | None = None
_store_lock = threading.Lock()


def get_store() -> OnDemandStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = OnDemandStore()
    return _store


# ---------------------------------------------------------------------------
# Audiobookshelf client
# ---------------------------------------------------------------------------

class AbsClient:
    """Minimal Audiobookshelf API client using urllib.request."""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        # Cache item_id → first audio file ino (avoids per-request detail fetches after first play)
        self._ino_cache: dict[str, str] = {}

    def _get(self, path: str, timeout: int = 10) -> Any:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"ABS HTTP {e.code} for {path}: {e.reason}") from e
        except Exception as e:
            raise RuntimeError(f"ABS request failed for {path}: {e}") from e

    def ping(self) -> bool:
        try:
            self._get("/ping", timeout=5)
            return True
        except Exception:
            return False

    def list_libraries(self) -> list[dict]:
        data = self._get("/api/libraries")
        return [
            {"id": lib["id"], "name": lib["name"], "media_type": lib.get("mediaType", "")}
            for lib in data.get("libraries", [])
        ]

    def list_items(self, library_id: str, limit: int = 100) -> list[dict]:
        """Return normalised item list for a library (first page, up to limit)."""
        data = self._get(f"/api/libraries/{library_id}/items?limit={limit}&sort=addedAt&desc=1")
        items = []
        for raw in data.get("results", []):
            media = raw.get("media", {})
            meta = media.get("metadata", {})
            # Books have a single file; podcasts have episodes — support both
            files = media.get("audioFiles", [])
            if not files:
                # Podcast episodes
                files = media.get("episodes", [])
            duration = media.get("duration") or sum(
                f.get("duration", 0) for f in files
            )
            # Use first file id for single-file items
            file_id = files[0].get("ino") if files else None
            items.append({
                "id": raw["id"],
                "library_id": library_id,
                "title": meta.get("title") or raw.get("path", "").split("/")[-1],
                "subtitle": meta.get("author") or meta.get("authorName") or "",
                "duration_seconds": duration,
                "file_id": file_id,
                "num_files": len(files),
                "added_at": raw.get("addedAt"),
            })
        return items

    def _get_file_ino(self, item_id: str) -> str:
        """Fetch and cache the ino of the first audio file for an item."""
        if item_id in self._ino_cache:
            return self._ino_cache[item_id]
        detail = self._get(f"/api/items/{item_id}")
        audio_files = detail.get("media", {}).get("audioFiles", [])
        if not audio_files:
            raise RuntimeError(f"No audio files found for ABS item {item_id}")
        ino = audio_files[0]["ino"]
        self._ino_cache[item_id] = ino
        return ino

    def audio_stream_url(self, item_id: str, ino: str) -> str:
        """Direct streaming URL for a specific audio file (requires auth header)."""
        return f"{self.base_url}/api/items/{item_id}/file/{ino}"

    def proxy_audio(self, item_id: str, range_header: str | None = None) -> tuple[int, dict, bytes]:
        """
        Fetch audio bytes from ABS, forwarding Range if provided.
        Lazily resolves the file ino on first call and caches it.
        Returns (status_code, headers_dict, body_bytes).
        """
        ino = self._get_file_ino(item_id)
        url = self.audio_stream_url(item_id, ino)
        headers: dict[str, str] = {"Authorization": f"Bearer {self.api_key}"}
        if range_header:
            headers["Range"] = range_header
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status = resp.status
                resp_headers = {
                    "Content-Type": resp.headers.get("Content-Type", "audio/mpeg"),
                    "Content-Length": resp.headers.get("Content-Length", ""),
                    "Content-Range": resp.headers.get("Content-Range", ""),
                    "Accept-Ranges": "bytes",
                }
                body = resp.read()
            return status, resp_headers, body
        except urllib.error.HTTPError as e:
            body = e.read() if hasattr(e, "read") else b""
            return e.code, {}, body
        except Exception as e:
            raise RuntimeError(f"ABS proxy failed for {item_id}: {e}") from e


def _make_abs_client() -> AbsClient | None:
    cfg = load_config()
    base_url = cfg.get("abs", {}).get("base_url") or os.environ.get("WRIT_ABS_URL", "")
    api_key = os.environ.get("WRIT_ABS_API_KEY", "")
    if not base_url or not api_key:
        return None
    return AbsClient(base_url, api_key)


# ---------------------------------------------------------------------------
# Inbox watcher
# ---------------------------------------------------------------------------

class InboxWatcher:
    """
    Scan output/ondemand/inbox/ for {name}.mp3 (or other audio) + {name}.json pairs.
    Validated pairs are atomically moved to output/ondemand/uploads/<source>/.
    """

    def scan_once(self) -> list[str]:
        """Process inbox, return list of item_ids that were ingested."""
        INBOX_DIR.mkdir(parents=True, exist_ok=True)
        ingested = []
        for json_file in sorted(INBOX_DIR.glob("*.json")):
            audio_file = self._find_audio(json_file)
            if not audio_file:
                continue
            try:
                meta = json.loads(json_file.read_text())
            except Exception:
                continue
            source_id = meta.get("source") or "misc"
            title = meta.get("title") or audio_file.stem
            dest_dir = UPLOADS_DIR / source_id
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_audio = dest_dir / audio_file.name
            dest_json = dest_dir / json_file.name
            # Ensure normalised sidecar fields
            meta.setdefault("id", f"upload:{audio_file.stem}")
            meta.setdefault("source", f"upload:{source_id}")
            meta.setdefault("title", title)
            meta.setdefault("created_at", datetime.now(timezone.utc).isoformat())
            # Move atomically
            shutil.move(str(audio_file), str(dest_audio))
            dest_json.write_text(json.dumps(meta, indent=2))
            json_file.unlink(missing_ok=True)
            ingested.append(meta["id"])
        return ingested

    def _find_audio(self, json_file: Path) -> Path | None:
        stem = json_file.stem
        for ext in AUDIO_EXTS:
            candidate = INBOX_DIR / f"{stem}{ext}"
            if candidate.exists():
                return candidate
        return None


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

@dataclass
class _InventoryCache:
    value: list[Item]
    ts: float = 0.0


_inventory_cache = _InventoryCache(value=[])
_inventory_lock = threading.Lock()


def get_inventory(force: bool = False) -> list[Item]:
    """Return merged on-demand inventory (ABS + uploads), with TTL cache."""
    with _inventory_lock:
        if not force and (time.time() - _inventory_cache.ts) < INVENTORY_CACHE_TTL:
            return _inventory_cache.value
        # Scan inbox first
        InboxWatcher().scan_once()
        items = _load_show_segments() + _load_uploads() + _load_abs_items()
        _inventory_cache.value = items
        _inventory_cache.ts = time.time()
        return items


def invalidate_inventory() -> None:
    with _inventory_lock:
        _inventory_cache.ts = 0.0


def _load_uploads() -> list[Item]:
    cfg = load_config()
    source_names = {s["id"]: s["name"] for s in cfg.get("upload_sources", [])}
    items: list[Item] = []
    if not UPLOADS_DIR.exists():
        return items
    for source_dir in sorted(UPLOADS_DIR.iterdir()):
        if not source_dir.is_dir():
            continue
        source_id = source_dir.name
        source_name = source_names.get(source_id, source_id.capitalize())
        for json_file in sorted(source_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                meta = json.loads(json_file.read_text())
            except Exception:
                continue
            # Find companion audio
            audio = None
            for ext in AUDIO_EXTS:
                candidate = json_file.with_suffix(ext)
                if candidate.exists():
                    audio = candidate
                    break
            if audio is None:
                continue
            item_id = meta.get("id") or f"upload:{json_file.stem}"
            items.append(Item(
                id=item_id,
                source=f"upload:{source_id}",
                source_name=source_name,
                title=meta.get("title") or json_file.stem,
                subtitle=meta.get("subtitle") or "",
                duration_seconds=float(meta.get("duration_seconds") or 0),
                mime_type=meta.get("mime_type") or _mime_for(audio),
                created_at=meta.get("created_at") or "",
                extra=meta.get("extra") or {},
            ))
    return items


def _load_abs_items() -> list[Item]:
    cfg = load_config()
    libraries: list[dict] = cfg.get("abs", {}).get("libraries", [])
    if not libraries:
        return []
    client = _make_abs_client()
    if client is None:
        return []
    items: list[Item] = []
    for lib in libraries:
        lib_id = lib.get("id", "")
        lib_name = lib.get("name", lib_id)
        try:
            raw_items = client.list_items(lib_id)
        except Exception:
            continue
        for raw in raw_items:
            items.append(Item(
                id=f"abs:{raw['id']}",
                source=f"abs:{lib_id}",
                source_name=lib_name,
                title=raw["title"],
                subtitle=raw.get("subtitle") or "",
                duration_seconds=float(raw.get("duration_seconds") or 0),
                mime_type="audio/mpeg",
                created_at=str(raw.get("added_at") or ""),
                extra={"abs_library_id": lib_id, "abs_file_id": raw.get("file_id")},
            ))
    return items


def _mime_for(path: Path) -> str:
    return {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
    }.get(path.suffix.lower(), "audio/mpeg")


def _get_show_names() -> dict[str, str]:
    """Return show_id → name from schedule.yaml."""
    try:
        schedule_path = PROJECT_ROOT / "config" / "schedule.yaml"
        with open(schedule_path) as f:
            data = yaml.safe_load(f) or {}
        return {sid: scfg.get("name", sid) for sid, scfg in data.get("shows", {}).items()}
    except Exception:
        return {}


def get_show_sources() -> list[dict]:
    """Return show sources that have audio content in output/talk_segments/."""
    if not SEGMENTS_DIR.exists():
        return []
    show_names = _get_show_names()
    sources = []
    for show_dir in sorted(SEGMENTS_DIR.iterdir()):
        if not show_dir.is_dir():
            continue
        if any(f.suffix.lower() in SEGMENT_AUDIO_EXTS for f in show_dir.iterdir() if f.is_file()):
            show_id = show_dir.name
            name = show_names.get(show_id, show_id.replace("_", " ").title())
            sources.append({"id": f"show:{show_id}", "name": name, "type": "show"})
    return sources


def _load_show_segments() -> list[Item]:
    """Load talk segments from output/talk_segments/ as on-demand items."""
    if not SEGMENTS_DIR.exists():
        return []
    show_names = _get_show_names()
    items: list[Item] = []
    for show_dir in sorted(SEGMENTS_DIR.iterdir()):
        if not show_dir.is_dir():
            continue
        show_id = show_dir.name
        show_name = show_names.get(show_id, show_id.replace("_", " ").title())
        for json_file in sorted(show_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            if json_file.name.endswith(".plays.json"):
                continue
            try:
                meta = json.loads(json_file.read_text())
            except Exception:
                continue
            audio: Path | None = None
            for ext in SEGMENT_AUDIO_EXTS:
                candidate = json_file.with_suffix(ext)
                if candidate.exists():
                    audio = candidate
                    break
            if audio is None:
                continue
            # Estimate duration from word count + WPM if available
            duration_s = 0.0
            wc = meta.get("word_count")
            wpm = (meta.get("voices") or {}).get("host_wpm")
            if wc and wpm:
                duration_s = float(wc) / float(wpm) * 60
            items.append(Item(
                id=f"show:{show_id}:{json_file.stem}",
                source=f"show:{show_id}",
                source_name=show_name,
                title=meta.get("topic") or json_file.stem,
                subtitle=meta.get("host") or "",
                duration_seconds=duration_s,
                mime_type=_mime_for(audio),
                created_at=meta.get("generated_at") or "",
                extra={"segment_type": meta.get("type"), "show_id": show_id},
            ))
    return items


def get_segment_path(item_id: str) -> Path | None:
    """Return filesystem path for a show segment item."""
    if not item_id.startswith("show:"):
        return None
    rest = item_id[5:]
    show_id, _, stem = rest.partition(":")
    if not stem:
        return None
    show_dir = SEGMENTS_DIR / show_id
    for ext in SEGMENT_AUDIO_EXTS:
        candidate = show_dir / f"{stem}{ext}"
        if candidate.exists():
            return candidate
    return None


def get_item(item_id: str) -> Item | None:
    for item in get_inventory():
        if item.id == item_id:
            return item
    return None


def get_upload_path(item_id: str) -> Path | None:
    """Return filesystem path for an upload item."""
    if not item_id.startswith("upload:"):
        return None
    # item_id format: "upload:<stem>" where audio lives in uploads/<source>/<stem>.<ext>
    stem = item_id[len("upload:"):]
    for source_dir in UPLOADS_DIR.iterdir() if UPLOADS_DIR.exists() else []:
        if not source_dir.is_dir():
            continue
        for ext in AUDIO_EXTS:
            candidate = source_dir / f"{stem}{ext}"
            if candidate.exists():
                return candidate
    return None
