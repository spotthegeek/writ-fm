"""Arctic Shift as a Reddit source.

On 2026-11-14 Reddit began answering every app-only OAuth call, and anonymous
JSON, with 403 "Blocked"; PullPush had been returning 521 for weeks. nosleep,
sysadmin and talesfromtechsupport generated nothing for a week. Reddit's public
API closes entirely in March 2027. These cover reading listings and threads
from Arctic Shift instead: as the only source (WRIT_REDDIT_SOURCE=arctic_shift)
and as the first fallback behind a refusing Reddit API.
"""

from __future__ import annotations

import time

import pytest

from station.content_generator import talk_generator as tg


DAY = 86400
# Captured at import, before conftest's per-test fixture swaps in its guard.
_REAL_ARCTIC_GET = tg._arctic_get


def _post(post_id: str, *, age_days: float = 1.0, score: int = 10, comments: int = 5,
          words: int = 400, sub: str = "testsub", **extra) -> dict:
    post = {
        "id": post_id,
        "subreddit": sub,
        "permalink": f"/r/{sub}/comments/{post_id}/story/",
        "title": f"Title for {post_id}",
        "selftext": " ".join(["word"] * words),
        "created_utc": time.time() - age_days * DAY,
        "num_comments": comments,
        "score": score,
        "author": "someone",
    }
    post.update(extra)
    return post


def _key(post_id: str, sub: str = "testsub") -> str:
    return tg._canonical_source_key(
        "reddit_thread", f"{tg._reddit_thread_base()}/r/{sub}/comments/{post_id}/story/"
    )


@pytest.fixture
def arctic(monkeypatch):
    """Serve canned Arctic Shift responses and record every request."""
    calls: list[tuple[str, dict]] = []
    state = {"posts": [], "threads": {}, "comments": {}}

    def _get(path, params):
        calls.append((path, dict(params)))
        if path == "posts/search":
            after = float(params.get("after") or 0)
            before = float(params.get("before") or 0) or float("inf")
            rows = [p for p in state["posts"] if after < p["created_utc"] < before]
            rows.sort(key=lambda p: p["created_utc"], reverse=True)
            return rows[: int(params.get("limit", 100))]
        if path == "posts/ids":
            return [p for p in state["posts"] if p["id"] == params["ids"]]
        if path == "comments/tree":
            return state["comments"].get(params["link_id"], [])
        raise AssertionError(f"unexpected Arctic Shift path {path}")

    def _no_reddit(url, timeout=None):
        raise AssertionError(f"Reddit was called in arctic_shift mode: {url}")

    monkeypatch.setattr(tg, "_arctic_get", _get)
    monkeypatch.setattr(tg, "_fetch_url_reddit", _no_reddit)
    monkeypatch.setattr(tg, "_min_source_words_for", lambda segment_type: 0)
    return calls, state


def test_mode_switch_reads_its_env(monkeypatch):
    assert tg._reddit_source_mode() == "reddit"
    monkeypatch.setenv("WRIT_REDDIT_SOURCE", "arctic-shift")
    assert tg._reddit_source_mode() == "arctic_shift"
    monkeypatch.setenv("WRIT_REDDIT_SOURCE", "something else")
    assert tg._reddit_source_mode() == "reddit"


def test_arctic_mode_never_calls_reddit_and_ranks_top_by_score(arctic, monkeypatch):
    calls, state = arctic
    monkeypatch.setenv("WRIT_REDDIT_SOURCE", "arctic_shift")
    state["posts"] = [_post("low", score=3), _post("high", score=900), _post("mid", score=50)]
    state["comments"]["high"] = []

    ctx = tg._fetch_reddit_subreddit_context_with_strategy("/r/testsub", 7, "top", set())

    assert ctx.title == "Title for high"
    assert calls[0][0] == "posts/search"
    assert float(calls[0][1]["after"]) == pytest.approx(time.time() - 7 * DAY, abs=5)


def test_arctic_mode_skips_used_posts(arctic, monkeypatch):
    _, state = arctic
    monkeypatch.setenv("WRIT_REDDIT_SOURCE", "arctic_shift")
    state["posts"] = [_post("a", score=900), _post("b", score=50)]

    ctx = tg._fetch_reddit_subreddit_context_with_strategy("/r/testsub", 7, "top", {_key("a")})

    assert ctx.title == "Title for b"


def test_latest_is_newest_first_even_though_new_posts_score_one(arctic, monkeypatch):
    _, state = arctic
    monkeypatch.setenv("WRIT_REDDIT_SOURCE", "arctic_shift")
    state["posts"] = [_post("old", age_days=3, score=500), _post("fresh", age_days=0.1, score=1)]

    ctx = tg._fetch_reddit_subreddit_context_with_strategy("/r/testsub", 7, "latest", set())

    assert ctx.title == "Title for fresh"


def test_thread_comes_with_best_comments_and_no_stickies(arctic, monkeypatch):
    _, state = arctic
    monkeypatch.setenv("WRIT_REDDIT_SOURCE", "arctic_shift")
    state["posts"] = [_post("t1")]
    state["comments"]["t1"] = [
        {"kind": "t1", "data": {"author": "mod", "body": "Rules reminder", "score": 999, "stickied": True}},
        {"kind": "t1", "data": {"author": "meh", "body": "fine", "score": 2}},
        {"kind": "t1", "data": {"author": "best", "body": "brilliant", "score": 250}},
    ]

    ctx = tg._fetch_reddit_thread_context("https://www.reddit.com/r/testsub/comments/t1/story/")

    assert ctx.comments[0].startswith("- best (250 upvotes)")
    assert all("Rules reminder" not in c for c in ctx.comments)
    assert len(ctx.comments) == 2
    assert ctx.subreddit == "testsub"


def test_thread_survives_missing_comments(arctic, monkeypatch):
    _, state = arctic
    monkeypatch.setenv("WRIT_REDDIT_SOURCE", "arctic_shift")
    state["posts"] = [_post("t2")]

    ctx = tg._fetch_reddit_thread_context("https://www.reddit.com/r/testsub/comments/t2/story/")

    assert ctx.comments == []
    assert ctx.title == "Title for t2"


def test_reddit_mode_falls_back_to_arctic_when_reddit_refuses(arctic, monkeypatch):
    calls, state = arctic

    def _blocked(url, timeout=None):
        raise RuntimeError("HTTP Error 403: Blocked")

    monkeypatch.setattr(tg, "_fetch_url_reddit", _blocked)
    monkeypatch.setattr(
        tg, "_pullpush_fetch_subreddit_posts",
        lambda *a, **k: pytest.fail("PullPush should not be needed when Arctic Shift answers"),
    )
    state["posts"] = [_post("rescued")]

    ctx = tg._fetch_reddit_subreddit_context_with_strategy("/r/testsub", 7, "top", set())

    assert ctx.title == "Title for rescued"
    # The thread too came from Arctic Shift once Reddit refused it.
    assert ("posts/ids", {"ids": "rescued"}) in calls


def test_pages_back_in_time_and_caches_the_listing(arctic):
    calls, state = arctic
    state["posts"] = [_post(f"p{i}", age_days=i * 0.01) for i in range(250)]

    first = tg._arctic_fetch_posts("testsub", after=time.time() - 7 * DAY)
    second = tg._arctic_fetch_posts("testsub", after=time.time() - 7 * DAY)

    assert len(first) == 250
    assert len({p["id"] for p in first}) == 250
    searches = [c for c in calls if c[0] == "posts/search"]
    assert len(searches) == 3  # 100 + 100 + 50, then stop on the short page
    assert "before" not in searches[0][1] and "before" in searches[1][1]
    assert second is first  # served from the per-process cache


def test_archive_widening_reads_posts_older_than_the_window(arctic, monkeypatch):
    calls, state = arctic
    monkeypatch.setenv("WRIT_REDDIT_SOURCE", "arctic_shift")
    monkeypatch.setattr(tg, "_archive_fallback_allowed", lambda: True)
    state["posts"] = [_post("recent", age_days=1), _post("classic", age_days=200, score=5000)]

    ctx = tg._fetch_reddit_subreddit_context_with_strategy("/r/testsub", 7, "top", {_key("recent")})

    assert ctx.title == "Title for classic"
    archive_calls = [c for c in calls if c[0] == "posts/search" and "before" in c[1] and "after" not in c[1]]
    assert archive_calls


def test_joke_bundle_reads_arctic_in_arctic_mode(arctic, monkeypatch):
    _, state = arctic
    monkeypatch.setenv("WRIT_REDDIT_SOURCE", "arctic_shift")
    state["posts"] = [
        _post(f"j{i}", sub="darkjokes", words=8, title=f"A perfectly serviceable joke number {i}")
        for i in range(6)
    ]
    used: set[str] = set()

    ctx = tg._fetch_reddit_bundle("/r/darkjokes", lookback_days=7, used_source_keys=used, n_min=5, n_max=5)

    assert ctx is not None
    assert len(used) == 5


def test_nsfw_opt_in_without_reddit_credentials(monkeypatch):
    monkeypatch.setattr(tg, "REDDIT_CLIENT_ID", "")
    monkeypatch.setattr(tg, "REDDIT_CLIENT_SECRET", "")
    assert tg._reddit_allow_nsfw() is False
    monkeypatch.setenv("WRIT_REDDIT_ALLOW_NSFW", "1")
    assert tg._reddit_allow_nsfw() is True


def test_busy_arctic_shift_is_retried(monkeypatch):
    import urllib.error

    monkeypatch.setattr(tg, "_arctic_get", _REAL_ARCTIC_GET)
    answers = [urllib.error.HTTPError("u", 422, "Unprocessable Entity", {}, None),
               b'{"data": [{"id": "ok"}]}']
    sleeps: list[float] = []

    def _fetch(url, timeout=None):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(tg, "_fetch_url", _fetch)
    monkeypatch.setattr(tg.time, "sleep", sleeps.append)

    assert tg._arctic_get("posts/search", {"subreddit": "x"}) == [{"id": "ok"}]
    assert sleeps == [2.0]


def test_a_failed_later_page_keeps_the_pages_already_read(arctic, monkeypatch):
    calls, state = arctic
    state["posts"] = [_post(f"p{i}", age_days=i * 0.01) for i in range(150)]
    real = tg._arctic_get

    def _second_page_fails(path, params):
        if "before" in params:
            raise RuntimeError("HTTP Error 422")
        return real(path, params)

    monkeypatch.setattr(tg, "_arctic_get", _second_page_fails)

    posts = tg._arctic_fetch_posts("testsub", after=time.time() - 7 * DAY)

    assert len(posts) == 100

