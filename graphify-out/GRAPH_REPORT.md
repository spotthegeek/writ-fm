# Graph Report - session-a-phases-0-1-2  (2026-07-26)

## Corpus Check
- 56 files · ~121,606 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 765 nodes · 1564 edges · 30 communities
- Extraction: 100% EXTRACTED · 0% INFERRED · 0% AMBIGUOUS · INFERRED: 7 edges (avg confidence: 0.8)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `ba6ac0e7`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]

## God Nodes (most connected - your core abstractions)
1. `generate_segment()` - 34 edges
2. `load_schedule()` - 27 edges
3. `run()` - 27 edges
4. `log()` - 24 edges
5. `_fetch_reddit_subreddit_context_with_strategy()` - 17 edges
6. `_build_segment_inventory()` - 16 edges
7. `build_generation_prompt()` - 16 edges
8. `_build_bumper_inventory()` - 15 edges
9. `_fetch_all_sources_for_briefing()` - 15 edges
10. `load_source_context()` - 15 edges

## Surprising Connections (you probably didn't know these)
- `_show_bumper_bounds()` --calls--> `load_schedule()`  [INFERRED]
  station/content_generator/music_bumper_generator.py → admin/app.py
- `_show_primary_host()` --calls--> `load_schedule()`  [INFERRED]
  station/content_generator/music_bumper_generator.py → admin/app.py
- `main()` --calls--> `load_schedule()`  [INFERRED]
  station/content_generator/talk_generator.py → admin/app.py
- `get_schedule_info()` --calls--> `load_schedule()`  [INFERRED]
  station/api_server.py → admin/app.py
- `run()` --calls--> `load_schedule()`  [INFERRED]
  station/stream_gapless.py → admin/app.py

## Import Cycles
- 1-file cycle: `admin/app.py -> admin/app.py`
- 1-file cycle: `admin/scheduler.py -> admin/scheduler.py`
- 1-file cycle: `station/stream_gapless.py -> station/stream_gapless.py`
- 1-file cycle: `tests/test_scheduler_stagger.py -> tests/test_scheduler_stagger.py`

## Communities (30 total, 0 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.06
Nodes (79): Popen, Thread, Start the HTTP API server in a daemon thread.      Args:         track_info: Mut, start_api_thread(), _acquire_instance_lock(), _apply_live_command_to_queue(), _build_display_queue(), _build_program_context_for_show() (+71 more)

### Community 1 - "Community 1"
Cohesion: 0.05
Nodes (58): _check_listener_auth(), check_process(), check_url(), _deny(), enqueue_live_command(), _find_next_show_start(), _find_show_end(), get_abs_sources() (+50 more)

### Community 2 - "Community 2"
Cohesion: 0.06
Nodes (49): _audio_duration_seconds(), bumper_count(), _display_name(), generate_bumpers_for_show(), _generate_custom_lyrics(), generate_music(), generate_one_bumper(), is_server_available() (+41 more)

### Community 3 - "Community 3"
Cohesion: 0.07
Nodes (49): _build_generation_env(), _cadence_ok(), _check_and_generate(), _cleanup_expired_segments(), _count_inventory(), _delete_segment(), _effective_generation_configs(), _load_schedule() (+41 more)

### Community 4 - "Community 4"
Cohesion: 0.05
Nodes (36): create_listener_token(), CreateTokenRequest, get_activity_log(), get_activity_segments(), get_live_upcoming(), get_show_taxonomy(), get_taxonomy_api(), _lifespan() (+28 more)

### Community 5 - "Community 5"
Cohesion: 0.09
Nodes (44): format_headlines(), _briefing_intro_line(), build_generation_prompt(), _build_reddit_story_script(), _fallback_two_host_script(), generate_segment(), get_segment_type_definition(), _google_performance_instructions() (+36 more)

### Community 6 - "Community 6"
Cohesion: 0.16
Nodes (32): log(), _bundle_listing_posts(), _clean_text_block(), _extract_reddit_comments(), _fetch_reddit_bundle(), _fetch_reddit_subreddit_context(), _fetch_reddit_subreddit_context_with_strategy(), _fetch_reddit_thread_context() (+24 more)

### Community 7 - "Community 7"
Cohesion: 0.11
Nodes (28): create_show(), delete_show(), GenerationConfig, get_generate_options(), get_schedule(), get_scheduler_status(), get_segment_types(), get_segment_types_api() (+20 more)

### Community 8 - "Community 8"
Cohesion: 0.09
Nodes (13): _generate(), _ok_response(), Tests for the Lyria 2 music backend and provider selection.  Covers the parts th, Signature must stay drop-in compatible with music_gen_client., A mistyped path must read as unconfigured, not as ready-to-generate., test_bearer_token_is_sent(), test_decodes_legacy_field_name(), test_minimax_only_params_are_accepted_and_ignored() (+5 more)

### Community 9 - "Community 9"
Cohesion: 0.14
Nodes (25): _audio_duration_seconds(), _backend_label(), _backend_origin_label(), _briefing_last_generated(), _build_bumper_inventory(), _build_segment_inventory(), _cached_inventory(), _expiry_info() (+17 more)

### Community 10 - "Community 10"
Cohesion: 0.18
Nodes (23): days_ago(), datetime, Regression tests for the inventory-threshold deadlock and generation staggering., Simulate the daily pass: a single-day cluster should spread out over the window., Mirror of the continuous-cadence trigger in _check_and_generate., should_run(), stagger(), test_above_threshold_does_not_run() (+15 more)

### Community 11 - "Community 11"
Cohesion: 0.12
Nodes (23): admin_ui(), _check_ingest_auth(), copy_library_item(), _copy_or_move_library_item(), _count_audio_files_per_show(), get_live_status(), get_status(), _library_base() (+15 more)

### Community 12 - "Community 12"
Cohesion: 0.10
Nodes (20): Acceptance Criteria, Architecture Simplification And Regression Test Plan, Delivery Phases, Documentation / Ops, Goal, Immediate Next Steps, Pacing, Phase 0: Test Harness And Safety Net (+12 more)

### Community 13 - "Community 13"
Cohesion: 0.15
Nodes (20): _canonical_source_key(), _choose_source_rule_for_show(), count_segments(), generate_all(), generate_for_current(), generate_for_show(), _is_youtube_collection_source(), main() (+12 more)

### Community 14 - "Community 14"
Cohesion: 0.13
Nodes (19): delete_bumper(), delete_segment(), _delete_segment_files(), GenerateRequest, _handle_post_regen(), _invalidate_inventory_cache(), _log_job(), _preview_text() (+11 more)

### Community 15 - "Community 15"
Cohesion: 0.11
Nodes (17): 1. Install dependencies, 2. Configure environment, 3. Configure the station, 4. Set up Icecast, 5. Run, Architecture, Credits, Docker (+9 more)

### Community 16 - "Community 16"
Cohesion: 0.12
Nodes (15): `admin/` — The Web Admin UI, `config/` — YAML Configuration, Content generators (`content_generator/`), Core services, Folder Structure, Key Environment Variables, `listener-app/` — Public Listener Web App, Other files (+7 more)

### Community 17 - "Community 17"
Cohesion: 0.19
Nodes (15): _default_host_assignment(), get_all_hosts(), get_hosts(), get_hosts_from_persona(), get_shows(), _hosts_yaml_to_api(), _normalize_show(), _primary_host_assignment_from_show() (+7 more)

### Community 18 - "Community 18"
Cohesion: 0.25
Nodes (13): clean_claude_output(), _extract_source_title(), fetch_headlines(), _find_child_text(), _llm_shorten(), make_short_title(), _normalize_title(), Return a display-safe short title (≤ SHORT_TITLE_MAX chars), title only.      Th (+5 more)

### Community 19 - "Community 19"
Cohesion: 0.19
Nodes (14): preprocess_for_tts(), _download_youtube_assets(), get_duration(), Render a single-voice script to audio., Render a long single-voice script in smaller pieces., Render a multi-voice script (panel/interview) to audio., Get audio duration in seconds., _read_webvtt_text() (+6 more)

### Community 20 - "Community 20"
Cohesion: 0.15
Nodes (12): Cleanup / Refactor, Current Status (2026-04-26, updated), Current Status (original, 2026-04-26), Generation Quality, High Priority, Immediate Priorities, Inventory / Production Readiness, Known Config Drift To Resolve (+4 more)

### Community 21 - "Community 21"
Cohesion: 0.23
Nodes (12): CompletedProcess, _fetch_all_sources_for_briefing(), _fetch_joke_api(), _fetch_rss_feed_items(), _fetch_url(), _normalize_youtube_source(), Batch-fetch source material from ALL research_sources for a briefing show., Fetch a batch of jokes from JokeAPI and return them as a bundled SourceContext. (+4 more)

### Community 22 - "Community 22"
Cohesion: 0.21
Nodes (5): Path, test_generate_segment_youtube_ingests_direct_audio_even_for_single_host_show(), test_render_single_voice_google_chunks_long_scripts(), test_render_single_voice_google_passes_wpm_to_provider(), test_render_single_voice_minimax_chunks_long_scripts()

### Community 23 - "Community 23"
Cohesion: 0.36
Nodes (9): create_host(), delete_host(), _host_update_to_yaml(), HostUpdate, _invalidate_hosts_cache(), load_hosts_config(), Load host definitions from config/hosts.yaml., save_hosts_config() (+1 more)

### Community 24 - "Community 24"
Cohesion: 0.36
Nodes (9): create_segment_type(), delete_segment_type(), get_segment_type_definitions(), load_segment_types_config(), Load managed segment type definitions from config/segment_types.yaml., save_segment_types_config(), _segment_type_update_to_yaml(), SegmentTypeUpdate (+1 more)

### Community 25 - "Community 25"
Cohesion: 0.22
Nodes (8): Current Snapshot (2026-04-26, updated), Milestone 1 — Pipeline Completeness, Milestone 2 — Generation Quality, Milestone 3 — Hosts & Voices Management, Milestone 4 — Segment Type And Source Management, Milestone 5 — Live Show Control, Parallel Track — Architecture Simplification, WRIT-FM Milestones

### Community 26 - "Community 26"
Cohesion: 0.22
Nodes (8): Current Verification Snapshot, Current Verification Snapshot, Notes On Prior Session Context, Previous Documentation Pass, Session 2026-04-26 (Claude Code), What Changed, What Changed In This Session, WRIT-FM Session Summary

### Community 27 - "Community 27"
Cohesion: 0.22
Nodes (7): Architecture, Commands, Config Files, graphify, Key Design Decisions, Key Environment Variables, Test Coverage

### Community 28 - "Community 28"
Cohesion: 0.29
Nodes (6): admin_login(), AdminAuthMiddleware, _make_admin_token(), _verify_admin_token(), BaseHTTPMiddleware, Request

### Community 29 - "Community 29"
Cohesion: 0.29
Nodes (6): 1. Sync source to doc02, 2. Build on doc02, 3. Distribute to doc03 and doc04 in parallel, 4. Confirm distribution, Notes, Steps

## Knowledge Gaps
- **84 isolated node(s):** `1. Sync source to doc02`, `2. Build on doc02`, `3. Distribute to doc03 and doc04 in parallel`, `4. Confirm distribution`, `Notes` (+79 more)
  These have ≤1 connection - possible missing edges or undocumented components.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `load_schedule()` connect `Community 7` to `Community 0`, `Community 1`, `Community 2`, `Community 4`, `Community 9`, `Community 11`, `Community 13`, `Community 14`, `Community 17`, `Community 23`, `Community 24`?**
  _High betweenness centrality (0.075) - this node is a cross-community bridge._
- **Why does `run()` connect `Community 0` to `Community 7`?**
  _High betweenness centrality (0.055) - this node is a cross-community bridge._
- **Why does `default_voice_for_backend()` connect `Community 17` to `Community 2`, `Community 4`, `Community 5`, `Community 14`?**
  _High betweenness centrality (0.048) - this node is a cross-community bridge._
- **Are the 5 inferred relationships involving `load_schedule()` (e.g. with `_show_bumper_bounds()` and `_show_primary_host()`) actually correct?**
  _`load_schedule()` has 5 INFERRED edges - model-reasoned connections that need verification._
- **What connects `1. Sync source to doc02`, `2. Build on doc02`, `3. Distribute to doc03 and doc04 in parallel` to the rest of the system?**
  _238 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Community 0` be split into smaller, more focused modules?**
  _Cohesion score 0.05583308845136644 - nodes in this community are weakly interconnected._
- **Should `Community 1` be split into smaller, more focused modules?**
  _Cohesion score 0.051759834368530024 - nodes in this community are weakly interconnected._