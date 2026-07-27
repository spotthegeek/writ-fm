"""Phase 3a/3b — the fetch window is no longer one page and the dedupe window
is no longer forever.

Covers the failure mode these tasks exist for: a slow subreddit or a quiet
YouTube channel whose recent posts are all already used, which previously
raised "No unused posts found" and became a failed generation job.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

import pytest

from station.content_generator import talk_generator as tg


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _post(post_id: str, *, created: float | None = None, words: int = 400, score: int = 10) -> dict:
    return {
        "id": post_id,
        "permalink": f"/r/testsub/comments/{post_id}/story/",
        "title": f"Title for {post_id}",
        "selftext": " ".join(["word"] * words),
        "created_utc": time.time() if created is None else created,
        "num_comments": 5,
        "score": score,
    }


def _permalink(post_id: str) -> str:
    return f"{tg._reddit_thread_base()}/r/testsub/comments/{post_id}/story/"


def _key(post_id: str) -> str:
    return tg._canonical_source_key("reddit_thread", _permalink(post_id))


def _listing_payload(posts: list[dict], after: str | None = None) -> bytes:
    return json.dumps(
        {"data": {"after": after, "children": [{"data": post} for post in posts]}}
    ).encode()


@pytest.fixture
def fetch_recorder(monkeypatch):
    """Serve canned listing pages by URL and record every URL requested."""
    calls: list[str] = []
    pages: dict[str, bytes] = {}

    def _fetch(url, timeout=None):
        calls.append(url)
        for prefix, payload in pages.items():
            if prefix in url:
                return payload
        raise AssertionError(f"unexpected fetch: {url}")

    monkeypatch.setattr(tg, "_fetch_url_reddit", _fetch)
    # Selecting a post normally fetches the full thread; short-circuit to the
    # listing fallback so these tests exercise selection, not thread parsing.
    monkeypatch.setattr(
        tg, "_fetch_reddit_thread_context",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("thread fetch stubbed out")),
    )
    return calls, pages


# ---------------------------------------------------------------------------
# 3.1 — page past the first listing page
# ---------------------------------------------------------------------------


def test_listing_pages_past_the_first_when_page_one_is_all_used(fetch_recorder, monkeypatch):
    calls, pages = fetch_recorder
    monkeypatch.setattr(tg, "REDDIT_LISTING_PAGE_SIZE", 3)
    monkeypatch.setattr(tg, "REDDIT_MAX_LISTING_PAGES", 4)

    page_one = [_post("a1"), _post("a2"), _post("a3")]
    page_two = [_post("b1")]
    pages["after=t3_a3"] = _listing_payload(page_two)
    pages["/r/testsub/new.json"] = _listing_payload(page_one, after="t3_a3")

    ctx = tg._fetch_reddit_subreddit_context_with_strategy(
        "/r/testsub",
        lookback_days=7,
        selection_strategy="latest",
        used_source_keys={_key("a1"), _key("a2"), _key("a3")},
    )

    assert ctx.source_value == _permalink("b1")
    assert any("after=t3_a3" in url for url in calls), "never requested the second page"


def test_listing_stops_at_the_page_cap(fetch_recorder, monkeypatch):
    calls, pages = fetch_recorder
    monkeypatch.setattr(tg, "REDDIT_MAX_LISTING_PAGES", 2)
    # Every page is the same used post and always offers another cursor.
    pages["/r/testsub/"] = _listing_payload([_post("a1")], after="t3_a1")

    with pytest.raises(RuntimeError):
        tg._fetch_reddit_subreddit_context_with_strategy(
            "/r/testsub", used_source_keys={_key("a1")}
        )

    # 2 pages for the recent scan, 2 for the top/all archive scan.
    assert len(calls) == 4


def test_single_page_of_fresh_posts_costs_one_request(fetch_recorder):
    calls, pages = fetch_recorder
    pages["/r/testsub/new.json"] = _listing_payload([_post("a1")], after="t3_a1")

    ctx = tg._fetch_reddit_subreddit_context_with_strategy("/r/testsub", used_source_keys=set())

    assert ctx.source_value == _permalink("a1")
    assert len(calls) == 1, "paged further than necessary on a healthy subreddit"


def test_chronological_scan_stops_once_a_page_falls_out_of_the_window(fetch_recorder):
    """On a newest-first listing an all-stale page means every later page is
    older still, so paging on only burns requests."""
    calls, pages = fetch_recorder
    old = time.time() - 90 * 86400
    pages["after=t3_a1"] = _listing_payload([_post("b1", created=old)], after="t3_b1")
    pages["/r/testsub/new.json"] = _listing_payload([_post("a1")], after="t3_a1")

    with pytest.raises(RuntimeError):
        tg._fetch_reddit_subreddit_context_with_strategy(
            "/r/testsub", lookback_days=7, used_source_keys={_key("a1"), _key("b1")}
        )

    recent_pages = [url for url in calls if "top.json" not in url]
    assert len(recent_pages) == 2, "kept paging past the lookback window"


# ---------------------------------------------------------------------------
# 3.2 — archive fallback when the recent window is dry
# ---------------------------------------------------------------------------


def test_falls_back_to_top_all_when_the_recent_window_is_exhausted(fetch_recorder):
    calls, pages = fetch_recorder
    archived = time.time() - 3 * 365 * 86400
    pages["top.json"] = _listing_payload([_post("old1", created=archived)])
    pages["/r/testsub/new.json"] = _listing_payload([_post("a1")])

    ctx = tg._fetch_reddit_subreddit_context_with_strategy(
        "/r/testsub", lookback_days=7, used_source_keys={_key("a1")}
    )

    assert ctx.source_value == _permalink("old1")
    assert any("t=all" in url for url in calls), "never widened to the archive"


def test_archive_fallback_ignores_the_lookback_cutoff(fetch_recorder):
    """The whole point of the archive pass: posts far older than lookback_days
    must be eligible, or a 15-year subreddit stays exhausted."""
    calls, pages = fetch_recorder
    pages["top.json"] = _listing_payload([_post("old1", created=time.time() - 4000 * 86400)])
    pages["/r/testsub/new.json"] = _listing_payload([])

    ctx = tg._fetch_reddit_subreddit_context_with_strategy("/r/testsub", lookback_days=1)

    assert ctx.source_value == _permalink("old1")


def test_archive_is_not_consulted_when_the_recent_window_yields(fetch_recorder):
    calls, pages = fetch_recorder
    pages["/r/testsub/new.json"] = _listing_payload([_post("a1")])

    tg._fetch_reddit_subreddit_context_with_strategy("/r/testsub", used_source_keys=set())

    assert not any("t=all" in url for url in calls)


def test_exhausted_everywhere_still_raises(fetch_recorder):
    calls, pages = fetch_recorder
    pages["/r/testsub/"] = _listing_payload([_post("a1")])

    with pytest.raises(RuntimeError, match="No unused posts"):
        tg._fetch_reddit_subreddit_context_with_strategy(
            "/r/testsub", used_source_keys={_key("a1")}
        )


def test_blocked_listing_still_falls_back_to_pullpush(monkeypatch):
    """The pre-existing PullPush path must survive the rewrite."""
    def _blocked(url, timeout=None):
        raise RuntimeError("403")

    monkeypatch.setattr(tg, "_fetch_url_reddit", _blocked)
    monkeypatch.setattr(
        tg, "_pullpush_fetch_subreddit_posts", lambda *a, **k: [_post("pp1")]
    )
    monkeypatch.setattr(
        tg, "_fetch_reddit_thread_context",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stubbed")),
    )

    ctx = tg._fetch_reddit_subreddit_context_with_strategy("/r/testsub")

    assert ctx.source_value == _permalink("pp1")


def test_too_small_posts_are_still_skipped(fetch_recorder, monkeypatch):
    calls, pages = fetch_recorder
    monkeypatch.setattr(tg, "_min_source_words_for", lambda segment_type: 300)
    monkeypatch.setattr(tg, "_segment_counts_comments", lambda segment_type: False)
    pages["/r/testsub/"] = _listing_payload([_post("tiny", words=10)])

    with pytest.raises(RuntimeError, match="large enough"):
        tg._fetch_reddit_subreddit_context_with_strategy(
            "/r/testsub", segment_type="reddit_post"
        )


# ---------------------------------------------------------------------------
# 3.3 — YouTube collection widening
# ---------------------------------------------------------------------------


def _yt_entry(video_id: str, *, upload_date: str | None = None) -> dict:
    entry = {"id": video_id, "title": f"Video {video_id}"}
    if upload_date:
        entry["upload_date"] = upload_date
    return entry


@pytest.fixture
def yt_dlp_stub(monkeypatch):
    state: dict = {"entries": [], "args": []}

    class _Result:
        returncode = 0

        def __init__(self, stdout):
            self.stdout = stdout
            self.stderr = ""

    def _run(args, timeout=300):
        state["args"] = args
        return _Result(json.dumps({"entries": state["entries"]}))

    monkeypatch.setattr(tg, "_run_yt_dlp", _run)
    return state


def test_youtube_scans_deeper_than_the_old_twelve(yt_dlp_stub, monkeypatch):
    monkeypatch.setattr(tg, "YOUTUBE_COLLECTION_DEPTH", 50)
    yt_dlp_stub["entries"] = [_yt_entry("v1")]

    tg._select_youtube_video_url_from_collection("https://www.youtube.com/@chan")

    args = yt_dlp_stub["args"]
    assert args[args.index("--playlist-end") + 1] == "50"


def test_youtube_falls_back_to_the_back_catalogue(yt_dlp_stub):
    today = datetime.now().strftime("%Y%m%d")
    old = (datetime.now() - timedelta(days=200)).strftime("%Y%m%d")
    yt_dlp_stub["entries"] = [
        _yt_entry("recent", upload_date=today),
        _yt_entry("archived", upload_date=old),
    ]
    used = {tg._canonical_source_key("youtube_video", "https://www.youtube.com/watch?v=recent")}

    url = tg._select_youtube_video_url_from_collection(
        "https://www.youtube.com/@chan", lookback_days=1, used_source_keys=used
    )

    assert url == "https://www.youtube.com/watch?v=archived"


def test_youtube_prefers_the_recent_window_when_it_has_something(yt_dlp_stub):
    today = datetime.now().strftime("%Y%m%d")
    old = (datetime.now() - timedelta(days=200)).strftime("%Y%m%d")
    yt_dlp_stub["entries"] = [
        _yt_entry("archived", upload_date=old),
        _yt_entry("recent", upload_date=today),
    ]

    url = tg._select_youtube_video_url_from_collection(
        "https://www.youtube.com/@chan", lookback_days=1
    )

    assert url == "https://www.youtube.com/watch?v=recent"


def test_youtube_entries_without_dates_are_treated_as_recent(yt_dlp_stub):
    """Flat-playlist entries often carry neither timestamp nor upload_date."""
    yt_dlp_stub["entries"] = [_yt_entry("v1")]

    url = tg._select_youtube_video_url_from_collection(
        "https://www.youtube.com/@chan", lookback_days=1
    )

    assert url == "https://www.youtube.com/watch?v=v1"


def test_youtube_all_used_still_raises(yt_dlp_stub):
    yt_dlp_stub["entries"] = [_yt_entry("v1")]
    used = {tg._canonical_source_key("youtube_video", "https://www.youtube.com/watch?v=v1")}

    with pytest.raises(RuntimeError, match="No unused YouTube entries"):
        tg._select_youtube_video_url_from_collection(
            "https://www.youtube.com/@chan", used_source_keys=used
        )


# ---------------------------------------------------------------------------
# 3.4 — the used-source ledger expires (decision B: 90 days)
# ---------------------------------------------------------------------------


def _write_script(scripts_dir, show_id: str, permalink: str, when: datetime) -> None:
    name = f"talk_reddit_post_{when.strftime('%Y%m%d_%H%M%S')}.json"
    (scripts_dir / name).write_text(json.dumps({
        "show_id": show_id,
        "source_type": "reddit",
        "source_value": permalink,
    }))


@pytest.fixture
def scripts_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tg, "SCRIPTS_DIR", tmp_path)
    return tmp_path


def test_default_window_is_ninety_days(monkeypatch):
    monkeypatch.delenv("WRIT_SOURCE_REUSE_DAYS", raising=False)
    assert tg._source_reuse_window_days() == 90


def test_key_just_inside_the_window_is_still_used(scripts_dir, monkeypatch):
    monkeypatch.setenv("WRIT_SOURCE_REUSE_DAYS", "90")
    _write_script(scripts_dir, "nosleep", _permalink("a1"), datetime.now() - timedelta(days=89))

    assert _key("a1") in tg._used_source_keys_for_show("nosleep")


def test_key_past_the_window_expires(scripts_dir, monkeypatch):
    monkeypatch.setenv("WRIT_SOURCE_REUSE_DAYS", "90")
    _write_script(scripts_dir, "nosleep", _permalink("a1"), datetime.now() - timedelta(days=91))

    assert _key("a1") not in tg._used_source_keys_for_show("nosleep")


def test_window_of_zero_disables_expiry(scripts_dir, monkeypatch):
    """The pre-Phase-3b behaviour stays available as an escape hatch."""
    monkeypatch.setenv("WRIT_SOURCE_REUSE_DAYS", "0")
    _write_script(scripts_dir, "nosleep", _permalink("a1"), datetime.now() - timedelta(days=800))

    assert _key("a1") in tg._used_source_keys_for_show("nosleep")


def test_expiry_does_not_leak_across_shows(scripts_dir, monkeypatch):
    monkeypatch.setenv("WRIT_SOURCE_REUSE_DAYS", "90")
    _write_script(scripts_dir, "sysadmin", _permalink("a1"), datetime.now() - timedelta(days=1))

    assert tg._used_source_keys_for_show("nosleep") == set()
    assert _key("a1") in tg._used_source_keys_for_show("sysadmin")


def test_undated_filename_falls_back_to_mtime(scripts_dir, monkeypatch):
    monkeypatch.setenv("WRIT_SOURCE_REUSE_DAYS", "90")
    path = scripts_dir / "talk_legacy_name.json"
    path.write_text(json.dumps({
        "show_id": "nosleep",
        "source_type": "reddit",
        "source_value": _permalink("a1"),
    }))
    import os as _os
    stale = time.time() - 200 * 86400
    _os.utime(path, (stale, stale))

    assert _key("a1") not in tg._used_source_keys_for_show("nosleep")


def test_rotation_state_files_are_never_read_as_scripts(scripts_dir, monkeypatch):
    """`.<show>_source_rotation.json` lives in the same directory but is not a
    script record — the glob must keep missing it."""
    monkeypatch.setenv("WRIT_SOURCE_REUSE_DAYS", "90")
    (scripts_dir / ".nosleep_source_rotation.json").write_text(
        json.dumps({"last_key": "reddit_subreddit:nosleep"})
    )

    assert tg._used_source_keys_for_show("nosleep") == set()
