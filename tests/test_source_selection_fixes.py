"""Source selection after a post is rejected, and joke bundles chosen up front.

The failures these cover, all seen on air on 2026-10-19:
- r/sysadmin: one 179-word post passed the listing's size estimate, failed the
  real size check, and was picked again for every segment of the run.
- `used_source_keys or set()` swapped an empty caller's set for a private one,
  so keys recorded during a run were lost when the show had no history yet.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

from station.content_generator import talk_generator as tg


def _joke(post_id: str) -> dict:
    return {
        "id": post_id,
        "permalink": f"/r/testsub/comments/{post_id}/joke/",
        "title": f"A perfectly serviceable joke number {post_id}",
        "selftext": "The punchline.",
        "created_utc": time.time(),
    }


def test_bundle_records_used_posts_in_an_empty_callers_set(monkeypatch):
    payload = json.dumps({"data": {"children": [{"data": _joke(str(i))} for i in range(6)]}}).encode()
    monkeypatch.setattr(tg, "_fetch_url_reddit", lambda url, timeout=None: payload)
    used: set[str] = set()
    ctx = tg._fetch_reddit_bundle("/r/testsub", used_source_keys=used, n_min=5, n_max=5)
    assert ctx is not None
    assert len(used) == 5


def test_too_small_source_is_remembered_for_the_rest_of_the_run():
    permalink = f"{tg._reddit_thread_base()}/r/testsub/comments/abc123/short/"
    ctx = tg.SourceContext(
        source_type="reddit",
        source_value=permalink,
        title="A short post",
        topic="A short post",
        source_material="Far too short to fill a segment.",
    )
    used: set[str] = set()
    show = SimpleNamespace(show_id="testshow", name="Test Show")
    result = tg.generate_segment(
        show, "reddit_post",
        source_type="reddit", source_value=permalink,
        source_context=ctx, used_source_keys=used, station_name="Test FM",
    )
    assert result is None
    assert tg._canonical_source_key("reddit_thread", permalink) in used
