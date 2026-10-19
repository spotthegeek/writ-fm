"""The listing size estimate counts only the comments a thread fetch will keep."""

from __future__ import annotations

from station.content_generator import talk_generator as tg


def test_a_busy_link_post_is_not_credited_for_comments_it_will_not_get():
    post = {"selftext": "", "num_comments": 500}
    assert tg._listing_post_source_words(post, count_comments=True) == tg.REDDIT_COMMENT_LIMIT * tg._COMMENT_WORD_ALLOWANCE


def test_comments_are_ignored_when_the_segment_ignores_them():
    post = {"selftext": "one two three", "num_comments": 500}
    assert tg._listing_post_source_words(post, count_comments=False) == 3
