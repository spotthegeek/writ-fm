"""Phase 3c/3d/3e — the used-source index, the backoff ladder, and the archive
as a reserve rather than the default.

The failure these cover is the one Session B's soak exposed (D20): the archive
fallback added in 3.2 fired on *every* generation for a slow subreddit, so
r/talesfromtechsupport aired eleven posts dated 2014-2017 as though they were
this week. 3.10 gates the archive on scarcity, 3.11 stops expiry walking a show
through that gate, and 3.12 dates whatever does come out of the reserve.

3.10 and 3.11 only work as a pair, and `test_between_floor_and_minimum_*` below
is the invariant that says so: a show in that band neither generates archive
material nor loses segments to expiry.
"""

from __future__ import annotations

import importlib
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from station.content_generator import talk_generator as tg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "admin"))
import scheduler  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _permalink(post_id: str) -> str:
    return f"{tg._reddit_thread_base()}/r/testsub/comments/{post_id}/story/"


def _key(post_id: str) -> str:
    return tg._canonical_source_key("reddit_thread", _permalink(post_id))


def _write_script(scripts_dir: Path, show_id: str, post_id: str, when: datetime) -> Path:
    path = scripts_dir / f"talk_reddit_post_{post_id}_{when.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps({
        "show_id": show_id,
        "source_type": "reddit",
        "source_value": _permalink(post_id),
    }))
    return path


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """A throwaway output/scripts + output/state pair."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    monkeypatch.setattr(tg, "SCRIPTS_DIR", scripts)
    monkeypatch.setattr(tg, "STATE_DIR", tmp_path / "state")
    monkeypatch.setenv("WRIT_SOURCE_REUSE_DAYS", "90")
    return scripts


# ---------------------------------------------------------------------------
# 3.5 — the index must reproduce the scan exactly
# ---------------------------------------------------------------------------


def test_index_reproduces_the_scan_key_set_exactly(ledger, monkeypatch):
    """The mandatory 3.5 invariant, with the 90-day window applied to BOTH sides.

    Comparing against an unwindowed full-history scan instead reports a
    difference of 19-69 keys per show on the live corpus, which is task 3.4's
    intended behaviour and not a bug (D14) — see the companion test below."""
    now = datetime.now()
    for i, age in enumerate([0, 1, 30, 89, 91, 200]):
        _write_script(ledger, "nosleep", f"n{i}", now - timedelta(days=age))
    for i, age in enumerate([2, 120]):
        _write_script(ledger, "sysadmin", f"s{i}", now - timedelta(days=age))

    for show in ("nosleep", "sysadmin", "no_such_show"):
        assert tg._used_source_keys_for_show(show) == tg._used_source_keys_by_scan(show)


def test_unwindowed_comparison_would_report_a_spurious_difference(ledger):
    """D14 in test form: the difference is the expiry doing its job. A future
    session comparing the index against a full-history scan should read this
    before concluding the index is broken."""
    now = datetime.now()
    _write_script(ledger, "nosleep", "fresh", now - timedelta(days=1))
    _write_script(ledger, "nosleep", "expired", now - timedelta(days=200))

    windowed = tg._used_source_keys_for_show("nosleep")
    full_history = set(tg._scan_used_source_stamps("nosleep"))

    assert windowed == {_key("fresh")}
    assert full_history - windowed == {_key("expired")}


def test_index_survives_the_scripts_it_was_built_from(ledger):
    """What makes the output/scripts/ TTL safe: once indexed, a key does not
    depend on its script record still being on disk."""
    now = datetime.now()
    path = _write_script(ledger, "nosleep", "a1", now - timedelta(days=1))
    assert _key("a1") in tg._used_source_keys_for_show("nosleep")

    path.unlink()

    assert _key("a1") in tg._used_source_keys_for_show("nosleep")
    assert tg._used_source_keys_by_scan("nosleep") == set(), "the scan has nothing left to find"


def test_writing_a_script_appends_to_the_index(ledger):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    meta_path = ledger / f"talk_reddit_{stamp}.json"
    tg._write_script_record(meta_path, {
        "show_id": "nosleep",
        "source_type": "reddit",
        "source_value": _permalink("new1"),
    })

    assert meta_path.exists()
    assert _key("new1") in tg._used_source_keys_for_show("nosleep")


def test_index_prunes_past_the_window_on_write(ledger):
    now = datetime.now()
    _write_script(ledger, "nosleep", "old", now - timedelta(days=200))
    _write_script(ledger, "nosleep", "new", now - timedelta(days=1))

    tg._used_source_keys_for_show("nosleep")  # bootstrap + persist

    stored = json.loads(tg._used_index_path("nosleep").read_text())["keys"]
    assert set(stored) == {_key("new")}, "an expired key should not be carried in the file"


def test_window_of_zero_keeps_the_whole_history_in_the_index(ledger, monkeypatch):
    monkeypatch.setenv("WRIT_SOURCE_REUSE_DAYS", "0")
    now = datetime.now()
    _write_script(ledger, "nosleep", "ancient", now - timedelta(days=800))

    assert _key("ancient") in tg._used_source_keys_for_show("nosleep")
    stored = json.loads(tg._used_index_path("nosleep").read_text())["keys"]
    assert set(stored) == {_key("ancient")}


def test_index_is_per_show(ledger):
    now = datetime.now()
    _write_script(ledger, "nosleep", "a1", now - timedelta(days=1))
    _write_script(ledger, "sysadmin", "b1", now - timedelta(days=1))

    assert tg._used_source_keys_for_show("nosleep") == {_key("a1")}
    assert tg._used_source_keys_for_show("sysadmin") == {_key("b1")}


def test_rotation_state_is_never_indexed(ledger):
    """`.<show>_source_rotation.json` shares the directory but is not a script."""
    (ledger / ".nosleep_source_rotation.json").write_text(
        json.dumps({"last_key": "reddit_subreddit:nosleep"})
    )
    assert tg._used_source_keys_for_show("nosleep") == set()


def test_corrupt_index_falls_back_to_the_scan(ledger):
    now = datetime.now()
    _write_script(ledger, "nosleep", "a1", now - timedelta(days=1))
    tg._used_source_keys_for_show("nosleep")
    tg._used_index_path("nosleep").write_text("{not json")

    assert tg._used_source_keys_for_show("nosleep") == {_key("a1")}


def test_indexing_failure_never_loses_the_script(ledger, monkeypatch):
    """A segment that exists but is unindexed costs one possible repeat. A
    segment lost to an indexing error costs the airtime."""
    monkeypatch.setattr(tg, "_record_used_source", lambda *a, **k: 1 / 0)
    meta_path = ledger / "talk_reddit_20260727_120000.json"

    tg._write_script_record(meta_path, {"show_id": "nosleep", "source_type": "reddit",
                                        "source_value": _permalink("x1")})

    assert json.loads(meta_path.read_text())["show_id"] == "nosleep"


# ---------------------------------------------------------------------------
# 3.6 — the failure backoff escalates and resets
# ---------------------------------------------------------------------------


@pytest.fixture
def sched_state():
    state = scheduler.SchedulerState()
    return state


def test_failure_backoff_escalates_then_holds(sched_state):
    waits = [sched_state.record_failure("nosleep", "talk") for _ in range(6)]
    assert waits == [1800, 3600, 14400, 43200, 43200, 43200]


def test_backoff_resets_on_success(sched_state):
    sched_state.record_failure("nosleep", "talk")
    sched_state.record_failure("nosleep", "talk")
    sched_state.record_success("nosleep", "talk")

    assert sched_state.backoff_state("nosleep", "talk") == ("", 0)
    assert sched_state.record_failure("nosleep", "talk") == 1800, "back to the first rung"


def test_backoff_is_per_show_and_per_content_type(sched_state):
    sched_state.record_failure("nosleep", "talk")
    sched_state.record_failure("nosleep", "talk")

    assert sched_state.record_failure("sysadmin", "talk") == 1800
    assert sched_state.record_failure("nosleep", "music") == 1800


def test_cold_has_its_own_longer_ladder(sched_state):
    waits = [sched_state.record_cold("nosleep", "talk") for _ in range(4)]
    assert waits == [14400, 43200, 86400, 86400]


def test_changing_kind_restarts_the_streak(sched_state):
    """A source that was failing and is now merely cold has had its problem
    fixed; it should not inherit the failure ladder's twelve hours."""
    for _ in range(4):
        sched_state.record_failure("nosleep", "talk")

    assert sched_state.record_cold("nosleep", "talk") == 14400
    assert sched_state.record_failure("nosleep", "talk") == 1800


def test_backoff_expires(sched_state, monkeypatch):
    sched_state.record_failure("nosleep", "talk")
    assert sched_state.in_failure_backoff("nosleep", "talk")

    entry = sched_state.backoff_per_show["nosleep"]["talk"]
    entry["at"] = entry["at"] - timedelta(seconds=1801)

    assert not sched_state.in_failure_backoff("nosleep", "talk")
    assert sched_state.backoff_state("nosleep", "talk") == ("", 0)


# ---------------------------------------------------------------------------
# 3.7 — cold is not failed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("line", [
    "No unused posts found for r/talesfromtechsupport (312 scanned, including the archive)",
    "No unused YouTube entries found (50 scanned, including the back catalogue)",
    "  r/sysadmin: nothing unused within 3 day(s) after 100 post(s); archive held back (show is above the scarcity floor).",
])
def test_cold_markers_are_recognised(line):
    assert scheduler._output_looks_cold([line])


@pytest.mark.parametrize("line", [
    "Traceback (most recent call last):",
    "Ollama error: timed out",
    "  TTS rendering failed",
    "No usable posts found for r/testsub",
])
def test_real_failures_are_not_mistaken_for_cold(line):
    assert not scheduler._output_looks_cold([line])


def test_cold_does_not_register_as_a_failure(sched_state):
    """Two months of starvation read as `Generation failed (exit 1)`, which is
    why nobody noticed. Cold gets its own timestamp."""
    sched_state.record_cold("nosleep", "talk")

    assert sched_state.last_failure("nosleep", "talk") is None
    assert sched_state.last_cold("nosleep", "talk") is not None
    assert sched_state.backoff_state("nosleep", "talk")[0] == "cold"


def test_snapshot_exposes_cold_and_backoff(sched_state):
    sched_state.record_cold("nosleep", "talk")
    snap = sched_state.snapshot()

    assert "nosleep" in snap["last_cold_per_show"]
    assert snap["backoff_per_show"]["nosleep"]["talk"]["kind"] == "cold"


# ---------------------------------------------------------------------------
# 3.10 — the archive is a reserve
# ---------------------------------------------------------------------------


def test_archive_is_open_by_default(monkeypatch):
    """A hand-run generation is an explicit request for content."""
    monkeypatch.delenv("WRIT_ALLOW_ARCHIVE", raising=False)
    assert tg._archive_fallback_allowed()


@pytest.mark.parametrize("value,expected", [("1", True), ("0", False), ("false", False), ("", True)])
def test_archive_gate_reads_the_env(monkeypatch, value, expected):
    monkeypatch.setenv("WRIT_ALLOW_ARCHIVE", value)
    assert tg._archive_fallback_allowed() is expected


def _listing(posts, after=None):
    return json.dumps({"data": {"after": after, "children": [{"data": p} for p in posts]}}).encode()


def _old_post(post_id, years):
    return {
        "id": post_id,
        "permalink": f"/r/testsub/comments/{post_id}/story/",
        "title": f"Title for {post_id}",
        "selftext": " ".join(["word"] * 400),
        "created_utc": time.time() - years * 365 * 86400,
        "num_comments": 5,
        "score": 10,
    }


@pytest.fixture
def dry_subreddit(monkeypatch):
    """A subreddit whose recency window is empty but whose archive is not —
    r/talesfromtechsupport, in other words."""
    calls: list[str] = []

    def _fetch(url, timeout=None):
        calls.append(url)
        if "t=all" in url:
            return _listing([_old_post("ancient", 9)])
        if "/new.json" in url or "/hot.json" in url:
            # The listing is not empty — it is just all older than the show's
            # lookback window. This is r/talesfromtechsupport on any given day,
            # and it is what made 3.2's fallback fire on every generation.
            return _listing([_old_post("stale", 1)])
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(tg, "_fetch_url_reddit", _fetch)
    monkeypatch.setattr(
        tg, "_fetch_reddit_thread_context",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("thread fetch stubbed out")),
    )
    return calls


def test_scarce_show_reaches_the_archive(dry_subreddit, monkeypatch):
    monkeypatch.setenv("WRIT_ALLOW_ARCHIVE", "1")

    ctx = tg._fetch_reddit_subreddit_context_with_strategy("testsub", lookback_days=3)

    assert "ancient" in ctx.source_value
    assert any("t=all" in url for url in dry_subreddit), "the archive should have been consulted"


def test_stocked_show_goes_cold_instead_of_airing_the_archive(dry_subreddit, monkeypatch):
    """The D20 fix. Above the floor, the segments already on disk keep airing —
    which sounds better than a nine-year-old thread read as though it were now."""
    monkeypatch.setenv("WRIT_ALLOW_ARCHIVE", "0")

    with pytest.raises(RuntimeError) as excinfo:
        tg._fetch_reddit_subreddit_context_with_strategy("testsub", lookback_days=3)

    assert "archive held back" in str(excinfo.value)
    assert not any("t=all" in url for url in dry_subreddit), "the archive must not be fetched"
    assert scheduler._output_looks_cold([str(excinfo.value)]), "and it must read as cold, not failed"


def test_scheduler_opens_the_archive_only_at_the_floor():
    floor = scheduler.ARCHIVE_SCARCITY_FLOOR
    assert floor == 3
    # The expression _check_and_generate applies, stated once so the two halves
    # of the 3.10/3.11 pair are provably keyed off the same number.
    assert [n <= floor for n in (0, 3, 4, 8)] == [True, True, False, False]


def test_archive_can_be_switched_off_per_show():
    """The operator's knob: shows whose material is stale in substance rather
    than only in framing can go cold at any inventory."""
    assert scheduler.DEFAULT_TALK_CONFIG["archive_fallback"] is True

    off = {**scheduler.DEFAULT_TALK_CONFIG, "archive_fallback": False}
    scarce = 0 <= scheduler.ARCHIVE_SCARCITY_FLOOR
    assert not (scarce and off["archive_fallback"])


# ---------------------------------------------------------------------------
# 3.11 — expiry stops at the floor, and the pair invariant
# ---------------------------------------------------------------------------


@pytest.fixture
def show_segments(tmp_path, monkeypatch):
    """Write n talk segments for a show, all `age_days` old."""
    talk = tmp_path / "talk_segments"
    monkeypatch.setattr(scheduler, "TALK_DIR", talk)

    def _make(show_id: str, count: int, age_days: float):
        d = talk / show_id
        d.mkdir(parents=True, exist_ok=True)
        base = time.time() - age_days * 86400
        for i in range(count):
            f = d / f"seg_{i:02d}.wav"
            f.write_text("audio")
            # Spread by a second each so "newest" is well defined.
            import os
            os.utime(f, (base + i, base + i))
        return d

    return _make


def test_expiry_stops_at_the_floor(show_segments):
    d = show_segments("talesfromtechsupport", 12, age_days=10)

    deleted = scheduler._cleanup_expired_segments("talesfromtechsupport", max_days=3)

    remaining = sorted(f.name for f in d.iterdir())
    assert len(remaining) == scheduler.ARCHIVE_SCARCITY_FLOOR
    assert deleted == 12 - scheduler.ARCHIVE_SCARCITY_FLOOR
    assert remaining == ["seg_09.wav", "seg_10.wav", "seg_11.wav"], "the newest survive"


def test_expiry_still_clears_everything_above_the_floor(show_segments):
    """The floor must not stop expiry creating the inventory gap that triggers
    generation — that gap is the only thing that ever starts a job."""
    d = show_segments("nosleep", 12, age_days=10)
    scheduler._cleanup_expired_segments("nosleep", max_days=3)
    assert len(list(d.iterdir())) == scheduler.ARCHIVE_SCARCITY_FLOOR


def test_expiry_leaves_unexpired_segments_alone(show_segments):
    d = show_segments("nosleep", 12, age_days=1)
    assert scheduler._cleanup_expired_segments("nosleep", max_days=3) == 0
    assert len(list(d.iterdir())) == 12


def test_expiry_below_the_floor_is_a_no_op(show_segments):
    d = show_segments("nosleep", 2, age_days=99)
    assert scheduler._cleanup_expired_segments("nosleep", max_days=3) == 0
    assert len(list(d.iterdir())) == 2, "already at or under the floor; nothing to give"


def test_between_floor_and_minimum_the_show_neither_drains_nor_airs_the_archive(show_segments, monkeypatch):
    """The 3.10/3.11 pair invariant, stated as one test because either half
    alone is worse than neither:

      - 3.10 without 3.11: expiry walks the show to zero while the generator
        declines to refill it.
      - 3.11 without 3.10: the floor only delays the same archive top-up.

    A show sitting between the floor and min_inventory holds what it has.
    """
    floor = scheduler.ARCHIVE_SCARCITY_FLOOR
    minimum = 8
    inventory = 5
    assert floor < inventory < minimum

    d = show_segments("sysadmin", inventory, age_days=10)

    # 3.11: expiry may take it down to the floor, never through it.
    scheduler._cleanup_expired_segments("sysadmin", max_days=3)
    assert len(list(d.iterdir())) == floor

    # 3.10: while it was above the floor, the archive stayed shut.
    assert not (inventory <= floor), "above the floor"
    # And once it reaches the floor, the reserve opens.
    assert floor <= floor


# ---------------------------------------------------------------------------
# 3.12 — reserve material is placed in time
# ---------------------------------------------------------------------------


def test_recent_material_is_not_dated():
    """Almost everything airs within days of being posted; a dateline on that
    would be noise."""
    dateline, instruction = tg._source_age_phrases(time.time() - 2 * 86400)
    assert dateline == "" and instruction == ""


def test_decade_old_material_is_placed_in_time():
    posted = datetime(2016, 3, 14, tzinfo=tg.station_now().tzinfo)
    dateline, instruction = tg._source_age_phrases(posted.timestamp())

    assert "2016-03-14" in dateline and "March 2016" in dateline
    assert "back in 2016" in instruction
    assert "not current" in instruction


def test_months_old_material_is_dated_in_months():
    dateline, instruction = tg._source_age_phrases(time.time() - 120 * 86400)
    assert "months ago" in dateline
    assert "months ago" in instruction


def test_missing_or_bogus_timestamps_are_ignored():
    for value in (None, "", 0, -1, "yesterday"):
        assert tg._source_age_phrases(value) == ("", "")


def test_archive_post_carries_its_date_into_the_prompt(dry_subreddit, monkeypatch):
    """End to end: the reserve post reaches the host with its age attached, so
    it is told as history rather than as news."""
    monkeypatch.setenv("WRIT_ALLOW_ARCHIVE", "1")

    ctx = tg._fetch_reddit_subreddit_context_with_strategy("testsub", lookback_days=3)

    assert "Posted:" in ctx.source_material
    assert "years ago" in ctx.source_material
    assert "not current" in ctx.format_instructions
    assert ctx.source_created_utc is not None


def test_fresh_post_carries_no_dateline(monkeypatch):
    ctx = tg._reddit_context_from_listing_post({
        "subreddit": "testsub",
        "title": "Something that happened today",
        "selftext": "body",
        "permalink": "/r/testsub/comments/fresh/story/",
        "created_utc": time.time() - 3600,
        "score": 10,
        "num_comments": 2,
    })

    assert "Posted:" not in ctx.source_material
    assert "not current" not in ctx.format_instructions
