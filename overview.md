# WRIT-FM Codebase Overview

WRIT-FM is a 24/7 AI internet radio station. The stack generates talk segments and music bumpers autonomously, streams them via Icecast, and exposes a web admin UI and a public listener app.

---

## Folder Structure

```
admin/          Web admin UI (FastAPI + single-page React app)
config/         All YAML configuration (schedule, hosts, segment types)
docs/           Design and refactor plans
listener-app/   Public listener web app (static HTML, served separately)
output/         Runtime-generated files (audio, scripts, cache, job logs)
shared/         Cross-cutting Python helpers (settings, host resolution)
station/        Station runtime: streamer, generators, TTS providers
tests/          Regression test suite
```

---

## `station/` — The Station Runtime

Everything the station needs to run. Named `mac/` historically (originally built on macOS); renamed to `station/` in April 2026.

### Core services

| File | Purpose |
|------|---------|
| `stream_gapless.py` | The gapless audio streamer. Reads the schedule, picks segments from the on-disk inventory, and pipes them to Icecast via ffmpeg. Runs as `writ-fm.service`. |
| `api_server.py` | Lightweight HTTP API (port 8001). Exposes `/now-playing`, `/schedule`, `/message`, and on-demand endpoints. Also serves Discogs metadata and QR codes. |
| `schedule.py` | Schedule loader and validator. Reads `config/schedule.yaml`, resolves the current show by time-of-day, and merges playback sequence defaults. |
| `ondemand.py` | On-demand content module. SQLite state store for resume positions and listen status. Audiobookshelf API client plus local upload/inbox inventory. |
| `time_utils.py` | Station-timezone-aware datetime helpers (`station_now()`, `station_iso_now()`). Reads timezone from `config/schedule.yaml`. Used everywhere. |
| `play_history.py` | SQLite-backed play history tracker. Used by the streamer to avoid repeats within a window. |

### TTS providers

| File | Purpose |
|------|---------|
| `kokoro/tts.py` | Kokoro TTS wrapper. Renders speech to WAV using ONNX inference. Runs in its own isolated venv (`kokoro/.venv`) due to dependency conflicts with the main stack. |
| `kokoro/onnx_render.py` | Low-level ONNX inference for Kokoro voice synthesis. |
| `google_tts.py` | Google Gemini TTS provider. Streams audio via the Gemini API with configurable voice, WPM, retries, and timeout. |
| `minimax_tts.py` | MiniMax TTS provider. Cloud API, supports multiple voices. |
| `music_gen_client.py` | MiniMax music-2.6 client for generating music bumpers (instrumental ~130s MP3s). |

**⚠️ Kokoro has a separate venv** at `station/kokoro/.venv` because its dependencies (PyTorch, specific numpy/librosa versions) conflict with the main project venv. The admin's "Generate Sample" button spawns it as a subprocess.

### Content generators (`content_generator/`)

| File | Purpose |
|------|---------|
| `talk_generator.py` | Core talk segment generator. Takes a show + segment type, resolves the host and voice, builds a prompt via `persona.py`, calls the LLM (Ollama), renders via TTS, and writes the WAV + JSON sidecar to `output/talk_segments/{show_id}/`. |
| `music_bumper_generator.py` | Generates instrumental music bumpers via MiniMax. Writes MP3 + JSON sidecar to `output/music_bumpers/{show_id}/`. |
| `listener_response_generator.py` | Watches `~/.writ/messages.json` for listener messages and generates short personalised on-air response segments. |
| `persona.py` | Host identity definitions. Returns character prompts, voice styles, and philosophies for each host ID. Falls back to `config/hosts.yaml` for production host data. |
| `helpers.py` | Shared utilities for content generators: RSS headline fetching, text preprocessing for TTS, Claude CLI subprocess wrapper. |
| `music_pools_expanded.py` | Static pool of music generation captions (instrumental descriptions) per show, used as the prompt source for music bumpers. |

### Other files

| File | Purpose |
|------|---------|
| `voice_samples.py` | Voice catalog and sample generation. Used by the admin UI to list available voices and generate/cache short sample audio clips. |
| `discogs_lookup.py` | Discogs API client for track metadata lookup. Requires `DISCOGS_TOKEN` env var. |
| `qr_generator.py` | Generates PNG QR codes for Discogs release URLs. Used by `api_server.py`. |
| `generate_voice_samples.py` | One-off maintenance script to pre-generate voice sample files for the admin UI. |
| `listener_daemon.sh` | Shell loop that polls every 30s and invokes `listener_response_generator.py`. |
| `operator_daemon.sh` | Shell loop that invokes `run_operator.sh` every 15 minutes for autonomous maintenance. |
| `start_music_gen.sh` | tmux helper to launch streamer, operator daemon, and listener daemon in named panes. |
| `operator_prompt.md` | System prompt for the autonomous operator agent. Describes what to check and how to act. |

---

## `admin/` — The Web Admin UI

| File | Purpose |
|------|---------|
| `app.py` | FastAPI backend (port 8080). All admin API routes: show/host/schedule editing, inventory browser, generation triggering, voice samples, live control, on-demand config. Also starts the background scheduler. Runs as `writ-fm-admin.service`. |
| `scheduler.py` | Background scheduler. Polls each show's generation config on a configurable interval, triggers talk and music generation jobs when inventory is below threshold, tracks job state and logs. |
| `index.html` | Single-file React 18 app (Babel CDN, no build step). All admin UI: shows, schedule, library, generation, live control, on-demand, voices. |

---

## `shared/` — Cross-Cutting Helpers

| File | Purpose |
|------|---------|
| `settings.py` | Centralised defaults: provider model IDs, default voices per backend and role, WPM baselines, Icecast URL, Ollama endpoint. All environment-variable-backed. Single source of truth — no scattered defaults. |
| `hosts.py` | Host and voice resolution logic. `primary_host_assignment()`, `secondary_host_assignment()`, `assignment_voice()`, `assignment_wpm()`. Reads canonical `hosts[]` format from the schedule and falls back to the hosts roster. |

**Why `shared/` matters:** before this module existed, voice/WPM defaults were duplicated across the admin, talk generator, and streamer — causing silent drift between what the UI showed and what actually got rendered.

---

## `config/` — YAML Configuration

| File | Purpose |
|------|---------|
| `schedule.yaml` | Station name, timezone, all shows (canonical `hosts[]` format with per-backend voices), and the base time-block schedule. Central source of truth for the station. |
| `hosts.yaml` | Host roster: identity, voice style, philosophy, anti-patterns, and per-backend voice IDs and WPM. Editable from the admin UI. |
| `segment_types.yaml` | Managed segment type definitions: prompt templates, single vs multi-voice, short vs long form. Used by the talk generator and admin UI. |
| `ondemand.yaml` | On-demand config: Audiobookshelf base URL and library IDs, upload source definitions. |

**Show schema:** every show uses canonical `hosts[]` arrays with `voice_kokoro`, `voice_minimax`, `voice_google`, and `tts_backend` per host. Legacy flat `host`/`voices` fields were removed in April 2026.

---

## `listener-app/` — Public Listener Web App

Static single-file HTML app served separately (not via the admin). Two modes:

- **Live** — connects to `api_server.py` for now-playing metadata, plays the Icecast stream, shows progress bar and segment timer
- **On-demand** — browses Audiobookshelf libraries and local uploads, supports resume position, mark-as-listened, scrub bar

---

## `tests/` — Regression Suite

Covers the known production failure modes:

- Voice resolution from canonical `hosts[]` and legacy fallback paths (`test_shared_hosts.py`)
- Schedule normalization from legacy YAML (`test_schedule_voice_defaults.py`)
- Admin voice resolution and canonical-only persistence (`test_admin_voice_resolution.py`)
- Talk generator voice/WPM/rendering per TTS backend (`test_talk_generator_voice_logic.py`)
- Google TTS retry and timeout behaviour (`test_google_tts.py`)

Run with `.venv/bin/pytest -q`. All 25 tests pass.

---

## Runtime Architecture

```
Icecast2 (port 8000)
    ^
    |  ffmpeg pipe
    |
station/stream_gapless.py          ← reads schedule + picks segments → pipes audio
    |
    ├── output/talk_segments/{show}/   ← WAV + JSON sidecars (generated async)
    └── output/music_bumpers/{show}/   ← MP3 + JSON sidecars (generated async)

admin/app.py (port 8080)           ← admin UI + background scheduler
    └── admin/scheduler.py             ← triggers generation jobs below threshold

station/api_server.py (port 8001)  ← now-playing, messages, on-demand API

Content generation (subprocesses):
    station/content_generator/talk_generator.py
    station/content_generator/music_bumper_generator.py

TTS pipeline:
    LLM (Ollama/gemma) → script text
        → Kokoro (local ONNX)    → WAV
        → Google Gemini TTS      → WAV
        → MiniMax TTS (cloud)    → MP3
```

---

## Key Environment Variables

| Variable | Purpose |
|----------|---------|
| `OLLAMA_URL` / `OLLAMA_MODEL` | LLM endpoint and model for script generation |
| `GOOGLE_TTS_API_KEY` / `GOOGLE_TTS_MODEL` | Google Gemini TTS |
| `MINIMAX_API_KEY` | MiniMax TTS + music generation |
| `ICECAST_PASS` | Icecast source password |
| `WRIT_CONSUME_SEGMENTS` | `1` = delete segments after play (production), `0` = keep (development) |
| `WRIT_ADMIN_PORT` | Admin UI port (default 8080) |

---

## Things Worth Knowing

- **Kokoro runs in its own venv.** `station/kokoro/.venv` is a Python 3.14 environment isolated from the main `.venv`. The admin spawns it as a subprocess for sample generation and rendering.
- **Editable install.** The project is installed via `pip install -e .`, which puts `/code/writ-fm` on `sys.path` for the venv via a `.pth` file. This is what makes `from station.xxx import` work in subprocesses without any `sys.path` manipulation.
- **Audio lifecycle.** Each generated segment has a `.json` sidecar and a `.plays.json` sidecar. The streamer tracks play count and age. `content_lifecycle` per show controls `max_days` and `max_plays`. When `WRIT_CONSUME_SEGMENTS=1`, played segments are deleted.
- **The `output/` directory is not committed.** It's the runtime working area: `talk_segments/`, `music_bumpers/`, `scripts/`, `source_cache/`, `ondemand/`, `jobs/`.
- **Admin WorkingDirectory is `admin/`.** The systemd unit for `writ-fm-admin.service` sets `WorkingDirectory=/code/writ-fm/admin`, so relative imports in `app.py` resolve from there. The editable install ensures `station/` is still importable.
