# Graph Report - session-b-phase-3a-3b  (2026-07-26)

## Corpus Check
- 59 files · ~130,290 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 212 nodes · 471 edges · 12 communities
- Extraction: 100% EXTRACTED · 0% INFERRED · 0% AMBIGUOUS
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `12f4639c`
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

## God Nodes (most connected - your core abstractions)
1. `generate_segment()` - 31 edges
2. `Crouch-FM Improvement Plan` - 17 edges
3. `_clean_text_block()` - 14 edges
4. `_fetch_reddit_subreddit_context_with_strategy()` - 14 edges
5. `_fetch_all_sources_for_briefing()` - 14 edges
6. `load_source_context()` - 14 edges
7. `SourceContext` - 13 edges
8. `build_generation_prompt()` - 13 edges
9. `_fetch_reddit_thread_context()` - 12 edges
10. `generate_for_show()` - 12 edges

## Surprising Connections (you probably didn't know these)
- `build_generation_prompt()` --calls--> `get_segment_type_definition()`  [EXTRACTED]
  station/content_generator/talk_generator.py → station/content_generator/talk_generator.py  _Bridges community 6 → community 8_
- `generate_segment()` --calls--> `get_segment_type_definition()`  [EXTRACTED]
  station/content_generator/talk_generator.py → station/content_generator/talk_generator.py  _Bridges community 6 → community 7_
- `main()` --calls--> `segment_word_targets()`  [EXTRACTED]
  station/content_generator/talk_generator.py → station/content_generator/talk_generator.py  _Bridges community 6 → community 2_
- `generate_segment()` --calls--> `_source_size_words()`  [EXTRACTED]
  station/content_generator/talk_generator.py → station/content_generator/talk_generator.py  _Bridges community 9 → community 7_
- `_fetch_reddit_subreddit_context_with_strategy()` --calls--> `_min_source_words_for()`  [EXTRACTED]
  station/content_generator/talk_generator.py → station/content_generator/talk_generator.py  _Bridges community 6 → community 1_

## Import Cycles
- 1-file cycle: `tests/test_source_widening.py -> tests/test_source_widening.py`
- 2-file cycle: `station/content_generator/talk_generator.py -> tests/test_source_widening.py -> station/content_generator/talk_generator.py`

## Communities (12 total, 0 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.11
Nodes (37): datetime, fetch_recorder(), _key(), _listing_payload(), _permalink(), _post(), Phase 3a/3b — the fetch window is no longer one page and the dedupe window is no, On a newest-first listing an all-stale page means every later page is     older (+29 more)

### Community 1 - "Community 1"
Cohesion: 0.11
Nodes (38): _bundle_listing_posts(), _clean_text_block(), _extract_reddit_comments(), _fetch_joke_api(), _fetch_reddit_bundle(), _fetch_reddit_subreddit_context(), _fetch_reddit_subreddit_context_with_strategy(), _fetch_reddit_thread_context() (+30 more)

### Community 2 - "Community 2"
Cohesion: 0.10
Nodes (27): _canonical_source_key(), _choose_source_rule_for_show(), _choose_unused_listing_post(), count_segments(), generate_all(), generate_for_current(), generate_for_show(), _is_youtube_collection_source() (+19 more)

### Community 3 - "Community 3"
Cohesion: 0.29
Nodes (10): _kokoro_speed_from_wpm(), _pace_wpm_for_assignment(), _primary_host_assignment(), Average speaking pace of the hosts who will actually voice this segment., _secondary_host_assignment(), segment_effective_wpm(), _selected_guest(), _uses_secondary_host_dialogue() (+2 more)

### Community 4 - "Community 4"
Cohesion: 0.18
Nodes (11): _normalize_dialogue_speaker(), _parse_dialogue_parts(), Render a single-voice script to audio., Render a long single-voice script in smaller pieces., Render a multi-voice script (panel/interview) to audio., Pick a topic from the pool matching the show's focus., render_multi_voice(), render_single_voice() (+3 more)

### Community 5 - "Community 5"
Cohesion: 0.14
Nodes (18): CompletedProcess, _download_youtube_assets(), _fetch_all_sources_for_briefing(), get_duration(), _normalize_youtube_source(), Watch URL for a flat-playlist entry, or "" when it carries no id., True when an entry is inside the lookback window, or carries no date.      Flat-, Batch-fetch source material from ALL research_sources for a briefing show. (+10 more)

### Community 6 - "Community 6"
Cohesion: 0.25
Nodes (9): get_segment_type_definition(), _min_source_words_for(), Run LLM to generate the script., Smallest source a segment type can work with, 0 when it does not care., Word band for a segment, derived from runtime minutes where declared.      For s, resolve_word_targets(), run_generation(), _segment_counts_comments() (+1 more)

### Community 7 - "Community 7"
Cohesion: 0.29
Nodes (8): _briefing_intro_line(), _fallback_two_host_script(), generate_segment(), _host_label(), Return a date-stamped intro sentence for briefing segment types., Generate a single talk segment with audio., _show_value(), _slugify_topic()

### Community 8 - "Community 8"
Cohesion: 0.40
Nodes (5): build_generation_prompt(), _google_performance_instructions(), _minimax_performance_instructions(), Build the full prompt for content generation., _two_host_prompt_prefix()

### Community 9 - "Community 9"
Cohesion: 0.50
Nodes (4): Rough size of the usable material for a segment.      Comments are excluded for, Map source size onto 0.0-1.0 across the configured span., _source_size_fraction(), _source_size_words()

### Community 10 - "Community 10"
Cohesion: 0.67
Nodes (3): _build_reddit_story_script(), _normalize_reddit_story_text(), Remove Reddit/Markdown noise while keeping the post content intact.

### Community 11 - "Community 11"
Cohesion: 0.06
Nodes (33): 3a — Widen the intake, 3b — Let the used-set expire, 3c — Make the hot path cheap, 3d — Fail quietly, Appendix — session prompts, Constraints this plan is written against, Crouch-FM Improvement Plan, Decision log (+25 more)

## Knowledge Gaps
- **28 isolated node(s):** `Progress`, `One correction to the review`, `Phases at a glance`, `Phase 0 — Reclaim the disk`, `Phase 1 — Stop it refilling` (+23 more)
  These have ≤1 connection - possible missing edges or undocumented components.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `generate_segment()` connect `Community 7` to `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 8`, `Community 9`, `Community 10`?**
  _High betweenness centrality (0.027) - this node is a cross-community bridge._
- **Why does `generate_for_show()` connect `Community 2` to `Community 1`, `Community 4`, `Community 7`?**
  _High betweenness centrality (0.012) - this node is a cross-community bridge._
- **What connects `Progress`, `One correction to the review`, `Phases at a glance` to the rest of the system?**
  _73 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Community 0` be split into smaller, more focused modules?**
  _Cohesion score 0.10975609756097561 - nodes in this community are weakly interconnected._
- **Should `Community 1` be split into smaller, more focused modules?**
  _Cohesion score 0.11095305832147938 - nodes in this community are weakly interconnected._
- **Should `Community 2` be split into smaller, more focused modules?**
  _Cohesion score 0.10256410256410256 - nodes in this community are weakly interconnected._
- **Should `Community 5` be split into smaller, more focused modules?**
  _Cohesion score 0.1437908496732026 - nodes in this community are weakly interconnected._