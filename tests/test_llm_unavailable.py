"""An unreachable LLM host must not freeze the station's content for hours.

The Mac that serves Ollama stops it while a video render runs. Before this, every
generation in that window failed as though its source were broken, walked the
failure ladder (30 min → 1 h → 4 h → 12 h), and a two-hour render could leave every
show without new material for half a day afterwards.
"""
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "admin"))

import scheduler  # noqa: E402
from station.content_generator import helpers  # noqa: E402


# ── scheduler ─────────────────────────────────────────────────────────────────

def test_unavailable_backoff_is_flat_and_short():
    assert {scheduler._backoff_seconds("unavailable", n) for n in range(1, 8)} == {600}


def test_failure_ladder_is_unchanged():
    assert [scheduler._backoff_seconds("failed", n) for n in (1, 2, 3, 4, 9)] == [
        1800, 3600, 14400, 43200, 43200]


def test_marker_is_classified():
    lines = ["[10:00:01] Generating segment", f"[10:00:02] {helpers.LLM_UNAVAILABLE_MARKER}: refused"]
    assert scheduler._output_looks_unavailable(lines) is True
    assert scheduler._output_looks_unavailable(["[10:00:02] Ollama error: 404"]) is False


def test_unavailable_is_not_recorded_as_a_failure():
    st = scheduler.SchedulerState()
    for _ in range(5):
        assert st.record_unavailable("show", "talk") == 600
    assert st.last_failure("show", "talk") is None
    kind, left = st.backoff_state("show", "talk")
    assert kind == "unavailable" and 0 < left <= 600


def test_outage_after_failures_resets_to_the_short_wait():
    st = scheduler.SchedulerState()
    for _ in range(3):
        st.record_failure("show", "talk")
    assert st.record_unavailable("show", "talk") == 600


# ── helpers.run_claude ────────────────────────────────────────────────────────

@pytest.fixture
def no_fallbacks(monkeypatch):
    """Ollama only: the claude and gemini CLI fallbacks are not under test."""
    monkeypatch.setattr(helpers.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(helpers.shutil, "which", lambda *_: None)
    monkeypatch.setattr(helpers, "_ollama_unavailable_since", None)
    monkeypatch.setenv("OLLAMA_URL", "http://ollama.invalid:11434")


def test_refused_connection_prints_the_marker_and_skips_ollama_after(no_fallbacks, monkeypatch, capsys):
    calls = []

    def refuse(*_a, **_k):
        calls.append(1)
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr(helpers.urllib.request, "urlopen", refuse)
    assert helpers.run_claude("hello") is None
    assert helpers.LLM_UNAVAILABLE_MARKER in capsys.readouterr().out
    assert helpers.run_claude("hello again") is None
    assert len(calls) == 1  # the second call went straight to the fallbacks


def test_server_stopped_mid_request_counts_as_unavailable(no_fallbacks, monkeypatch, capsys):
    def dropped(*_a, **_k):
        raise ConnectionResetError(104, "Connection reset by peer")

    monkeypatch.setattr(helpers.urllib.request, "urlopen", dropped)
    helpers.run_claude("hello")
    assert helpers.LLM_UNAVAILABLE_MARKER in capsys.readouterr().out


def test_missing_model_is_a_real_error(no_fallbacks, monkeypatch, capsys):
    def not_found(*_a, **_k):
        raise urllib.error.HTTPError("http://x/api/generate", 404, "model not found", {}, None)

    monkeypatch.setattr(helpers.urllib.request, "urlopen", not_found)
    helpers.run_claude("hello")
    out = capsys.readouterr().out
    assert helpers.LLM_UNAVAILABLE_MARKER not in out and "Ollama error" in out
    assert helpers._ollama_unavailable_since is None


def test_slow_model_is_not_an_outage(no_fallbacks, monkeypatch, capsys):
    def slow(*_a, **_k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(helpers.urllib.request, "urlopen", slow)
    helpers.run_claude("hello")
    assert helpers.LLM_UNAVAILABLE_MARKER not in capsys.readouterr().out
