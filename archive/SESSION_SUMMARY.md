# WRIT-FM Session Summary

## Session 2026-04-26 (Claude Code)

### What Changed

**Admin UI — Library tab**
- Added grouped view toggle (segments grouped by show, with show headers)
- Added show filter dropdown
- Added "Other" tab capturing structural segment types (show_intro, show_outro, station_id) separate from Talk and Music

**Admin UI — Generation tab**
- Fixed silent polling bug: job status check compared against `"done"` but backend sets `"completed"`
- Replaced unstructured text activity log with a structured, filterable log (colour-coded rows, source badges, expandable job detail panel with full subprocess output)
- Restored per-show auto-generate enabled/disabled toggle (with visual dot indicator) for both talk and music
- Trigger buttons now show loading state and disable during generation; status bar updates as for manual jobs
- Active streaming generation log panel shown while a triggered job runs (same as manual generation experience)

**Scheduler / backend**
- Added `HF_TOKEN` to `.env.example` and wired it through to generation subprocess env
- Added 30-minute failure backoff per show/type: scheduler skips a show after a failed generation run and logs the backoff state
- Fixed scheduler crash: YAML inventory values returned as strings; added `int()` casts for `target_inventory` and `min_inventory`
- Switched scheduler-triggered generation from `subprocess.run` to `subprocess.Popen` for streaming output
- Pre-register scheduler jobs in `_jobs` dict before thread start so `job_id` can be returned immediately from trigger endpoints
- Admin status and trigger API endpoints now expose `last_failure`, `in_backoff`, and `job_id` fields

**Admin console — progress bar**
- Added 2px segment progress bar + elapsed/total time to the sidebar "On Air" card
- Added inline progress (▶ title · elapsed [bar] total) to the status bar

**Listener app (port 8001) — progress bar**
- Added segment progress bar + elapsed/total time below track title, driven by `duration` and `timestamp` fields already present in `/now-playing` response
- 1-second ticker updates bar width and time display; bar is hidden when no duration is available

**Project structure**
- Moved `listener-app/index.html` from project root into new `listener-app/` folder (mirrors `admin/` pattern)
- Added `listener-app/favicon.svg` — radio wave icon, adapts to OS dark/light preference
- `api_server.py` updated to serve from `listener-app/` and added `/favicon.svg` + `/favicon.ico` routes
- Added `Cache-Control: no-cache` header to HTML responses
- Updated systemd service descriptions: `writ-fm.service` → "WRIT-FM AI Radio Streamer & Listener App"; `writ-fm-admin.service` → "WRIT-FM Admin Interface (port 8080)"

### Current Verification Snapshot

- Git branch: `main`
- Tests: run `./.venv/bin/pytest -q` to verify (25 passed at last check)
- Both services active: `writ-fm.service` (streamer + listener app) and `writ-fm-admin.service` (admin)

## Previous Documentation Pass

Updated on 2026-04-26 to prepare handoff into Claude Code.

This pass reviewed:

- Recent git history through `4ed1aac` (`Simplify show config and fix source-driven generation`)
- Current checked-in config in `config/schedule.yaml`, `config/hosts.yaml`, and `config/segment_types.yaml`
- Local Claude memory files under `~/.claude/projects/-root/memory/`
- Current test status via `./.venv/bin/pytest -q`

## What Changed In This Session

- Refreshed `README.md` to match the current Linux-first, source-led station layout
- Updated `TODO.md`, `MILESTONES.md`, and `SCHEDULE.md` to reflect the present repo state
- Updated the architecture simplification plan to acknowledge progress already landed
- Rewrote `mac/operator_prompt.md` to stop referring to the stale show lineup and ACE-Step-centric workflows
- Added `handover.md` for the next agent

## Current Verification Snapshot

- Git branch: `main`
- HEAD: `4ed1aac`
- Worktree status at review time: clean before doc edits
- Tests: `25 passed` in about 10 seconds
- Known warning: FastAPI `on_event` deprecation in `admin/app.py`

## Notes On Prior Session Context

The local Claude memory is useful but partially stale. It still mentions older assumptions such as MiniMax not being wired in some places and older inventory notes. Treat it as background context, not source of truth.

For the current state, prefer:

- `handover.md`
- `TODO.md`
- `MILESTONES.md`
- `config/schedule.yaml`
