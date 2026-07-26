# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies (uses uv package manager)
uv sync

# Run services
uv run python admin/app.py           # Admin UI on port 8080
uv run python station/api_server.py  # Now-playing API on port 8001
uv run python station/stream_gapless.py  # Gapless streamer → Icecast

# Tests
./.venv/bin/pytest -q                # Run all 25 regression tests

# Operator (automated maintenance loop)
./run_operator.sh                    # One-shot Gemini CLI run
bash station/operator_daemon.sh      # Persistent loop (every 15 min)
```

Production uses systemd: `writ-fm.service` (streamer) and `writ-fm-admin.service` (admin UI, WorkingDirectory=`admin/`).

## Architecture

WRIT-FM is a 24/7 AI radio station: a schedule-driven gapless streamer feeds Icecast; a background scheduler triggers async content generation when inventory falls below threshold.

```
Icecast2 (port 8000)
    ^ ffmpeg pipe
station/stream_gapless.py     — reads schedule, picks segments, pipes audio
    ├── output/talk_segments/{show}/   — WAV + JSON sidecars
    └── output/music_bumpers/{show}/   — MP3 + JSON sidecars

admin/app.py (port 8080)      — admin UI + background scheduler
    └── admin/scheduler.py    — triggers generation jobs when stock is low

station/api_server.py (port 8001)  — /now-playing, /schedule, /message

Content generation (subprocesses):
    station/content_generator/talk_generator.py
    station/content_generator/music_bumper_generator.py

TTS pipeline:
    LLM (Ollama) → script → Kokoro (local ONNX) → WAV
                           → Google Gemini TTS   → WAV
                           → MiniMax TTS (cloud) → MP3
```

## Key Design Decisions

**`shared/` is the single source of truth for defaults.** `shared/settings.py` holds all provider model IDs, default voices, WPM baselines, and Icecast/Ollama endpoints — all env-variable-backed. `shared/hosts.py` provides `primary_host_assignment()`, `secondary_host_assignment()`, `assignment_voice()`, and `assignment_wpm()`. Before this module existed, defaults were scattered across admin, streamer, and generator and drifted silently. Don't add defaults elsewhere.

**Canonical `hosts[]` format in schedule.yaml.** Every show uses a `hosts[]` array where each entry has `voice_kokoro`, `voice_minimax`, `voice_google`, and `tts_backend`. The legacy flat `host`/`voices` fields were removed in April 2026. Don't re-introduce flat fields.

**Kokoro runs in its own venv.** `station/kokoro/.venv` is isolated from `.venv` due to PyTorch/numpy/librosa dependency conflicts. The admin spawns Kokoro as a subprocess — it is not importable from the main venv.

**Editable install.** The project is installed via `pip install -e .`, placing `/code/writ-fm` on `sys.path` via a `.pth` file. This is what makes `from station.xxx import` work in subprocesses without `sys.path` manipulation.

**Segment lifecycle.** Each generated segment has a `.json` sidecar and a `.plays.json` sidecar. `content_lifecycle` per show controls `max_days` and `max_plays`. When `WRIT_CONSUME_SEGMENTS=1`, segments are deleted after playback. `output/` is never committed.

**Admin WorkingDirectory is `admin/`.** The systemd unit sets `WorkingDirectory=/code/writ-fm/admin`, so relative path resolution in `app.py` starts there.

**`station/time_utils.py` is the time authority.** Use `station_now()` and `station_iso_now()` everywhere; they read the timezone from `config/schedule.yaml`. Don't call `datetime.now()` directly.

## Config Files

| File | Controls |
|------|----------|
| `config/schedule.yaml` | Station name, timezone, shows (canonical `hosts[]`), base daily schedule |
| `config/hosts.yaml` | Host identities, per-backend voice IDs, WPM, bio |
| `config/segment_types.yaml` | Managed segment definitions: prompt templates, single/multi-voice, cadence |
| `config/show_taxonomy.yaml` | Bumper styles and source-type definitions per show |
| `config/ondemand.yaml` | Audiobookshelf base URL, library IDs, upload sources |

## Key Environment Variables

| Variable | Purpose |
|----------|---------|
| `OLLAMA_URL` / `OLLAMA_MODEL` | LLM for script generation |
| `GOOGLE_TTS_API_KEY` / `GOOGLE_TTS_MODEL` | Google Gemini TTS |
| `MINIMAX_API_KEY` | MiniMax TTS + music generation (legacy; kept for fallback) |
| `WRIT_MUSIC_BACKEND` | Music bumper provider: `lyria` (default) or `minimax` |
| `GOOGLE_VERTEX_PROJECT` | GCP project ID for Lyria (falls back to `GOOGLE_CLOUD_PROJECT`) |
| `GOOGLE_VERTEX_LOCATION` | Vertex AI region (default `us-central1`) |
| `GOOGLE_APPLICATION_CREDENTIALS` | Service-account JSON with `roles/aiplatform.user` |
| `GOOGLE_VERTEX_ACCESS_TOKEN` | Pre-minted OAuth token, instead of a service account |
| `ICECAST_PASS` | Icecast source password |
| `WRIT_CONSUME_SEGMENTS` | `1` = delete after play (prod), `0` = keep (dev) |
| `WRIT_ADMIN_PORT` | Admin UI port (default 8080) |
| `REDDIT_CLIENT_ID` | Reddit OAuth app client ID (enables NSFW subreddit access) |
| `REDDIT_CLIENT_SECRET` | Reddit OAuth app client secret |
| `REDDIT_USERNAME` | Reddit account username for OAuth |
| `REDDIT_PASSWORD` | Reddit account password for OAuth |

## Test Coverage

Regression tests cover the known production failure modes:
- `test_shared_hosts.py` — voice resolution from canonical `hosts[]` and legacy fallback paths
- `test_schedule_voice_defaults.py` — schedule normalization from legacy YAML
- `test_admin_voice_resolution.py` — admin voice resolution and canonical-only persistence
- `test_talk_generator_voice_logic.py` — voice/WPM/rendering per TTS backend
- `test_google_tts.py` — Google TTS retry and timeout behaviour

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
