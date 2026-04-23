# WRIT-FM Status & TODO

## Current Status (2026-04-12)

The station is live and the core system is working end to end:

- Icecast, streamer, admin UI, talk generation, bumper generation, and scheduler are in place
- Host management is centralized in `config/hosts.yaml` and exposed in the admin UI
- Segment types are now managed in `config/segment_types.yaml` and exposed in the admin UI
- Show editing uses roster-backed hosts and managed segment types
- Show editing now also saves per-show auto-generation thresholds and cadence
- Talk generation loads managed prompt templates and supports multi-voice generation for Kokoro
- Content lifecycle settings now persist correctly from the admin UI into `schedule.yaml`
- Manual generation now supports source-aware prompts for web URLs and Reddit threads/subreddits
- Manual generation also supports direct YouTube audio ingest via `yt-dlp`
- Reddit generation is split into `reddit_storytelling` and `reddit_post`, with `Topic` ignored for both
- YouTube source runs auto-route to the new `youtube` segment type
- Show types now split editorial shows from source-led shows; auto-generation should continue to move toward rule-based source selection per show
- Taxonomy editing now exists in the admin UI for topic focuses and bumper styles
- Library management now supports cross-show copy/move and play-count resets
- Library actions are now tucked behind a compact menu to keep the list readable
- Library view now has talk/music filters and per-show bulk delete for visible items
- Scheduler cards now include a per-show Generate Now button for exercising the auto-generation path
- Story subreddits like `/r/nosleep` automatically route to `reddit_storytelling`
- MiniMax long-form async is now opt-in and confirmation-gated in the admin UI
- Voice samples are cached on disk for Kokoro and MiniMax, with play/stop audition buttons in the admin UI
- Manual generation includes an `Include topic` toggle to disable topic selection entirely
- Logs, metadata, and scheduler timestamps now align to the station timezone

## Completed Recently

### Milestone 3

- Hosts tab with CRUD
- Global roster-backed host assignment in show editor
- Kokoro + MiniMax preview endpoints and UI controls
- Fixes for host assignment validation and voice-preview/backend mismatch

### Milestone 4

- Segment Types tab with CRUD
- Managed segment definitions in `config/segment_types.yaml`
- Dynamic segment type picker in show editor and generate form
- Prompt templates and word-count targets loaded from managed config
- Multi-voice generation controlled by segment type metadata
- Panel/interview generation now uses the show's configured host roster
- Station Settings tab for global station name and timezone management
- Source-aware manual generation in the admin UI
- `reddit_post` and `reddit_storytelling` Reddit segment types
- `youtube` source ingest for direct video audio capture plus metadata/captions
- Cached voice sample playback and split Hosts/Voices admin sub-tabs

## Outstanding Work

### Pipeline / TTS

- Fully wire MiniMax as a first-class talk-generation backend in all generation paths
- Add MiniMax emotion/sound markers to prompts
- Implement true MiniMax multi-voice rendering instead of primary-voice fallback

### Generation Quality

- Make topic pool contents editable from config/admin rather than hardcoded
- Wire `bumper_style` into music bumper prompt selection
- Review and tune default prompt templates now that they are editable
- Consider using show-level `research_sources` automatically in scheduled generation, not just manual runs

### Inventory / Production Readiness

- Seed missing music bumper inventory for all shows
- Turn on `WRIT_CONSUME_SEGMENTS=1` in production once satisfied with inventory replenishment
- Tune `content_lifecycle` and auto-generation thresholds per show

### Live Ops / Admin

- Build live queue / showrun view
- Skip current audio
- Reorder queue
- Inject specific library items into the queue

### Review Follow-ups

- Stop recording failed generation jobs as successful cadence runs so talk/music retries can happen on the next scheduler pass.
- Either wire the live-control `segment` action into playback or remove the dead `force_segment` flag.
- Fix listener stats so `/stats` reflects actual audience activity instead of poll frequency.

### Proxy / Delivery

- Keep the public user app pointed at the same origin it was loaded from when running behind Cloudflare Tunnel.

## Notes

- `SESSION_SUMMARY.md` is now historical migration context only.
- `MILESTONES.md` is the canonical milestone tracker.
