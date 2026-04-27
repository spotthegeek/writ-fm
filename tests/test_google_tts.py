from __future__ import annotations

import json
import socket
from pathlib import Path

from station import google_tts


def test_generate_speech_retries_timeout_then_succeeds(monkeypatch, tmp_path: Path) -> None:
    calls = {"count": 0}
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "inlineData": {
                                "data": "AAABAA==",
                            }
                        }
                    ]
                }
            }
        ]
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    def fake_urlopen(req, timeout):
        calls["count"] += 1
        if calls["count"] < 3:
            raise socket.timeout("timed out")
        return FakeResponse()

    monkeypatch.setattr(google_tts, "google_tts_api_key", lambda: "test-key")
    monkeypatch.setattr(google_tts, "google_tts_max_retries", lambda: 3)
    monkeypatch.setattr(google_tts, "google_tts_timeout_seconds", lambda: 1.0)
    monkeypatch.setattr(google_tts.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(google_tts.time, "sleep", lambda seconds: None)

    out = tmp_path / "sample.wav"
    ok = google_tts.generate_speech("Testing", out, voice_id="Kore")

    assert ok is True
    assert calls["count"] == 3
    assert out.exists()
