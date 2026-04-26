# WRIT-FM Milestones

## Current Snapshot (2026-04-26, updated)

- Milestone 3 is complete.
- Milestone 4 is complete with follow-up polish still available.
- Milestone 1 is in progress — scheduler reliability improved (failure backoff, HF_TOKEN, streaming jobs).
- Milestone 2 has started indirectly through config management, but key quality work remains open.
- Milestone 5 has not started.
- The architecture simplification effort is now active, not just planned.
- Admin UI and listener app have received significant UX polish (structured logs, progress bars, grouped library, favicon, project structure cleanup).

## Milestone 1 — Pipeline Completeness

Status: In progress.

Delivered so far:

- End-to-end station stack is running from one repo
- Scheduler-driven inventory generation exists
- Google TTS support landed
- Shared voice/default helpers and regression tests now cover several production bugs

Open work:

- Finish MiniMax routing and validation across all talk-generation paths
- Add MiniMax emotion/sound markers where useful
- Seed or verify bumper inventory for all scheduled shows
- Flip `WRIT_CONSUME_SEGMENTS=1` in production once lifecycle replenishment is trusted
- Clean up scheduler failure handling so retries happen correctly

## Milestone 2 — Generation Quality

Status: In progress.

Delivered so far:

- Segment types are managed in config/admin
- Source-aware generation exists for Reddit, YouTube, and web inputs
- Station taxonomy exists in `config/show_taxonomy.yaml`

Open work:

- Wire `bumper_style` into music bumper prompt selection
- Make topic-pool contents editable in config/admin instead of hardcoded
- Tune default prompt templates against actual on-air output
- Expand source-rule-driven scheduled generation behavior

## Milestone 3 — Hosts & Voices Management

Status: Complete.

Delivered:

- Global host roster in `config/hosts.yaml`
- Dedicated Hosts UI with CRUD
- Cached voice samples with audition controls
- Per-backend voice defaults handled in shared helpers
- Roster-backed voice resolution covered by tests
- Google, Kokoro, and MiniMax voice-selection paths represented in admin/runtime

## Milestone 4 — Segment Type And Source Management

Status: Complete, with follow-up caveats.

Delivered:

- Managed segment type registry in `config/segment_types.yaml`
- Segment Types UI with CRUD
- Prompt templates and word counts loaded from managed config
- Multi-voice behavior driven by segment metadata
- Source-aware manual generation for web, Reddit, and YouTube
- `reddit_storytelling`, `reddit_post`, and `youtube` segment types
- Station Settings for station name and timezone
- Scheduler-side generate-now actions

Caveats:

- MiniMax multi-voice still falls back to a primary-voice path
- Legacy flattened show fields still exist alongside canonical `hosts[]` assignments

## Milestone 5 — Live Show Control

Status: Not started.

Open work:

- Live queue view
- Skip current audio
- Reorder upcoming queue
- Inject specific library items into the queue
- Richer admin-to-streamer IPC

## Parallel Track — Architecture Simplification

Status: In progress.

Delivered:

- Initial shared modules in `shared/`
- Test coverage for several previously regressed behaviors
- Updated plan in `docs/plans/2026-04-23-architecture-simplification-plan.md`

Next:

- Finish canonical schedule/host schema migration
- Centralize more defaults and runtime settings
- Reduce `mac/`-specific imports and script entrypoints
