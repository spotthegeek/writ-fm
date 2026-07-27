from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]

for path in (
    ROOT,
    ROOT / "admin",
    ROOT / "station",
    ROOT / "station" / "content_generator",
):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)


@pytest.fixture(autouse=True)
def _sandbox_generator_state(tmp_path, monkeypatch):
    """Keep the suite out of the real `output/` tree.

    Individual tests patch `SCRIPTS_DIR` when they care about it; the ones that
    do not were writing rotation state into the live station — which is how
    `output/scripts/` ended up with a forked `.youtube-ai_` / `.youtube_ai_`
    pair. 3.5 raised the stakes: `output/state/` is now the used-source ledger,
    and a stray test write there would tell the generator a source was used when
    it was not. Suite-wide default, overridable by any test that sets its own.
    """
    try:
        from station.content_generator import talk_generator
    except Exception:  # a test collection that never imports the generator
        return
    sandbox = tmp_path / "generator_state"
    monkeypatch.setattr(talk_generator, "SCRIPTS_DIR", sandbox / "scripts", raising=False)
    monkeypatch.setattr(talk_generator, "STATE_DIR", sandbox / "state", raising=False)
