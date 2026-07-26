# Crouch-FM Improvement Plan

*Drafted 2026-07-26. Derived from the architecture & operations review of the same date.*

---

## Working state

> **Current phase:** Phases 0, 1, 2 complete (Session A, 2026-07-26). Phase 3 is next and unblocked.
> **Last updated:** 2026-07-26
> **Plan status:** approved, all six decisions settled — see [Decision log](#decision-log). No phase is blocked on input.
>
> **Read [Deviations](#deviations) before starting Phase 3 or 4.** Two findings change later
> work: the disk problem is mostly *outside* `output/` (Phase 0 could not reach its target),
> and `config/hosts.yaml` overrides `persona.py` at import — a class of override that Phase 4
> should assume exists elsewhere.

**How to use this file.** One session per phase (0+1+2 can share one — they are small and land
together). Open a session with:

> Read `docs/plans/2026-07-26-improvement-plan.md`. Execute Phase N only.
> Use subagents where the phase notes say they help.

Tick each task as it lands, update **Current phase** above, and record anything you learned
that contradicts the plan in [Deviations](#deviations). A task is only ticked once its
phase's verification step passes — not when the code is written.

Subagents are suppressed by default in this project's sessions. If a phase below says they
help, **say so explicitly in the prompt** or they will not be used. Any subagent that
explores code must be told to run `graphify query` before grepping — the project hook that
enforces this only fires on the main session.

### Progress

| Phase | | Status | Waits on |
|---|---|---|---|
| 0 | Reclaim the disk | ⚠ Done, target not met | — |
| 1 | Stop it refilling | ☑ Done | — |
| 2 | Fix the on-air name | ☑ Done | — |
| 3 | End the content starvation | ☐ Not started | — *ready now* |
| 4 | Delete what is dead | ☐ Not started | Phase 3 |
| 5 | Make supply visible | ☐ Not started | — |
| 6 | Programming improvements | ☐ Not started | Phase 3 |
| 7 | Finish the rebrand | ☐ Not started | — |

---

## Constraints this plan is written against

1. **The station must stay on air.** Every phase is designed to be applied to a running
   system. Where a restart is needed it is called out, and the streamer recovers in ~10s
   (`Restart=always`, `RestartSec=10`).
2. **One box, one to two listeners.** `fm01` is the whole deployment. Nothing here should
   add a service, a container, or a per-listener cost. Effort spent on scale is wasted;
   effort spent on unattended reliability is not.
3. **Sequenced by risk of going dark, then by visible value.** Disk first because it takes
   the station off air on its own. Branding second because it is an hour's work and is
   currently audible. Structural fixes third.
4. **Each phase lands independently.** No phase depends on a later one. You can stop after
   any of them and the system is in a better, coherent state.

### One correction to the review

The review said exhausted sources retry "every five minutes." That is the scheduler's
*check* interval. A flat 30-minute failure backoff already exists
(`admin/scheduler.py:54`, `FAILURE_BACKOFF_SECONDS = 1800`). Measured cadence is ~21 failed
jobs/day for the worst show. This does not change the diagnosis — it changes the fix in
Phase 3 from "add backoff" to "make the existing backoff escalate."

---

## Phases at a glance

| # | Phase | Why now | Effort | Risk | Subagents |
|---|-------|---------|--------|------|-----------|
| 0 | Reclaim the disk | 938 MB free; hard failure imminent | Small | Low | No — one careful operator |
| 1 | Stop it refilling | Phase 0 is worthless without it | Medium | Low | No |
| 2 | Fix the on-air name | Audible today; ~1 hour | Small | Low | No — too small to coordinate |
| 3 | End the content starvation | The actual engineering problem | Large | Medium | Tests only, after interfaces settle |
| 4 | Delete what is dead | Cheapest after 3 proves what's live | Medium | Low | **Yes** — one verifier per candidate |
| 5 | Make supply visible | Prevents recurrence of 2 & 3 | Medium | Low | **Yes** — panels are independent |
| 6 | Programming improvements | Quality, once supply is fixed | Medium | Low | Partly — 6.3 wants one context |
| 7 | Finish the rebrand | Deferred deliberately | Medium | Medium | No — lockstep changes |

---

## Phase 0 — Reclaim the disk

**Goal:** get root from 99% to under 80% today, with no code changes.
**Blocked by:** decision A.
**Subagents:** no. Destructive and irreversible; wants a single operator.

Root is 65 G used of 69 G, 938 MB free. When it fills, both the segment writer and
generation fail, and Icecast keeps streaming until the queue drains — a silent failure that
looks fine for about an hour.

- [x] **0.1** Delete `output/source_cache/youtube/**/*.mp3` (308 files). The audio has
      already been copied into `talk_segments/` for any segment that aired; the cache copy is
      redundant. Keep `.vtt` and `info.json` — small, and the actual dedupe/transcript record.
      **Reclaims ~7.3 GB.**
- [x] **0.2** Delete the 7,684 junk `youtube-ai` intro/outro/station-ID scripts in
      `output/scripts/` (written 13 Apr – 19 May, empty `source_value`, never referenced).
      ~8 MB.
- [x] **0.3** Sweep the 931 orphaned `.plays.json` sidecars whose audio no longer exists.
- [x] **0.4** Delete `output/jobs/*.json` older than 30 days (last real entry 13 Jun).
- [ ] **0.V** **Verify:** `df -h /` under 80%. Streamer log shows no gap. `/api/status`
      still returns.
      **Partially met.** 99% → **89%** (937 MB → 7.8 GB free). Streamer showed no gap and
      the API stayed up throughout. The 80% target is **not reachable from `output/` alone**
      — see [Deviations](#deviations) D1. All four deletions completed as specified.

0.2–0.4 are small, but they are the same files Phase 3 needs to stop scanning, so they are
worth doing in the same pass.

**Risk / rollback.** Deleting cached MP3s means a YouTube video whose segment has already
expired would need re-downloading if it ever came round again — but the used-source ledger
prevents that by design. No rollback needed; nothing here is a source of truth.

---

## Phase 1 — Stop it refilling

**Goal:** make retention a property of the system rather than something a human notices.
**Blocked by:** Phase 0.
**Subagents:** no.

Phase 0 buys headroom; without this it is gone again in ~10 weeks at the current fill rate.
There is currently **no retention policy anywhere except segment age**.

- [x] **1.1** Delete the cached MP3 after use. In `talk_generator.py`, the `youtube` segment
      path (`generate_segment`, ~L3313) does `shutil.copy2(source_audio, output_path)`.
      Unlink the cache copy immediately after a successful copy. Removes the growth at
      source rather than sweeping it later. *Highest-leverage change in the phase.*
- [x] **1.2** Add a janitor task to the scheduler. `run_scheduler` already has an hourly
      cleanup slot (`admin/scheduler.py:~765`). Extend it to enforce:
      `output/source_cache/` TTL (suggest 30 days on last touch);
      `output/scripts/` TTL — **⚠ must not run before Phase 3**, these files *are* the
      dedupe ledger today;
      orphan `.plays.json` / `.json` sidecars;
      `output/jobs/` beyond 30 days.
- [x] **1.3** Disk gauge in the admin UI — free space, `output/` breakdown, warning state
      under 10 GB. Cheap, and the thing that would have caught this.
- [x] **1.4** Reconsider audio quality. yt-dlp runs `--audio-quality 0` (best), ~24 MB per
      video, for content re-encoded to 96 kbps at the Icecast mount anyway. A 128 kbps
      target cuts the transient footprint ~5×.
- [ ] **1.V** **Verify:** leave it a week; `df` flat or falling. Janitor actions logged with
      byte counts.
      **Pending by design — needs a week of observation.** Code is deployed and running.
      Pre-flight done: the janitor was dry-run against the live corpus and matched an
      independent count exactly (674 orphaned sidecars, nothing else, none of the 56 live
      segments flagged). Byte-count logging is in. **Check `df` around 2026-08-02.**

---

## Phase 2 — Fix the on-air name

**Goal:** the hosts stop saying "WRIT-FM."
**Blocked by:** nothing.
**Subagents:** no — contained to one file.

Of 550 scripts written in the last 30 days, **62 say WRIT-FM and 132 say Crouch**. The
station contradicts itself several times an hour. Highest visible-value-per-hour item in the
plan.

**Cause.** `config/station.yaml` already says `Crouch-FM`, and it *is* plumbed correctly:
`schedule.station_name` → `generate_segment(station_name=...)` → `show_context["station_name"]`
→ `build_host_prompt` (`persona.py:332`). The override works. What defeats it is that each
host's `identity` prose hardcodes the old name and is injected verbatim regardless —
`persona.py:54, 101, 138, 175, 213` ("*You are The Liminal Operator, the voice of WRIT-FM*",
"*Nyx, the night voice of WRIT-FM*", …), plus the station lore block at L40 and the fallback
at L351.

- [x] **2.1** Replace the literal in all five host `identity` blocks, the lore block (L40),
      and the `show_name` fallback (L351) with a placeholder resolved from station context.
- [x] **2.2** Delete `STATION_NAME = "WRIT-FM"` (`persona.py:35`) and the two
      default-argument uses in `talk_generator.py` (L2212, L3172). Make `station_name`
      required and sourced from config — a wrong name should be a config error, not a silent
      default.
- [x] **2.3** Update the fixed `station_id` prompt template (`talk_generator.py:219`) if it
      carries the name.
- [x] **2.4** Flush the current segment pool so the old name stops airing immediately. Talk
      expires in 3 days anyway; a manual flush plus a generation trigger makes it minutes.
- [x] **2.V** **Verify:** generate one segment per on-air show, grep the scripts for `WRIT`
      — expect zero. Then spot check the audio.

**Risk.** Low, contained to prompt text. The 64-test suite covers voice resolution, not
prompt content, so it will not catch a regression here — 2.1 wants a careful diff read.

---

## Phase 3 — End the content starvation

**Goal:** shows stop running out of material, and the failure path stops being expensive.
**Blocked by:** decision B.
**Subagents:** for test-writing only, once the interfaces are settled. The core refactor
needs one coherent mental model of the generator held in a single context — splitting it is
how you get an index that does not quite reproduce the old key set.

The core problem: **the fetch window is 25 posts, the dedupe window is forever.**
`_fetch_reddit_subreddit_context_with_strategy` pulls one listing page (`limit=25`) filtered
to `lookback_days`, then discards any post whose permalink appears anywhere in the show's
entire history. On a slow subreddit the intersection empties within weeks. 2,992 failed jobs
since 20 May; production down from 259/month to 67.

Largest phase, and the one most worth splitting across sessions — 3a/3b, soak a day, then 3c/3d.

### 3a — Widen the intake
- [ ] **3.1** Page the Reddit listing past the first 25 results (`after` cursor), to a cap.
- [ ] **3.2** When the recent window comes back dry, fall back to `top`/`all` before failing.
      A subreddit with 15 years of archive is not exhausted after six weeks.
- [ ] **3.3** Apply the same widening to `_select_youtube_video_url_from_collection` — the
      four `youtube-ai` channels exhaust the same way (889 failures).

### 3b — Let the used-set expire
- [ ] **3.4** Age the used-source ledger out after the window set in decision B, so
      evergreen material recycles.

### 3c — Make the hot path cheap
- [ ] **3.5** Replace `_used_source_keys_for_show()` (`talk_generator.py:828`) — currently
      globs and `json.loads` all 9,903 files in `output/scripts/` (49 MB) on **every**
      attempt, ~11 attempts per failing job — with a single per-show index under
      `output/state/`. Append on write, prune by the 3b window.
      **Invariant to assert before switching:** the new index must reproduce the same key
      set as the old scan, checked against the current corpus.
      Unblocks the scripts-TTL deferred from 1.2.

### 3d — Fail quietly
- [ ] **3.6** Make `FAILURE_BACKOFF_SECONDS` escalate (30 min → 1 h → 4 h → 12 h) instead of
      staying flat, resetting on success.
- [ ] **3.7** Distinguish *cold* (source legitimately has nothing new) from *failed*
      (something broke) in the scheduler log and state. Both currently read as
      `Generation failed (exit 1)`, which is why two months of starvation looked like noise.
- [ ] **3.8** Clear the `briefing_daily` orphan — the show no longer exists in `shows.yaml`
      but the scheduler has tried it 63 times.

### Tests to add
- [ ] **3.9** Index/scan equivalence; listing pagination; used-set expiry boundary; backoff
      escalation.
- [ ] **3.V** **Verify:** failure count per day drops an order of magnitude. All five on-air
      shows sit at or above `min_inventory: 8` for a full week. `talesfromtechsupport`
      climbs off 1.

---

## Phase 4 — Delete what is dead

**Goal:** the tree stops describing a system that no longer exists.
**Blocked by:** Phase 3 (so anything 3 turns out to need is still present); decisions C, D.
**Subagents: yes — the natural fit.** One verifier per deletion candidate, each proving
nothing live imports or invokes it, all reporting back *before* anything is removed. A wrong
answer here means deleting live code.

~2,500 lines are provably not executing. Separate from `DORMANT_CODE.md` (13 Jun), which
covered unused imports and dead branches — these are whole features.

- [ ] **4.1** `listener_response_generator.py` + `listener_daemon.sh` + streamer support
      (`get_listener_responses`, priority sort, `/message` path). ~400 lines.
- [ ] **4.2** `config/schedule.yaml` — superseded by the station/shows/sources split, never
      loaded. 1,110 lines.
- [ ] **4.3** Docker / Swarm: `Dockerfile`, `docker-compose.yml`, `docker-stack.yml`,
      `deploy/`, `.claude/commands/deploy-image.md`. ~350 lines. *Per decision C.*
- [ ] **4.4** Operator loop: `run_operator.sh`, `operator_daemon.sh`, `operator_prompt.md` —
      shells to `gemini`, not installed, macOS PATH. ~190 lines.
- [ ] **4.5** Discogs + QR (`discogs_lookup.py`, `qr_generator.py`) — no token set, built for
      real music tracks. 456 lines.
- [ ] **4.6** ACE-Step (`music_gen_client.py`, `start_music_gen.sh`) — backend is Lyria.
      201 lines. Also fix the stale `"model": "ace-step"` metadata label.
- [ ] **4.7** Local Kokoro venv path + inline renderer — unreachable while
      `KOKORO_SERVICE_URL` is set. ~180 lines. Simplify rather than delete outright.
- [ ] **4.8** Tidy: `archive/`, `tools/backfill_short_titles.py`, `writ-fm.pid`,
      `SCHEDULE.md`, `WRIT-FM-Screenshot.png`, `CLAUDE_CODE_ISSUE_…md`, and the two stale
      repo copies in `.claude/worktrees/`.
- [ ] **4.8b** **Reclaim the ~32 GB outside the repo.** Added after Session A, which found
      that Phase 0's disk problem was mostly *not* in `output/` — see [D1](#deviations).
      Root is still at **89%** (7.7 GB free) after Phase 0 did everything it could.
      This is the item that actually fixes it.

      | Path | Size | Status | Verified |
      |---|---|---|---|
      | `/root/music-gen.server` | 16 GB (9.6 GB checkpoints) | ACE-Step model + venv | Backend is Lyria — **pair with 4.6** |
      | `/root/.cache/uv` | 11 GB | uv package cache | Regenerable |
      | `/root/writ-fm` | 14 GB (5.8 venv / 1.9 output / 1.0 `.git`) | **Stale pre-move copy of this repo** | Untouched since 26 Apr, nothing newer than 1 Jun, no process cwd, both units use `/code/writ-fm` |

      **Sizes do not add up, and that is expected.** Measured separately they total 41 GB;
      measured in one `du` invocation, which counts each hardlinked inode once, they total
      **32 GB**. The uv cache hardlinks into the venvs, so 32 GB is the real figure. Deleting
      all three takes root from 89% to roughly **40%**.

      Cautions, in the order they bite:
      - **`/root/writ-fm/output/` holds 1.9 GB of its own segments and sidecars.** Confirm
        nothing there is unique before deleting — it is a different corpus from
        `/code/writ-fm/output/`, not a copy of it.
      - `/root/writ-fm/.git` is a full 1 GB history. Confirm it has no commits absent from
        the live repo (`git log --oneline` against `origin/main`) before removing it.
      - ACE-Step checkpoints are only dead if 4.6 lands. Do them together or not at all.
      - Prefer `uv cache prune` (removes unused entries) over `uv cache clean`. Because of
        the hardlinking above, pruning may free far less than 11 GB — measure, do not assume.
        The cache is shared with `music-gen.server`, so deleting that first changes the sum.
      - All three are **root-owned and outside the repo**, so none of this is covered by
        decision A. Treat it as its own decision.

      *Session A did not touch any of it — out of scope for Phase 0, and deleting another
      repo copy is not an unattended call. `uv cache prune` was attempted and blocked by the
      tool sandbox.*
- [ ] **4.9** Correct `CLAUDE.md`. It still names `config/schedule.yaml` as the config
      authority and describes `api_server.py` as a standalone third service (it is a thread
      inside the streamer). Both send future work to the wrong place. Update the Kokoro note
      if 4.7 lands.
- [ ] **4.10** Sandbox the tests. `tests/` only patches `SCRIPTS_DIR` in one test, so running
      `pytest` writes real state into `output/scripts/` — there are currently two forked
      rotation files for `youtube-ai` because of it. Patch it in `conftest.py` suite-wide.
- [ ] **4.11** Retire the 8 shows that never air: signal_report, crosswire, listener_hours,
      midnight_signal, dawn_chorus, sonic_archaeology, the_groove_lab, dark_jokes. Only
      dark_jokes still generates — into a void. *Per decision D.*
- [ ] **4.V** **Verify:** full test suite green. Services restart clean.
      `graphify update .` to re-sync the graph.

---

## Phase 5 — Make supply visible

**Goal:** the next starvation is obvious in a day, not two months.
**Blocked by:** nothing (better after 3, but not dependent).
**Subagents: yes** — the panels are independent of each other.

Everything below is computed inside the generator today and then discarded.

- [ ] **5.1** Source health panel (admin UI). Per source rule: posts available in the
      current window, how many already used, last successful fetch, cold-until timestamp.
      *Highest value in this phase* — starvation is currently only detectable by reading
      journals or noticing a show repeat itself.
- [ ] **5.2** Inventory trend. Segments on hand per show against `min_inventory` over 7
      days. The downward slope is the leading indicator; the absolute count is lagging.
- [ ] **5.3** Play log. `station/play_history.py` already records what aired; nothing
      surfaces it. Segments by play count, unique vs repeated hours, per-show rotation
      depth. Would have shown one segment reaching 283 plays long before it got there.
- [ ] **5.4** Disk gauge, if not already done in 1.3.

---

## Phase 6 — Programming improvements

**Goal:** better radio, once supply is reliable.
**Blocked by:** Phase 3; decision E for 6.2.
**Subagents:** partly — 6.1 and 6.2 are separable, 6.3 wants one context.

Deliberately last — none of this is worth doing while shows are starving.

- [ ] **6.1** Evergreen bank per show. ~10 timeless segments each (station identity pieces,
      format explainers, archive readings) marked unlimited lifetime, played only when live
      inventory hits zero. The direct fix for "one file looping across a three-hour block."
      Cheap: generate once.
- [ ] **6.2** Put the briefings on air. Four daily briefings generate reliably and are only
      reachable through an app two people open. A three-minute briefing at the top of each
      hour gives the linear stream a spine, uses content that already exists, and reduces
      load on the exhausted Reddit sources. *Per decision E.*
- [ ] **6.3** Split `talk_generator.py`. 3,938 lines, ~1,100 of which are source acquisition
      (Reddit OAuth, PullPush fallback, yt-dlp, RSS, WebVTT, joke APIs). A coherent
      `sources/` package behind one `fetch(rule) -> SourceContext` contract, and the part
      most likely to need changes as feeds break. Best done *after* Phase 3, which rewrites
      much of it anyway.

---

## Phase 7 — Finish the rebrand

**Goal:** close out the naming, having already fixed the part that matters in Phase 2.
**Blocked by:** decision F.
**Subagents:** no — 7.2 is a lockstep change across `.env`, settings, and both systemd units.

~250 occurrences of `writ`/`WRIT` across 37 tracked files, in four layers of very different
cost. Phase 2 handled layer 1.

- [ ] **7.1** Docs, comments, tests (layer 2). README, `CLAUDE.md`, docstrings, fixtures with
      `station_name="WRIT-FM"`. Mechanical, no runtime effect. Worth doing so the tree stops
      teaching the old name to future sessions.
- [ ] **7.2** Environment variables (layer 3). 42 distinct `WRIT_*` vars. Rename via a
      compatibility shim in `shared/settings.py`: read `CROUCH_X` first, fall back to
      `WRIT_X`, warn on the fallback. Removes the lockstep-coordination risk across `.env`,
      both systemd units and every `os.environ.get` call. Drop the shim a month later.
      *Per decision F.*
- **7.3** Filesystem paths and service names (layer 4) — **not being done.** Out of scope
      per decision F; listed here so a future session does not re-propose it.
      `/code/writ-fm`, both unit names, the editable-install `.pth`, the credentials path
      `~/.config/writ-fm/lyria-sa.json`, the pid file. Most work, only part that risks
      downtime, only part no human ever sees. `GOOGLE_VERTEX_PROJECT` is already
      `crouch-fm`. If wanted, do it as part of a rebuild rather than in-place.
      *Per decision F.*

---

## Decision log

Answers here are binding for the phases that reference them. Anything still marked
**PROPOSED** is my recommendation only and has not been confirmed — do not act on it.

All six are settled. Answers here are binding for the phases that reference them; change one
by editing this table and noting it in [Deviations](#deviations).

| | Phase | Question | Decision | Confirmed |
|---|---|---|---|---|
| **A** | 0 | Delete the 7.3 GB of cached YouTube MP3s? | ✅ **Delete.** Redundant with `talk_segments/`; the used-source ledger stops the same video being fetched twice | 2026-07-26 |
| **B** | 3b | How long before used source material may be re-aired? | ✅ **90 days.** At 1–2 listeners the repetition cost is near zero, and it is the difference between a subreddit being a finite and an effectively infinite source | Default |
| **C** | 4.3 | Docker/Swarm path: delete, or keep dormant? | ✅ **Delete.** `Dockerfile`, both compose files, `deploy/`, `.claude/commands/deploy-image.md`. Production is plain systemd | 2026-07-26 |
| **D** | 4.11 | The 8 shows that never air: delete, or disable? | ✅ **Move to `shows.disabled.yaml`.** Keeps the host/voice/taxonomy work; reviving a show becomes a one-line move | 2026-07-26 |
| **E** | 6.2 | Put the daily briefings on the linear clock? | ✅ **Yes, top of each hour.** Free content, gives the stream shape, takes ~1 h/day of load off the exhausted Reddit sources | 2026-07-26 |
| **F** | 7 | Rename env vars? Rename paths and services? | ✅ **Vars yes** (behind a compat shim), **paths no.** The path rename is the most work, the only downtime risk, and invisible to any human | Default |

B and F were taken as working defaults rather than explicitly confirmed — both are cheap to
revisit right up until their phase starts.

---

## Deviations

*Record anything found during execution that contradicts this plan — a line reference that
moved, a count that was wrong, an approach that did not survive contact. Later phases read
this section.*

### Session A (Phases 0, 1, 2) — 2026-07-26

**D1 — The disk problem is mostly outside `output/`. Phase 0 could not reach 80%.**
The plan attributes the 99% to `output/`. `output/` was only **8.2 GB of a 69 GB root**.
Reclaiming 6.9 GB of it moved root 99% → **89%**, and nothing further in Phase 0's scope
remains. The actual top consumers, found only after the deletions:

| Path | Size | What it is |
|---|---|---|
| `/root/music-gen.server` | 13 GB | ACE-Step, incl. **9.6 GB of model checkpoints**. Phase 4.6 declares this backend dead (Lyria replaced it) |
| `/root/.cache/uv` | 11 GB | uv package cache. Regenerable |
| `/root/writ-fm` | 8.4 GB | **A stale second copy of this repo** — its own `.venv` (5.5 GB), `output/` (1.9 GB), `.git` (1 GB) |

`/root/writ-fm` is confirmed dead: untouched since 26 Apr, nothing newer than 1 Jun, no
process has it as cwd, and both systemd units use `/code/writ-fm`. **None of this was
deleted** — all three sit outside Phase 0's scope and outside decision A, and deleting
another repo copy is not a call to make unattended.

➡ **Now tracked as [task 4.8b](#phase-4--delete-what-is-dead)**, with per-path sizes,
verification status and cautions. Together they are **~32 GB** (not the 41 GB the
per-directory figures suggest — the uv cache hardlinks into the venvs, so a single `du`
invocation counts the shared inodes once). Clearing all three takes root from 89% to
roughly **40%**. `uv cache prune` was attempted in Session A and blocked by the tool
sandbox.

**D2 — Phase 0 counts.** 0.1 was 308 mp3s = **6.78 GB** (plan said ~7.3 GB), plus one stray
17 MB `.webm` left beside its own mp3, deleted with it. 0.2 was exactly 7,684 files as
stated (7.66 MB), all confirmed to contribute nothing to the dedupe ledger —
`_canonical_source_key()` returns `""` for an empty `source_value`. 0.3 was **1,171**
orphaned `.plays.json`, not 931. A further **674 orphaned metadata `.json`** sidecars
existed that 0.3 does not mention; left for the 1.2 janitor, which then reaped them. 0.4 was
all 101 job files (every one was >30 days old).

**D3 — Phase 2's stated cause was incomplete, and the missing part was the load-bearing one.**
`config/hosts.yaml` overrides `persona.HOSTS` **field-by-field at import**
(`persona.py:248-255`), and it hardcoded the old name in all five `identity` blocks. The
YAML is the text that actually ships. **Task 2.1 as written — persona.py only — would have
changed nothing on air.** Both files were fixed.

*Generalise this for Phase 4:* a `config/*.yaml` file can silently override a Python
default. Do not conclude code is dead, or that a constant is authoritative, without
checking for a YAML override of the same name.

**D4 — Two more injected strings carried the old name, unlisted in 2.1.**
`TIME_PERIOD_MOODS["morning"]["mood"]` ("But still WRIT") and
`["night"]["operator_state"]` ("prime time for WRIT"). `mood` is injected into **every**
prompt via the `CURRENT STATE` block. Both fixed and now covered by a test.

**D5 — More silent `"WRIT-FM"` defaults on the live path than 2.2 lists.** Beyond
`persona.py:35` and the two in `talk_generator.py`: `StationSchedule.station_name`
(`schedule.py:284`) and its parser (`:504`), and three in `admin/app.py` (`get_settings`,
`SettingsUpdate`, `update_settings` — the last silently reset the station to the old name
on an empty form submit). `schedule.station_name` feeds `generate_segment(station_name=...)`,
so it was live. All now raise instead of defaulting.

**D6 — 2.4's flush was almost a no-op, and a blanket flush would have risked dead air.**
Only **1 of 62** live segments contained the old name, and it belonged to `dark_jokes`,
which is **not on the base clock** (on-air shows are exactly: nosleep, sysadmin,
alien_theory, talesfromtechsupport, youtube-ai). Nothing that airs was affected. That one
segment was removed; no flush of on-air inventory was performed or needed. *This
independently confirms 4.11's claim that dark_jokes generates into a void.*

**D7 — Evidence for Phase 4.2 (`config/schedule.yaml`).** Production calls
`load_schedule(config/)` — the **directory**, not the file (`SCHEDULE_PATH` is
`PROJECT_ROOT / "config"`). The directory load yields 13 shows and `Crouch-FM`; loading
`config/schedule.yaml` directly yields 11 shows. Consistent with 4.2's "never loaded", and
now positively evidenced rather than assumed.

**D8 — Visible old-name branding that Phase 2 deliberately did not touch.** Not host
speech, so out of Phase 2's scope, but they are *seen* rather than buried in a docstring
and Phase 7.1 should not miss them: `stream_gapless.py:1114` `-ice_name "WRIT-FM"`
(**displayed in every listener's player**), `stream_gapless.py:793` (a `station_id` →
`"WRIT-FM"` display label), and `listener-app/index.html` (`<title>` and brand text).
Changing `ice_name` needs an Icecast reconnect.

**D9 — 1.2 scope calls.** The `output/scripts/` TTL was **not** implemented, as instructed —
the code says why, so a later session does not "fix" the omission. The `source_cache` TTL
was scoped to **media files only** (`.mp3/.m4a/.webm/.opus/.wav`), preserving `.vtt` and
`info.json` per Phase 0's reasoning. Orphan-sidecar sweeping is restricted to the two
segment trees so it can never reach `output/scripts/` or the
`.<show>_source_rotation.json` rotation state that also lives there.

**D10 — Repo state this landed on.** `/code/writ-fm` had **1,516 lines of uncommitted work**
(Lyria backend, scheduler stagger, admin UI) and `origin/main` was a commit behind local
`HEAD`. The running station *is* that dirty working tree. Work was done on branch
`worktree-session-a-phases-0-1-2`, whose first commit snapshots that state so Phases 1-2
apply to production reality. The changes were then applied to the live working tree (still
uncommitted, as found) and both services restarted. **`gh` is not installed on this box**,
so the PR was pushed but not opened.

**D11 — Generating a segment by hand needs `.env`.** A manual `talk_generator.py` run as
`claude` fails with `PermissionError` on `station/kokoro/.venv/bin/python`, because without
`.env` loaded it falls back to the local Kokoro venv (root-owned) instead of
`KOKORO_SERVICE_URL`. Production is unaffected — the services load `.env` and run as
`User=claude`. Source `.env` first, or run via the scheduler. Also: generating via `sudo`
leaves **root-owned** files under `output/` that the services then cannot delete; `chown`
them back to `claude`.

---

## Explicitly out of scope

- Anything that adds a service, container, or host.
- The `youtube` segment format rebroadcasting third-party audio verbatim. Fine for a private
  LAN mount; would need revisiting only if the stream ever went public. Flagged, not planned.
- The line-level findings already catalogued in `CODE_REVIEW.md` and `DORMANT_CODE.md`
  (13 Jun) — unused imports, `datetime.now()` calls, non-atomic YAML writes. Worth a separate
  cleanup pass; not folded in here to keep the phases reviewable.
- Multi-listener features, public streaming, authentication hardening beyond what exists.

---

## Suggested order of execution

Phases 0 → 1 → 2 can all land in one sitting and remove both immediate outage risks plus the
audible branding bug. Phase 3 is the substantial one and wants its own session or two, with a
day of soak between 3a/3b and 3c. Phase 4 follows naturally once 3 has proved what is live.
5, 6 and 7 are independent of each other and can be picked up in any order, or dropped.

---

## Appendix — session prompts

Paste-ready opening prompts, one per session, matching the execution order above. Sessions A
and B–C map to more than one phase; the rest are one-to-one.

The **Deviations** instruction at the end of each is load-bearing. It is how a later session
finds out that an earlier one moved a line reference or invalidated an assumption, and it is
the most likely way this plan goes wrong across sessions.

Subagents are suppressed by default in this project. Where a prompt below does not ask for
them, that is deliberate.

### Session A — Phases 0, 1, 2

```
Read docs/plans/2026-07-26-improvement-plan.md. That file is the source of truth;
decisions A–F at the bottom are settled and binding.

Execute Phases 0, 1 and 2 only. Do not start Phase 3.

Notes:
- Phase 0 is destructive and irreversible. Show me the file list and byte counts
  for each of 0.1–0.4 before deleting anything.
- In task 1.2, do NOT implement the output/scripts/ TTL. Those files are the dedupe
  ledger until Phase 3.5 replaces them. Everything else in 1.2 is fine to build.
- Phase 2 is prompt text in persona.py. The test suite does not cover prompt content,
  so it will not catch a regression — read the diff carefully.
- Restart writ-fm.service and writ-fm-admin.service yourself when needed; don't ask me to.

When done: tick the checkboxes, update the Working state header and Progress table,
and record anything that contradicted the plan under Deviations.
```

Splittable into three sessions if preferred — each phase stands alone.

### Session B — Phase 3a + 3b

```
Read docs/plans/2026-07-26-improvement-plan.md. Decisions A–F are settled and binding.

Execute Phase 3a (tasks 3.1–3.3) and 3b (task 3.4) only. Stop before 3c.

Notes:
- Decision B sets the re-air window at 90 days.
- These change which source gets selected. Roll out to one show first, watch a
  generation cycle, then apply to the rest.
- Do not touch _used_source_keys_for_show() yet — that's 3.5, next session.

Leave it to soak for a day before I start the next session.

When done: tick the checkboxes, update Working state and Progress, record Deviations.
```

### Session C — Phase 3c + 3d

```
Read docs/plans/2026-07-26-improvement-plan.md. Decisions A–F are settled and binding.
Check the Deviations section — Phase 3a/3b ran in a previous session.

Execute Phase 3c (3.5), 3d (3.6–3.8) and the tests in 3.9. Do not start Phase 4.

Notes:
- 3.5 has a mandatory invariant: the new output/state/ index must reproduce exactly
  the same key set as the current full scan of output/scripts/. Assert that against
  the live corpus and show me the result BEFORE switching over.
- Once 3.5 lands, the output/scripts/ TTL deferred from task 1.2 becomes safe. Add it.
- 3.7 matters more than it looks: two months of starvation read as generic
  "Generation failed (exit 1)" in the logs. Cold and failed must be distinguishable.
- Restart services yourself.

When done: tick the checkboxes, update Working state and Progress, record Deviations.
```

### Session D — Phase 4

```
Read docs/plans/2026-07-26-improvement-plan.md. Decisions A–F are settled and binding.
Read the Deviations section first — Phase 3 may have changed what is live.

Execute Phase 4 only.

Use subagents for this phase. Spawn one verifier per deletion candidate (4.1, 4.3–4.7),
each proving nothing live imports, invokes or references it. All must report back
before anything is removed — a wrong answer here means deleting live code.

Any subagent that explores code must run `graphify query "<question>"` before grepping.
The project hook that enforces this only fires on the main session, so put it in
each subagent prompt.

Decisions already made: C = delete the Docker/Swarm path outright.
D = move the 8 dead shows to shows.disabled.yaml, do not delete them.

Task 4.9 (correcting CLAUDE.md) is not optional cleanup — the file currently tells
every new session that config/schedule.yaml is the config authority and that
api_server.py is a standalone service. Both are wrong.

Verify with the full test suite, a clean restart of both services, and `graphify update .`.

When done: tick the checkboxes, update Working state and Progress, record Deviations.
```

### Session E — Phase 5

```
Read docs/plans/2026-07-26-improvement-plan.md. Decisions A–F are settled and binding.
Read the Deviations section first.

Execute Phase 5 only.

Use subagents — the three panels (5.1, 5.2, 5.3) are independent of each other and
touch different parts of admin/app.py and admin/index.html. Watch for collisions in
index.html; assign each subagent a distinct section or serialise the edits.

Any subagent exploring code must run `graphify query` before grepping — the enforcing
hook only fires on the main session.

5.1 is the one that matters. Everything it needs is already computed inside the
generator and thrown away.

When done: tick the checkboxes, update Working state and Progress, record Deviations.
```

### Session F — Phase 6

```
Read docs/plans/2026-07-26-improvement-plan.md. Decisions A–F are settled and binding.
Read the Deviations section first.

Execute Phase 6 only.

Decision E is settled: put the daily briefings on the linear clock at the top of
each hour (task 6.2).

Subagents: 6.1 and 6.2 are separable and can run in parallel. Do NOT split 6.3 —
restructuring talk_generator.py into a sources/ package needs one coherent mental
model of the module in a single context.

6.3 is the largest item. If context runs short, land 6.1 and 6.2 and leave 6.3 for
its own session rather than half-finishing it.

When done: tick the checkboxes, update Working state and Progress, record Deviations.
```

### Session G — Phase 7

```
Read docs/plans/2026-07-26-improvement-plan.md. Decisions A–F are settled and binding.
Read the Deviations section first.

Execute Phase 7, tasks 7.1 and 7.2 only.

Task 7.3 (renaming /code/writ-fm, the systemd units, the .pth file and the
credentials path) is explicitly NOT being done — decision F. Do not propose it.

7.2 is a lockstep change across .env, shared/settings.py, both systemd units and
every os.environ.get call. Use the compat shim: read CROUCH_X first, fall back to
WRIT_X, warn on the fallback. A missed variable fails silently by falling back to
a default, so the shim is what makes this safe.

No subagents — the coordination is the hard part and it wants one context.

Verify both services restart clean and the streamer reconnects to Icecast.
Restart them yourself.

When done: tick the checkboxes, update Working state and Progress, record Deviations.
```
