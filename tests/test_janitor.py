"""Regression tests for the output/ retention janitor.

Root reached 99% in July 2026 because nothing under output/ had a retention
policy except per-show segment age. The janitor is the fix, and it deletes
files — so the tests here are mostly about what it must NOT touch:

- output/scripts/ is the used-source dedupe ledger until Phase 3.5 replaces it.
  Sweeping it would silently let exhausted sources be re-aired.
- .vtt transcripts and info.json are the record of what was fetched; only the
  cached media beside them is disposable.
- A sidecar whose audio still exists belongs to a live segment.
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "admin"))

import scheduler  # noqa: E402


DAY = 86400


@pytest.fixture
def out(tmp_path, monkeypatch):
    """Point the janitor at a throwaway output/ tree."""
    root = tmp_path / "output"
    dirs = {
        "OUTPUT_DIR": root,
        "TALK_DIR": root / "talk_segments",
        "BUMPERS_DIR": root / "music_bumpers",
        "SOURCE_CACHE_DIR": root / "source_cache",
        "SCRIPTS_DIR": root / "scripts",
        "JOBS_DIR": root / "jobs",
    }
    for name, path in dirs.items():
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(scheduler, name, path)
    return root


def write(path: Path, content: str = "x", age_days: float = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    if age_days:
        ts = time.time() - age_days * DAY
        import os

        os.utime(path, (ts, ts))
    return path


def test_sweeps_stale_cached_media_but_keeps_the_record(out):
    video = out / "source_cache" / "youtube" / "abc123"
    stale_mp3 = write(video / "abc123.mp3", "audio", age_days=45)
    vtt = write(video / "abc123.en.vtt", "captions", age_days=45)
    info = write(video / "info.json", "{}", age_days=45)

    scheduler._janitor_sweep()

    assert not stale_mp3.exists(), "cached media past TTL should be swept"
    assert vtt.exists(), ".vtt is the transcript record and must survive"
    assert info.exists(), "info.json is the fetch record and must survive"


def test_leaves_recent_cached_media_alone(out):
    fresh = write(out / "source_cache" / "youtube" / "new" / "new.mp3", "audio", age_days=2)
    scheduler._janitor_sweep()
    assert fresh.exists(), "media inside the TTL is still in use"


def test_never_touches_the_dedupe_ledger(out):
    """output/scripts/ is load-bearing until Phase 3.5. Nothing may sweep it."""
    old_script = write(out / "scripts" / "talk_reddit_post_20260101_000000.json", "{}", age_days=365)
    rotation = write(out / "scripts" / ".youtube-ai_source_rotation.json", "{}", age_days=365)

    scheduler._janitor_sweep()

    assert old_script.exists(), "sweeping the ledger would let used sources re-air"
    assert rotation.exists(), "rotation state is not a sidecar"


def test_removes_orphan_sidecars_only(out):
    live_audio = write(out / "talk_segments" / "nosleep" / "seg.wav", "audio")
    live_meta = write(out / "talk_segments" / "nosleep" / "seg.json", "{}")
    live_plays = write(out / "talk_segments" / "nosleep" / "seg.wav.plays.json", "{}")

    orphan_meta = write(out / "talk_segments" / "nosleep" / "gone.json", "{}")
    orphan_plays = write(out / "talk_segments" / "nosleep" / "gone.wav.plays.json", "{}")

    report = scheduler._janitor_sweep()

    assert live_audio.exists() and live_meta.exists() and live_plays.exists()
    assert not orphan_meta.exists() and not orphan_plays.exists()
    assert report["orphan_sidecars"]["files"] == 2


def test_expires_old_job_records(out):
    old = write(out / "jobs" / "old.json", "{}", age_days=45)
    recent = write(out / "jobs" / "recent.json", "{}", age_days=3)

    scheduler._janitor_sweep()

    assert not old.exists()
    assert recent.exists()


def test_dry_run_reports_without_deleting(out):
    orphan = write(out / "talk_segments" / "s" / "gone.json", "{}")
    report = scheduler._janitor_sweep(dry_run=True)
    assert orphan.exists(), "dry run must not delete"
    assert report["orphan_sidecars"]["files"] == 1


def test_reports_byte_counts(out):
    write(out / "talk_segments" / "s" / "gone.json", "0123456789")
    report = scheduler._janitor_sweep()
    assert report["orphan_sidecars"]["bytes"] == 10


def test_empty_report_when_nothing_to_do(out):
    assert scheduler._janitor_sweep() == {}


def test_disk_report_shape(out):
    write(out / "talk_segments" / "s" / "seg.wav", "audio")
    monkey = scheduler.disk_usage_report()
    assert monkey["state"] in {"ok", "warning", "critical"}
    assert monkey["total_bytes"] > 0
    assert "talk_segments" in monkey["output_breakdown"]
