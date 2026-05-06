#!/usr/bin/env python3
"""Backfill short_title into existing segment sidecar JSON files.

Usage:
    uv run python tools/backfill_short_titles.py [--dry-run]

For each sidecar that lacks a short_title (or has a truncated one ending with …),
generates one using:
  - Channel prefix for YouTube segments (source_channel, or looked up from yt-dlp cache)
  - LLM compression for titles > 50 chars
  - Original topic as-is for titles that already fit
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from station.content_generator.helpers import make_short_title, log

TALK_DIR = PROJECT_ROOT / "output" / "talk_segments"
YOUTUBE_CACHE_DIR = PROJECT_ROOT / "output" / "source_cache" / "youtube"


def _build_yt_channel_cache() -> dict[str, str]:
    """Map YouTube video_id → channel name from local yt-dlp cache."""
    result: dict[str, str] = {}
    if not YOUTUBE_CACHE_DIR.exists():
        return result
    for vid_dir in YOUTUBE_CACHE_DIR.iterdir():
        info_path = vid_dir / "info.json"
        if info_path.exists():
            try:
                info = json.loads(info_path.read_text())
                ch = info.get("channel") or info.get("uploader") or ""
                if ch:
                    result[vid_dir.name] = ch
            except Exception:
                pass
    return result


def _video_id_from_url(url: str) -> str:
    """Extract YouTube video ID from a URL."""
    if not url:
        return ""
    for part in url.split("?")[1].split("&") if "?" in url else []:
        if part.startswith("v="):
            return part[2:]
    # Short URLs: youtu.be/ID
    if "youtu.be/" in url:
        return url.split("youtu.be/")[-1].split("?")[0]
    return ""


def process_sidecar(path: Path, dry_run: bool, yt_channels: dict[str, str]) -> str:
    """Return 'skipped', 'updated', or 'error'."""
    try:
        meta = json.loads(path.read_text())
    except Exception as e:
        log(f"  ERROR reading {path.name}: {e}")
        return "error"

    is_youtube = (
        meta.get("tts_backend") == "youtube_ingest"
        or meta.get("source_type") in {"youtube"}
    )
    existing = meta.get("short_title", "")
    channel_prefixed = (
        existing
        and meta.get("source_channel")
        and existing.startswith(meta["source_channel"] + ": ")
    )
    # Skip only if we have a good short_title that isn't a stale channel-prefixed one
    if existing and not existing.endswith("…") and not channel_prefixed:
        return "skipped"

    topic = meta.get("topic") or ""
    if not topic:
        return "skipped"

    # Determine channel for YouTube segments
    channel = meta.get("source_channel") or ""
    if not channel and meta.get("source_type") in {"youtube", "youtube_ingest"} or meta.get("tts_backend") == "youtube_ingest":
        source_url = meta.get("source_value") or ""
        vid_id = _video_id_from_url(source_url)
        if vid_id:
            channel = yt_channels.get(vid_id, "")
        # Write back so future runs don't need to look it up
        if channel and not meta.get("source_channel"):
            meta["source_channel"] = channel

    short = make_short_title(topic, channel=channel)

    if not dry_run:
        meta["short_title"] = short
        path.write_text(json.dumps(meta, indent=2))

    marker = "[DRY RUN] " if dry_run else ""
    ch_note = f" [{channel}]" if channel else ""
    log(f"  {marker}{path.parent.name}/{path.stem[:35]}{ch_note} → {short!r}")
    return "updated"


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill short_title into segment sidecars")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be done without writing")
    args = parser.parse_args()

    if not TALK_DIR.exists():
        log(f"Talk segments dir not found: {TALK_DIR}")
        sys.exit(1)

    yt_channels = _build_yt_channel_cache()
    log(f"Loaded {len(yt_channels)} YouTube channel mappings from cache")

    sidecars = sorted(TALK_DIR.glob("**/*.json"))
    log(f"Found {len(sidecars)} sidecar files under {TALK_DIR}")

    counts = {"updated": 0, "skipped": 0, "error": 0}
    for sidecar in sidecars:
        result = process_sidecar(sidecar, dry_run=args.dry_run, yt_channels=yt_channels)
        counts[result] += 1

    log(f"Done — updated: {counts['updated']}, skipped: {counts['skipped']}, errors: {counts['error']}")


if __name__ == "__main__":
    main()
