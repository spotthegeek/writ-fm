# WRIT-FM Status & TODO

## Current Status (2026-04-26, updated)

Recent session progress:

- Admin library tab: grouped view, show filter, and "Other" tab for structural segments
- Admin generation tab: structured activity log, restored per-show auto-gen toggle, trigger button loading states, streaming log panel for triggered jobs
- Scheduler: failure backoff (30 min), HF_TOKEN wired through, int() cast fix for YAML inventory values, job streaming via Popen
- Listener app: segment progress bar + elapsed/total time
- Project structure: `listener-app/` folder created, favicon added, systemd descriptions updated

## Current Status (original, 2026-04-26)

The station stack is functional end to end and the codebase is in the middle of a cleanup/refactor rather than an initial build phase.

Working now:

- Icecast, streamer, admin UI, talk generation, music bumper generation, and the scheduler exist in one repo
- Hosts, segment types, station settings, and source taxonomy are config-backed and editable through the admin UI
- Source-aware manual generation supports web URLs, Reddit threads/subreddits, and YouTube ingest
- Voice samples are cached and playable for Kokoro, MiniMax, and Google TTS
- Station timezone handling is wired through admin, scheduler, metadata, and now-playing timestamps
- Shared helpers now exist in `shared/settings.py` and `shared/hosts.py`
- A regression suite covers voice fallback, Google TTS defaults, schedule normalization, and talk-generator voice logic

Recent code-level progress since the older doc set:

- Google TTS support landed
- Show config and source-driven generation were simplified in `4ed1aac`
- Initial shared-module extraction and tests landed

## Immediate Priorities

### High Priority

- Finish migrating show config away from legacy flattened fields like `host`, `tts_backend`, and `voices`
- Stop scheduler cadence from treating failed generation jobs as successful runs
- Decide whether live-control `segment` forcing is real or dead code, then wire or remove it
- Fix listener stats so reported audience metrics reflect listeners rather than poll frequency

### Pipeline / TTS

- Verify MiniMax behavior across every talk-generation path and document the remaining gaps clearly
- Add MiniMax emotion/sound markers where they materially improve delivery
- Implement true MiniMax multi-voice rendering instead of primary-voice fallback
- Chatterbox: removed (zero usage, experimental only — deleted mac/chatterbox/)

### Generation Quality

- Make topic-pool contents editable from config/admin rather than hardcoded
- Wire `bumper_style` into music bumper prompt selection
- Review prompt-template defaults now that segment types are fully managed
- Push more scheduled generation toward show-level source rules instead of manual-only source selection

### Inventory / Production Readiness

- Seed or verify music bumper inventory for all scheduled shows
- Turn on `WRIT_CONSUME_SEGMENTS=1` in production once replenishment is trusted
- Tune `content_lifecycle` and per-show inventory thresholds against real on-air behavior

### Live Ops / Admin

- Build a live queue/showrun view
- Skip current audio
- Reorder queued items
- Inject specific library items into the queue

### Cleanup / Refactor

- Replace FastAPI `on_event("startup")` with a lifespan handler
- Continue moving shared config/voice/time logic out of `mac/`
- Reduce repo-level reliance on `sys.path` insertion and script-style imports
- Update packaging so the wheel layout matches the Linux-first runtime, not just `mac`

## Known Config Drift To Resolve

- `config/schedule.yaml` still contains a mix of canonical `hosts[]` assignments and legacy `host` / `voices` fields
- Some legacy editorial shows remain defined but are not in the base schedule
- Several docs previously described the old 15-show grid and ACE-Step-first workflows; those were updated in this pass, but older notes may still exist elsewhere

## Verification

Current regression result from this handoff pass:

- `./.venv/bin/pytest -q` -> `25 passed`
- Warning only: FastAPI `on_event` deprecation
