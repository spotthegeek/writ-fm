"""OLLAMA_THINK: the reasoning mode for calls that do not choose one.

Gemma 4 with thinking on spends all of num_predict thinking and returns an empty
response, so a station on Gemma sets OLLAMA_THINK=false. Explicit callers still win.
"""

from __future__ import annotations

import io
import json

import pytest

from station.content_generator import helpers


@pytest.fixture
def sent(monkeypatch):
    payloads: list[dict] = []

    def fake_urlopen(req, timeout=None):
        payloads.append(json.loads(req.data.decode()))
        return io.BytesIO(json.dumps({"response": "A perfectly good script."}).encode())

    monkeypatch.setattr(helpers, "_ollama_unavailable_since", None)
    monkeypatch.setenv("OLLAMA_URL", "http://ollama.invalid:11434")
    monkeypatch.setattr(helpers.urllib.request, "urlopen", fake_urlopen)
    return payloads


def test_unset_leaves_think_out_of_the_request(sent, monkeypatch):
    monkeypatch.delenv("OLLAMA_THINK", raising=False)
    helpers.run_claude("hello")
    assert "think" not in sent[-1]


def test_false_turns_thinking_off_for_unspecified_calls(sent, monkeypatch):
    monkeypatch.setenv("OLLAMA_THINK", "false")
    helpers.run_claude("hello")
    assert sent[-1]["think"] is False


def test_an_explicit_caller_still_wins(sent, monkeypatch):
    monkeypatch.setenv("OLLAMA_THINK", "false")
    helpers.run_claude("hello", think=True)
    assert sent[-1]["think"] is True
