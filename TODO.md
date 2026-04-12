# WRIT-FM Project To-Do List

## Current Status (as of 2026-04-12)
The core pipeline is working end-to-end. Icecast2 is running, the streamer
broadcasts 24/7, talk segments generate via Ollama + Kokoro TTS, music bumpers
generate via MiniMax music-2.6, and the admin UI is fully operational at
port 8080.

---

## Admin UI — Pending Features

### Host Management
- [ ] Dedicated "Hosts / Speakers" section in admin UI (currently hosts only editable
      inside each show's modal)
- [ ] Global host roster: name, bio, personality description, default TTS backend,
      Kokoro voice, MiniMax voice
- [ ] Ability to create/edit/delete host personas independently of shows
- [ ] Assign hosts to shows from the global roster

### TTS Voice Preview
- [ ] Preview button next to voice selectors in host/speaker management UI
      — plays a short sample sentence in the selected voice via the TTS API
- [ ] Same preview in the Manual Generate UI so you can audition configured
      hosts/speakers before committing to a generation run
- [ ] Both Kokoro and MiniMax voices should be previewable

### Segment Type Management
- [ ] Dedicated segment type manager in admin UI (currently a fixed checkbox list
      in the Show modal)
- [ ] Ability to define custom segment types beyond the built-in set
- [ ] Per-type: name, target word count range, prompt template, whether it supports
      multi-voice (panel/interview) or single-voice
- [ ] Show modal segment type picker updates dynamically from this managed list

### Live Show Control
- [ ] Live showrun view: see what is currently playing and the forward queue
      (next N segments + bumpers scheduled to play)
- [ ] Ability to reorder upcoming audio in the queue
- [ ] Skip current playing audio (sends signal to streamer to advance immediately)
- [ ] Ability to inject a specific file from the library into the front of the queue
- [ ] Requires a richer IPC mechanism between admin (port 8080) and streamer (port 8001)

### Topic Focus & Bumper Style — Clarification
**Topic Focus** — *actively used, worth exposing in the UI.*
It selects which topic pool (`TOPIC_POOLS` in `talk_generator.py`) is used to pick
random segment topics when no explicit topic is given. Current pools: `philosophy`,
`music_history`, `current_events`, `culture`, `soul_music`, `night_philosophy`,
`listeners`. Should be editable in the Show modal (already has a dropdown — but the
pool contents themselves are hardcoded and should be editable too).
- [ ] Make topic pool contents editable per-show in admin (add/remove/edit topic strings)

**Bumper Style** — *currently cosmetic, not wired up.*
The field exists in `schedule.yaml` and is loaded into `ProgramContext`, but
`music_bumper_generator.py` ignores it — per-show hardcoded caption pools determine
music style instead. To make it meaningful:
- [ ] Wire `bumper_style` into `music_bumper_generator.py` to filter/weight the
      caption pool (e.g. only pick captions tagged as matching the style)
- [ ] OR replace per-show hardcoded pools with style-tagged caption library that
      `bumper_style` selects from
- [ ] Update Show modal Bumper Style dropdown to reflect actual effect

---

## Generation Pipeline — Pending

### MiniMax TTS Integration
- [ ] Wire `WRIT_TTS_BACKEND=minimax` routing in `talk_generator.py` — env var
      is passed from admin but the generator currently always uses Kokoro
- [ ] Add MiniMax emotion/sound markers to prompts: `[laugh]`, `[sigh]`, `[cough]`,
      `[clears throat]` for more natural output (MiniMax speech-2.8-hd supports these)

### Music Bumpers
- [ ] Generate initial bumper inventory for 7 shows (only `dawn_chorus` has bumpers)
- [ ] Enable auto-scheduler for all shows once initial inventory is seeded
- [ ] Consider wiring `bumper_style` into caption pool selection (see above)

### Content Lifecycle
- [ ] Set `WRIT_CONSUME_SEGMENTS=1` in `writ-fm.service` when done testing
      (currently 0 — files are kept after play)
- [ ] Configure `content_lifecycle` blocks in `schedule.yaml` per show once
      happy with inventory replenishment rates

---

## Infrastructure — Pending

### Git
- [ ] Push latest commit (`6fb40af`) to remote

### MiniMax API Costs
- [ ] Monitor usage — music generation (~130s per track) and TTS both cost credits

### Local Music Generation
- [ ] ACE-Step (`music-gen.server`) not set up — all music is MiniMax cloud
- [ ] If local generation is desired: clone `music-gen.server`, download checkpoints,
      start service, restore localhost:4009 path in `music_gen_client.py`

---

## Completed ✓

### Infrastructure
- System dependencies (icecast2, ffmpeg, espeak-ng, uv)
- Project cloned, Python 3.11 venv, Kokoro TTS with CUDA
- Icecast2 running, source password `hackme`
- Both systemd services registered and running

### Pipeline
- End-to-end: LLM → script → Kokoro TTS → streamer → Icecast → stream
- MiniMax TTS client (`mac/minimax_tts.py`), correct base URL + model `speech-2.8-hd`
- MiniMax music client fixed (was using wrong domain `minimaxi.chat` → `minimax.io`)
- Music bumpers playing between talk segments
- `WRIT_CONSUME_SEGMENTS` flag for testing vs production mode
- Content lifecycle system: sidecar `.plays.json` tracking, max_plays/max_days enforcement

### Admin UI (port 8080)
- FastAPI backend with full REST API
- Dashboard, Shows, Schedule, Generate, Library tabs
- Show editor: hosts, TTS, voices, lifecycle, research sources, segment types
- Manual generation: Talk Segment + Music Bumper, streaming log, timer, form lock
- Topic "✦ Expand" button (LLM expands hint → full prompt)
- Guest/Call-in behind checkbox toggle
- Auto-generate scheduler: inventory floors, cadence, trigger-now per show
- Activity log: persisted across restarts, shows manual + scheduler + history
- Library: inline preview, play counts, generation prompt expand, word count, refresh
- Content lifecycle config in show modal (max_plays, max_days per content type)

### Generation Fixes
- Ollama `num_predict: 8192` — prevents truncated scripts
- Stronger length instruction in prompts + 3 retries for long-form segments
- `talk_generator.py` exits 1 on total failure (was always exiting 0)
- `music_bumper_generator.py` `is_server_available()` no longer times out
- `.json` sidecar written next to each audio file for library metadata
- Library falls back to `output/scripts/` for pre-sidecar files
- Custom `--caption` / `--vocal` CLI flags on music bumper generator
