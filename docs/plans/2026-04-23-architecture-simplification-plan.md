# Architecture Simplification And Regression Test Plan

## Goal

Simplify WRIT-FM into a Linux-first, test-backed application without regressing the production issues we have already hit:

- wrong default host voice resolution, especially for Google TTS
- per-backend voice settings drifting out of sync across admin, schedule, and generators
- WPM/pacing differences not being honored consistently across providers
- stale legacy fields masking real bugs
- environment- or machine-specific defaults leaking into runtime behavior

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

Initial regression coverage should include:

- schedule normalization from legacy `host` / `voices` into canonical host assignments
- primary host voice resolution from roster defaults when show host blocks are incomplete
- Google TTS default voice resolution when UI says `Use show default`
- per-backend WPM lookup, especially `speaking_pace_wpm_google`
- Google render path passing WPM hints through to the provider client
- playback-sequence normalization/default merging

### Phase 1: Canonical Schema

Make `shows[].hosts[]` the canonical show/voice model.

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

Steps:

- introduce package-based imports
- remove ad hoc `sys.path` insertion
- move generators/providers/services into explicit packages
- update admin subprocess paths and service entrypoints
- preserve backwards-compatible entry scripts only as thin shims during rollout

Exit condition:

- `mac/` is gone or contains only temporary shims slated for deletion

### Phase 5: Legacy Feature Review And Pruning

Decide which old components remain supported.

Likely review targets:

- Chatterbox integration
- `mac/config.yaml` legacy config
- tmux helper scripts
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

1. Land the initial test suite covering voice fallback, WPM, and schedule normalization.
2. Freeze the canonical schema for show host assignments.
3. Remove compatibility writes from admin after migration coverage is in place.
4. Extract shared voice/settings/time helpers.
5. Start moving runtime code out of `mac/` behind package imports.
