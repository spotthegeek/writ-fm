# WRIT-FM

WRIT-FM is a 24/7 AI radio stack: a streamer, an admin UI, scheduled content generation, and a source-aware talk pipeline that can turn Reddit, web, and YouTube material into on-air segments.

The repo still has historical `station/` paths, but the live deployment is Linux-first and a shared refactor is underway.

## Current State

As of 2026-04-26, the repo includes:

- A gapless streamer in `station/stream_gapless.py`
- A FastAPI admin UI in `admin/app.py`
- Talk generation in `station/content_generator/talk_generator.py`
- Music bumper generation in `station/content_generator/music_bumper_generator.py`
- Shared host/settings helpers in `shared/`
- Regression tests covering voice resolution, schedule normalization, and Google TTS behavior

The default station config currently describes `Crouch-FM` in the `Australia/Adelaide` timezone with a source-led schedule built around Reddit- and YouTube-driven shows. See `config/schedule.yaml` and `SCHEDULE.md`.

## Architecture

```text
Icecast
  ^
  |
ffmpeg encoder <- station/stream_gapless.py
                   |- reads show schedule and content inventory
                   |- plays music + talk segments
                   `- updates now-playing state

admin/app.py
  |- edits schedule, hosts, segment types, and taxonomy
  |- starts the inventory scheduler
  |- exposes generation, library, and voice-sample tools
  `- persists job/activity history

station/content_generator/
  |- talk_generator.py
  |- music_bumper_generator.py
  `- listener_response_generator.py

shared/
  |- settings.py
  `- hosts.py
```

## Repo Layout

```text
admin/                  FastAPI admin app + scheduler
config/                 schedule, hosts, segment types, taxonomy
station/                    streamer, generators, provider clients, legacy entrypoints
shared/                 refactor-in-progress shared helpers
tests/                  regression coverage
output/                 generated assets, logs, source cache, jobs
```

## Setup

### 1. Install dependencies

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
cd /root/writ-fm
uv sync
cp .env.example .env
```

### 2. Configure environment

Edit `.env` and set the keys you actually use:

- `MINIMAX_API_KEY`
- `GEMINI_API_KEY` or `GOOGLE_TTS_API_KEY`
- `ICECAST_PASS`
- `OLLAMA_URL`
- `OLLAMA_MODEL`

Key defaults in `.env.example`:

```bash
WRIT_ADMIN_PORT=8080
WRIT_CONSUME_SEGMENTS=0
MINIMAX_MUSIC_MODEL=music-2.6
GOOGLE_TTS_MODEL=gemini-3.1-flash-tts-preview
```

### 3. Configure the station

Main config files:

- `config/schedule.yaml` for station name, timezone, shows, source rules, and daily schedule
- `config/hosts.yaml` for host identities and per-backend voices
- `config/segment_types.yaml` for prompt templates and single/multi-voice behavior
- `config/show_taxonomy.yaml` for bumper styles and source-type definitions

### 4. Start the services

Local development:

```bash
cd /root/writ-fm
uv run python admin/app.py
uv run python station/api_server.py
uv run python station/stream_gapless.py
```

Production on this machine has historically used systemd for the streamer and admin UI. If you are operating the live box, verify current unit names before changing anything.

## Current Schedule

The checked-in default base schedule is:

- `00:00-03:00` `alien_theory`
- `03:00-06:00` `nosleep`
- `06:00-09:00` `sysadmin`
- `09:00-12:00` `youtube-ai`
- `12:00-15:00` `alien_theory`
- `15:00-18:00` `nosleep`
- `18:00-21:00` `sysadmin`
- `21:00-00:00` `youtube-ai`

There are currently no weekly overrides in `config/schedule.yaml`.

## Generation Features

- Source-aware talk generation from web URLs, Reddit threads/subreddits, and YouTube videos/channels
- Managed segment types via `config/segment_types.yaml`
- Cached voice samples for Kokoro, MiniMax, and Google TTS
- Per-show talk/music inventory thresholds and cadence in the scheduler
- Station-timezone-aware timestamps across admin, scheduler, metadata, and now-playing

Current source-led/default shows include:

- `/r/nosleep`
- `/r/alien_theory`
- `/r/sysadmin`
- `YouTube AI`

Legacy shows like `signal_report`, `crosswire`, and `listener_hours` still exist in config but are not currently scheduled in the base rotation.

## TTS Backends

The repo currently contains support for:

- Kokoro
- MiniMax TTS
- Google Gemini TTS
- Chatterbox (legacy/optional)

Important nuance:

- Google TTS support is now wired and covered by tests.
- MiniMax exists across the codebase, but true multi-voice rendering still needs follow-up.

## Tests

Run the current regression suite with:

```bash
cd /root/writ-fm
./.venv/bin/pytest -q
```

At the time of this doc update, the suite passes with `25 passed` and only FastAPI `on_event` deprecation warnings.

## Docs Map

- `TODO.md` for current implementation status and open work
- `MILESTONES.md` for milestone tracking
- `SCHEDULE.md` for the checked-in program grid
- `SESSION_SUMMARY.md` for current-session summary and pointers
- `handover.md` for Claude Code onboarding context
- `docs/plans/2026-04-23-architecture-simplification-plan.md` for the refactor plan
- Copying or moving segments and bumpers between shows
- Resetting play counts for existing items
- Seeing the current expiry window for each item based on the owning show's lifecycle rules
- A compact per-item Actions menu so the list stays readable
- Filtering the library between all, talk, and music items, plus deleting only the currently visible items for a show

## Automated Operation

For hands-off operation, the operator script runs maintenance:

```bash
./run_operator.sh
```

Or via cron:
```
0 */2 * * * cd /path/to/writ-fm && ./run_operator.sh
```

Or as a persistent loop:
```bash
bash station/operator_daemon.sh
```

The operator loop checks stream health, decides which shows need content, generates new talk segments and AI bumpers when needed, and processes listener messages.

## Files

```
├── station/
│   ├── stream_gapless.py      # Main streamer
│   ├── schedule.py            # Weekly schedule parser
│   ├── now_playing_server.py  # API server
│   ├── play_history.py        # Track history/dedup
│   ├── discogs_lookup.py      # Album art + QR codes
│   ├── qr_generator.py        # Discogs QR for now-playing
│   ├── operator_prompt.md     # Automated maintenance prompt
│   ├── chatterbox/            # Chatterbox TTS wrapper
│   ├── kokoro/                # Kokoro TTS wrapper
│   ├── content_generator/
│   │   ├── talk_generator.py            # Talk segment generator (managed segment types + multi-voice)
│   │   ├── music_bumper_generator.py    # AI music bumper generator (MiniMax)
│   │   ├── listener_response_generator.py # Listener message → audio
│   │   ├── persona.py                   # Station identity
│   │   └── helpers.py                   # Shared utilities
│   └── voice_reference/       # Voice samples for cloning
├── config/
│   ├── hosts.yaml             # Global hosts / speakers roster
│   ├── schedule.yaml          # Weekly show schedule
│   ├── segment_types.yaml     # Managed segment definitions
│   └── icecast.xml.example    # Icecast template
├── output/
│   ├── segments/              # Generated DJ audio (by period)
│   └── scripts/               # Script metadata
└── run_operator.sh            # Operator launcher
```

## Requirements

- Python 3.11+
- ffmpeg
- Icecast2
- Claude CLI or Gemini CLI (for script generation)
- ~200MB for Kokoro TTS, ~4GB for Chatterbox
- Apple Silicon recommended for Chatterbox (uses MPS acceleration)

## License

MIT
