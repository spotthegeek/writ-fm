# Architecture Simplification And Regression Test Plan

## Goal

Simplify WRIT-FM into a Linux-first, test-backed application without regressing the production issues we have already hit:

- wrong default host voice resolution, especially for Google TTS
- per-backend voice settings drifting out of sync across admin, schedule, and generators
- WPM/pacing differences not being honored consistently across providers
- stale legacy fields masking real bugs
- environment- or machine-specific defaults leaking into runtime behavior

## Status Update (2026-04-26)

This plan is no longer purely aspirational. Some of the early work has already landed:

- `shared/settings.py` centralizes several provider defaults and environment-backed settings
- `shared/hosts.py` centralizes primary-host, secondary-host, voice, and WPM resolution
- Tests now cover schedule normalization, Google TTS voice defaults, admin voice resolution, shared host helpers, and talk-generator voice logic

What remains true:

- The schedule schema is still mixed between canonical `hosts[]` data and legacy flattened fields
- The repo still relies heavily on `mac/` paths and script-style imports
- Packaging and runtime entrypoints have not fully caught up to the Linux-first direction

## Principles

- Keep the station working at every phase.
- Add tests before or alongside structural changes.
- Prefer migration over indefinite compatibility layers.
- Centralize defaults once, then remove duplicated fallback logic.
- Treat the current Linux + systemd deployment as the supported runtime model.

## Target Structure

This is the intended end state, not a big-bang move:

```text
docs/
  plans/
services/
  streamer/
  live_api/
generators/
  talk/
  music/
  listener/
providers/
  tts/
  music/
shared/
  config/
  schedule/
  time/
  voices/
ops/
tests/
```

`mac/` should be retired once imports, scripts, and service entrypoints stop depending on it.

## Delivery Phases

### Phase 0: Test Harness And Safety Net

Add repo-level tests and cover the bugs we have already seen in production.

Status: Started, with an initial suite already green.

Initial regression coverage should include:

- schedule normalization from legacy `host` / `voices` into canonical host assignments
- primary host voice resolution from roster defaults when show host blocks are incomplete
- Google TTS default voice resolution when UI says `Use show default`
- per-backend WPM lookup, especially `speaking_pace_wpm_google`
- Google render path passing WPM hints through to the provider client
- playback-sequence normalization/default merging

### Phase 1: Canonical Schema

Make `shows[].hosts[]` the canonical show/voice model.

Status: In progress but incomplete.

Steps:

- define one canonical show schema
- add migration helpers for old YAML
- stop writing legacy `host`, `tts_backend`, and `voices` fields from admin
- update readers to consume only canonical host assignments
- keep a one-time migration path for older configs if needed

Exit condition:

- no live logic depends on flattened host/voice fields

### Phase 2: Centralized Settings

Replace scattered hard-coded defaults with a shared settings layer.

Status: Started.

Candidates to centralize:

- provider model IDs
- default voice IDs per backend and role
- default WPM baselines
- Icecast status URLs and service URLs
- OLLAMA endpoint defaults
- message cooldowns and cache TTLs
- fallback asset paths

Exit condition:

- backend defaults live in one settings module, not repeated across admin, generators, and runtime

### Phase 3: Shared Domain Modules

Move duplicated logic into shared modules.

Status: Started.

High-value extractions:

- timezone/station clock helpers
- schedule loading and validation
- host roster lookups
- voice resolution and pace lookup
- backend label/default helpers

Exit condition:

- admin, streamer, and generators consume the same shared logic instead of local copies

### Phase 4: Package And Runtime Restructure

Rename and reorganize the old `mac/` layout into Linux-neutral packages.

Status: ✅ Complete (2026-04-27).

All `sys.path.insert` hacks removed from mac/ and content_generator/. All intra-mac imports now use
package-qualified paths (`from mac.xxx import`, `from mac.content_generator.xxx import`). The editable
install (`_editable_impl_writ_radio.pth`) puts `/code/writ-fm` on sys.path for all venv-run scripts,
making the hacks unnecessary. The `mac/` directory stays for now as the runtime location — renaming
to Linux-neutral package names is deferred until it adds clear value.

### Phase 5: Legacy Feature Review And Pruning

Decide which old components remain supported.

Status: ✅ Complete (2026-04-27).

- Chatterbox integration: **removed** (`mac/chatterbox/` deleted — zero usage, experimental only)
- `mac/config.yaml` legacy config: already absent — nothing to do
- tmux helper scripts: kept — `listener_daemon.sh`, `operator_daemon.sh`, `start_music_gen.sh` are still functional
- ACE-Step references: **updated** — README, music_gen_client.py, music_bumper_generator.py docstrings corrected to MiniMax
- stale docs for obsolete segment types and old startup flows
- unused QR/cache helpers and outdated provider compatibility code

Exit condition:

- supported features are documented and the rest are explicitly removed or marked experimental

## Regression Test Matrix

These scenarios should remain green throughout the cleanup:

### Voice Resolution

- show host assignment omits `voice_google`, roster has it, generated Google audio uses roster voice
- `Use show default` in admin resolves to the show's primary host voice, not a backend hard-coded fallback
- host-level defaults for Kokoro, MiniMax, and Google are all independently respected
- co-host/guest voice fallbacks do not overwrite primary host defaults

### Pacing

- `speaking_pace_wpm` remains a general fallback
- `speaking_pace_wpm_google` overrides general WPM for Google only
- `speaking_pace_wpm_minimax` overrides general WPM for MiniMax only
- provider calls receive the expected speed/WPM values

### Schedule And Config

- legacy schedule YAML still loads during migration
- canonical schedule YAML round-trips without reintroducing flattened legacy fields
- playback sequence defaults merge correctly by show type
- station timezone is applied consistently in admin and schedulers

### Runtime Integration

- admin manual generation uses selected backend/voice/WPM
- scheduler-triggered generation uses the same voice resolution rules as manual generation
- listener-response generation inherits current show/host context safely
- voice sample catalog uses the same provider voice registry as generation paths

### Documentation / Ops

- README matches real Linux deployment
- service/unit commands reference current entrypoints
- no docs or scripts imply Mac-only setup unless explicitly optional

## Acceptance Criteria

We can call the refactor successful when:

- tests cover the known bugs that previously escaped into production
- show/host/voice logic has one canonical representation
- provider defaults are centralized
- the project layout reflects the real Linux deployment model
- docs and ops scripts describe the current app, not the historical one

## Immediate Next Steps

1. Freeze the canonical schema for show host assignments and remove compatibility writes from admin once coverage is sufficient.
2. Continue migrating voice/default/time logic into `shared/`.
3. Expand tests around scheduler-triggered generation and failure handling.
4. Replace script-style imports and `sys.path` hacks with package-based imports.
5. Move runtime entrypoints toward Linux-neutral package paths while keeping thin compatibility shims only where necessary.
