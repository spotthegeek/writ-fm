"""A slot whose source fails the size gate tries the next source instead of being spent.

Seen 2026-10-19: `--show sysadmin --count 1` generated nothing, because its only slot
went on rejecting the same 179-word post the listing had over-estimated.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from station.content_generator import talk_generator as tg


def _ctx(post_id: str) -> tg.SourceContext:
    return tg.SourceContext(source_type="reddit", source_value=f"https://www.reddit.com/r/t/comments/{post_id}/x/",
                            title=post_id, topic=post_id, source_material="text")


def _run(monkeypatch, sources, thin, count=1):
    picks = iter(sources)
    calls = []
    rule = {"type": "reddit_subreddit", "value": "/r/t/", "lookback_days": 7,
            "selection_strategy": "top", "segment_type": "reddit_post"}
    monkeypatch.setattr(tg, "_ordered_source_rules_for_show", lambda *a, **k: [rule])
    monkeypatch.setattr(tg, "load_source_context", lambda *a, **k: next(picks))
    monkeypatch.setattr(tg, "_used_source_keys_for_show", lambda show_id: set())
    monkeypatch.setattr(tg, "_record_source_rotation", lambda *a, **k: None)
    monkeypatch.setattr(tg.time, "sleep", lambda *_: None)

    def fake_generate(show, st, *, source_context=None, used_source_keys=None, **_):
        calls.append(source_context.title)
        if source_context.title in thin:  # what the size gate does
            used_source_keys.add(tg._canonical_source_key(source_context.source_type, source_context.source_value))
            return None
        return Path(f"/tmp/{source_context.title}.wav")

    monkeypatch.setattr(tg, "generate_segment", fake_generate)
    show = SimpleNamespace(name="T", segment_types=["reddit_post"], source_rules=[rule], show_id="t")
    schedule = SimpleNamespace(shows={"t": show}, station_name="Test FM")
    return tg.generate_for_show("t", schedule, count=count), calls


def test_a_rejected_source_does_not_spend_the_slot(monkeypatch):
    success, calls = _run(monkeypatch, [_ctx("thin"), _ctx("good")], thin={"thin"})
    assert success == 1
    assert calls == ["thin", "good"]


def test_retries_are_bounded(monkeypatch):
    sources = [_ctx(f"thin{n}") for n in range(10)]
    success, calls = _run(monkeypatch, sources, thin={f"thin{n}" for n in range(10)})
    assert success == 0
    assert len(calls) == 4  # the slot, plus three retries
