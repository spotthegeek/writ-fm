# WRIT-FM Milestones

## Current Snapshot (2026-04-12)

- Milestone 3 is complete in the admin/runtime path.
- Milestone 4 is complete for managed segment definitions and Kokoro-backed multi-voice generation.
- Milestones 1, 2, and 5 remain open.

## Milestone 1 — Pipeline Completeness

Status: In progress, intentionally deferred.

Open work:
- Push git remote / latest local milestone work
- Fully wire `WRIT_TTS_BACKEND=minimax` routing throughout talk generation
- Add MiniMax emotion/sound markers (`[laugh]`, `[sigh]`, etc.) to prompts
- Seed bumper inventory for shows that still have none
- Flip `WRIT_CONSUME_SEGMENTS=1` in production and tune per-show lifecycle rules

## Milestone 2 — Generation Quality

Status: Not started.

Open work:
- Wire `bumper_style` from `schedule.yaml` into music bumper caption selection
- Make topic pool contents editable in config/admin instead of hardcoded in `talk_generator.py`

## Milestone 3 — Hosts & Voices Management

Status: Complete.

Delivered:
- Global host roster in `config/hosts.yaml`
- Dedicated Hosts tab in admin UI with CRUD
- Cached voice samples with play/stop audition buttons, plus split `Hosts & Speakers` / `Voices` admin sub-tabs
- Show host assignment from roster instead of ad hoc inline-only data
- Kokoro and MiniMax voice preview in admin
- Schedule/runtime normalization so roster-backed host assignments flow into generation
- Fixes for lifecycle persistence and invalid custom-host saves

## Milestone 4 — Segment Type Management

Status: Complete, with one MiniMax caveat.

Delivered:
- Managed segment type registry in `config/segment_types.yaml`
- Dedicated Segment Types admin tab with CRUD
- Show modal segment type picker driven from the managed registry
- Generator prompt templates and word counts loaded from managed segment definitions
- Multi-voice flag controls generation behavior instead of hardcoded `panel`/`interview` checks
- Multi-host prompt wiring for panel/interview generation
- Station Settings now manage global station name and timezone
- Source-aware manual generation for web URLs and Reddit threads/subreddits
- `reddit_storytelling` and `reddit_post` Reddit segment types with story-aware routing for narrative subreddits
- `youtube` source ingest with `yt-dlp` direct audio capture and stored metadata/captions
- Manual generation `Include topic` toggle and `--no-topic` CLI support for intro/outro/station IDs
- Station-time alignment for admin logs, scheduler, generator metadata, and now-playing timestamps

Caveat:
- MiniMax multi-voice rendering still falls back to the primary voice. Kokoro multi-voice is fully wired.

## Milestone 5 — Live Show Control

Status: Not started.

Open work:
- Live queue view: current + next N items
- Skip current audio
- Reorder upcoming queue
- Inject a specific file to the front of the queue
- Richer IPC between admin and streamer

## Out of Scope For Now

- ACE-Step local music generation unless cloud costs justify the extra setup
- Automated MiniMax cost monitoring
