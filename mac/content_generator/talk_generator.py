#!/usr/bin/env python3
"""
WRIT-FM Talk Segment Generator

Generates long-form talk show content for the talk-first radio format.
Uses Claude CLI for scripts and Kokoro TTS for rendering.

Segment types:
  Long-form (primary content, 1500-3000 words):
    deep_dive       - Extended single-topic exploration
    news_analysis   - Current events through late-night lens (uses RSS headlines)
    interview       - Simulated interview with historical/fictional figure
    panel           - Two hosts discuss topic from different angles
    story           - Narrative storytelling, true stories from music/culture
    listener_mailbag - Invented listener letters + responses
    music_essay     - Extended essay on artist/album/genre

  Short-form (transitions):
    station_id      - 15-30 word station identification
    show_intro      - 80-150 word show opening
    show_outro      - 60-120 word show closing

Usage:
    uv run python talk_generator.py                                # Current show
    uv run python talk_generator.py --show midnight_signal --count 5
    uv run python talk_generator.py --type deep_dive --topic "why vinyl matters"
    uv run python talk_generator.py --all --count 3                # 3 per show
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from html import unescape
from pathlib import Path
import re
import urllib.parse
import urllib.request
import urllib.error

import yaml

sys.path.insert(0, str(Path(__file__).parent))

from helpers import log, preprocess_for_tts, fetch_headlines, format_headlines, run_claude

warnings.filterwarnings(
    "ignore",
    message=r"dropout option adds dropout after all but last recurrent layer.*",
    category=UserWarning,
)
warnings.filterwarnings(
    "ignore",
    message=r"`torch\.nn\.utils\.weight_norm` is deprecated.*",
    category=FutureWarning,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCHEDULE_PATH = PROJECT_ROOT / "config" / "schedule.yaml"
SEGMENT_TYPES_PATH = PROJECT_ROOT / "config" / "segment_types.yaml"
OUTPUT_DIR = PROJECT_ROOT / "output" / "talk_segments"
SCRIPTS_DIR = PROJECT_ROOT / "output" / "scripts"
SOURCE_CACHE_DIR = PROJECT_ROOT / "output" / "source_cache"
YOUTUBE_CACHE_DIR = SOURCE_CACHE_DIR / "youtube"

sys.path.insert(0, str(PROJECT_ROOT / "mac"))
from schedule import load_schedule, StationSchedule
from time_utils import station_now, station_iso_now

sys.path.insert(0, str(Path(__file__).parent))
from persona import HOSTS, get_host, build_host_prompt, STATION_NAME

# =============================================================================
# SEGMENT TYPE DEFINITIONS
# =============================================================================

DEFAULT_SEGMENT_TYPES = {
    "deep_dive": {
        "name": "Deep Dive",
        "word_count_min": 1500,
        "word_count_max": 2500,
        "multi_voice": False,
        "prompt_template": """Write an extended exploration of this topic. Go deep.
Build your central idea through stories, examples, tangents.
Let one thought lead naturally to another. Circle back to earlier threads.
Include specific details: years, names, places when relevant.
Structure: open with a hook, develop through 3-4 connected ideas, land somewhere unexpected.
Use [pause] for natural rhythm. Output ONLY the spoken words.""",
    },
    "news_analysis": {
        "name": "News Analysis",
        "word_count_min": 1500,
        "word_count_max": 2000,
        "multi_voice": False,
        "prompt_template": """Analyze these headlines through a late-night lens.
Don't just report - interpret. What patterns do you see? What's being missed?
Connect current events to deeper themes. Ask the questions daytime anchors don't.
Be thoughtful, not reactive. Skeptical but not cynical.

HEADLINES:
{headlines}

Use [pause] for natural rhythm. Output ONLY the spoken words.""",
    },
    "interview": {
        "name": "Interview",
        "word_count_min": 2000,
        "word_count_max": 3000,
        "multi_voice": True,
        "prompt_template": """Write a simulated interview where {primary_host_name} talks with {guest_name}.
Format with HOST: and GUEST: markers on separate lines.
The guest is a fictional/composite character, not a real living person being impersonated.
The conversation should feel natural - interruptions, tangents, moments of surprise.
Build to genuine insight or revelation.
Use [pause] for natural rhythm. Output ONLY the spoken dialogue.""",
    },
    "panel": {
        "name": "Panel",
        "word_count_min": 2000,
        "word_count_max": 3000,
        "multi_voice": True,
        "prompt_template": """Write a discussion between {primary_host_name} and {secondary_host_name} on this topic.
Format with HOST_A: and HOST_B: markers on separate lines.
They have different perspectives but mutual respect.
The conversation should build - start with disagreement, find nuance, reach unexpected common ground.
Include moments of genuine surprise and humor.
Use [pause] for natural rhythm. Output ONLY the spoken dialogue.""",
    },
    "story": {
        "name": "Story",
        "word_count_min": 1500,
        "word_count_max": 2500,
        "multi_voice": False,
        "prompt_template": """Tell a story. It can be true, apocryphal, or mythological - but tell it like it happened.
Good stories have specific details: the color of the room, the year, the weather.
Build tension. Let the listener wonder where this is going.
The ending should reframe everything that came before.
Use [pause] for dramatic effect. Output ONLY the spoken words.""",
    },
    "reddit_storytelling": {
        "name": "Reddit Storytelling",
        "word_count_min": 1200,
        "word_count_max": 2200,
        "multi_voice": False,
        "prompt_template": """Read the Reddit post as a story, not a summary.
Stay close to the original wording and arc. Do not add commentary or analysis.
Use light performance cues sparingly where they fit the story: [pause], [sigh], [laugh], [chuckle].
If the post is already a story, preserve its pacing and tone.
Do not discuss comments, external links, or your own reaction.
Output ONLY the spoken words.""",
    },
    "reddit_post": {
        "name": "Reddit Post",
        "word_count_min": 1400,
        "word_count_max": 2200,
        "multi_voice": False,
        "prompt_template": """Turn this Reddit thread into a compelling on-air segment.
Open by grounding the listener in the subreddit and what kind of post this is.
Retell the original post clearly and vividly in radio-friendly language.
Bring in a handful of revealing, funny, skeptical, or emotionally resonant comments.
If the post links to outside material, weave in the useful parts without sounding like you're reading a webpage.
Distinguish between the original post, the community reaction, and the host's own interpretation.
Output ONLY the spoken words.""",
    },
    "youtube": {
        "name": "YouTube",
        "word_count_min": 1400,
        "word_count_max": 2400,
        "multi_voice": False,
        "prompt_template": """Turn this YouTube video into a compelling on-air segment.
Ground the listener in the channel, the title, and what kind of video this is.
Use the transcript, audio-derived notes, and metadata as your primary source material.
Summarize the key beats, arguments, or story clearly and vividly.
If there is no usable transcript, work from the title, description, chapters, and metadata.
Keep the narration radio-friendly. Output ONLY the spoken words.""",
    },
    "listener_mailbag": {
        "name": "Listener Mailbag",
        "word_count_min": 1500,
        "word_count_max": 2000,
        "multi_voice": False,
        "prompt_template": """Write a segment responding to invented listener messages.
Create 2-3 messages from listeners (with first names and cities).
Each message should touch on something real - a memory, a question, a feeling.
Respond to each with genuine warmth and thoughtfulness.
Format: read the message, then respond. Natural transitions between letters.
Use [pause] for natural rhythm. Output ONLY the spoken words.""",
    },
    "music_essay": {
        "name": "Music Essay",
        "word_count_min": 1500,
        "word_count_max": 2500,
        "multi_voice": False,
        "prompt_template": """Write an extended essay about music.
This is not a review. It's a love letter, an excavation, a meditation.
Pick a specific angle: a single song, a studio, a year, a collaboration, a genre's birth.
Use vivid, sensory language. Make the listener hear what you're describing.
Be specific with details but universal with feeling.
Use [pause] for natural rhythm. Output ONLY the spoken words.""",
    },
    "station_id": {
        "name": "Station ID",
        "word_count_min": 15,
        "word_count_max": 30,
        "multi_voice": False,
        "prompt_template": """Write a short, direct station ID for {show_name} on {station_name}.
Use one sentence. Be plain and on-air, not poetic.
Mention the show name and station name if it fits naturally.
Do not invent imagery or a backstory.
Output ONLY the spoken text. No quotes, headers, or explanations.""",
    },
    "show_intro": {
        "name": "Show Intro",
        "word_count_min": 80,
        "word_count_max": 150,
        "multi_voice": False,
        "prompt_template": """Write an 80-150 word opening for the show.
Welcome listeners. Set the mood. Hint at what's ahead without being specific.
Ground the listener in time and space - what hour is it, what kind of night.
Output ONLY the spoken text.""",
    },
    "show_outro": {
        "name": "Show Outro",
        "word_count_min": 60,
        "word_count_max": 120,
        "multi_voice": False,
        "prompt_template": """Write a 60-120 word show closing.
Thank the listener for staying. Acknowledge the time spent together.
Hint at what's next on the station. Leave them with something to carry.
Output ONLY the spoken text.""",
    },
}


def load_segment_type_definitions() -> dict[str, dict]:
    data: dict[str, dict] = {}
    if SEGMENT_TYPES_PATH.exists():
        try:
            payload = yaml.safe_load(SEGMENT_TYPES_PATH.read_text()) or {}
            data = payload.get("segment_types", {}) or {}
        except Exception as exc:
            log(f"Failed to load segment_types.yaml: {exc}")
    merged = {key: dict(value) for key, value in DEFAULT_SEGMENT_TYPES.items()}
    for sid, cfg in data.items():
        if sid in merged:
            merged[sid] = {**merged[sid], **(cfg or {})}
        else:
            merged[sid] = cfg or {}
    return merged


SEGMENT_DEFINITIONS = load_segment_type_definitions()


def get_segment_type_definition(segment_type: str) -> dict:
    return SEGMENT_DEFINITIONS.get(segment_type, DEFAULT_SEGMENT_TYPES["deep_dive"])


def segment_word_targets(segment_type: str) -> tuple[int, int]:
    cfg = get_segment_type_definition(segment_type)
    return int(cfg.get("word_count_min", 1500)), int(cfg.get("word_count_max", 2500))

# =============================================================================
# TOPIC POOLS
# =============================================================================

TOPIC_POOLS = {
    "philosophy": [
        "The 3am mind - why we think differently in darkness",
        "Alone together - the paradox of mass media intimacy",
        "The archaeology of memory - how songs excavate the past",
        "Waiting rooms of the soul - the liminal spaces we inhabit",
        "The democracy of insomnia - who else is awake right now",
        "Time as texture - why some hours feel longer than others",
        "The comfort of routine - rituals that hold us together",
        "Nostalgia as navigation - using the past to find the future",
        "The weight of small things - objects that carry meaning",
        "Silence as sound - what we hear when nothing plays",
        "The myth of productivity - what we lose when everything must be useful",
        "Boredom as portal - what happens when we stop filling every moment",
        "The loneliness of crowds versus the company of solitude",
        "Why we tell stories to strangers in the dark",
        "The philosophy of night shifts - what the invisible economy teaches us",
    ],
    "music_history": [
        "The secret history of the B-side - when the throwaway becomes the classic",
        "How geography shaped sound - the cities that invented genres",
        "The lost art of the album sequence - why track order matters",
        "Recording studios as instruments - rooms that shaped decades of music",
        "The sample and the sampled - how old records live in new ones",
        "One-hit wonders who deserved more - careers that should have been",
        "The technology of music - from wax cylinders to streaming algorithms",
        "Regional scenes that never crossed over - local sounds lost to time",
        "Pirate radio - outlaws of the airwaves and the sounds they set free",
        "The golden age of the record shop - archaeology for the ears",
        "How jazz escaped from New Orleans and conquered the world",
        "The birth of electronic music - when machines learned to feel",
        "Ethiopian jazz and the sound of a country's golden age",
        "The DJ as curator - the art of selection and sequence",
        "Vinyl mastering - the physics of grooves and the art of the cut",
    ],
    "current_events": [
        "What the headlines aren't telling you this week",
        "The economy of attention - who benefits when we're distracted",
        "Technology and trust - the crisis nobody's naming",
        "The changing shape of cities after midnight",
        "Climate reports and the language of urgency",
        "The state of journalism at the end of the world",
        "Immigration stories that don't fit the narrative",
        "The education system as a mirror of what we value",
        "Healthcare access and the geography of survival",
        "The gig economy and the myth of freedom",
    ],
    "culture": [
        "The coffee shop as third place - where strangers become regulars",
        "Night shift workers - the invisible economy that keeps everything running",
        "The last video stores - temples to a dying format",
        "Diners at 2am - confessionals with unlimited refills",
        "24-hour establishments - who keeps the lights on and why",
        "The changing meaning of downtown after dark",
        "Bookstores as sanctuaries - the quiet resistance of print",
        "The art of the mix tape - playlists as unsent letters",
        "Street food and the democracy of flavor",
        "Public transportation at night - the bus as equalizer",
    ],
    "soul_music": [
        "What makes a song 'soul' - it's not a genre, it's an approach",
        "The Muscle Shoals sound and the white musicians who played Black",
        "Motown's assembly line of heartbreak",
        "The gospel roots that feed every groove",
        "Curtis Mayfield and the politics of the bassline",
        "Neo-soul and the question of authenticity",
        "The art of the slow jam - why vulnerability needs a groove",
        "Funk as philosophy - Parliament and the mothership connection",
        "Erykah Badu and the church of vibe",
        "Disco's death and resurrection - who killed the dance floor and who brought it back",
    ],
    "night_philosophy": [
        "What the dark knows that the light doesn't",
        "Sleep as surrender - why we resist the thing we need most",
        "Dreams as the radio station of the subconscious",
        "The 4am confession - why truth comes easier in darkness",
        "Nocturnal animals and what they teach us about seeing differently",
        "The history of the night - how humans learned to occupy the dark",
        "Insomnia as unwanted clarity",
        "The night sky before light pollution - what we lost when we lit up the world",
        "Lullabies and the ancient technology of singing someone to sleep",
        "Why creativity peaks after midnight",
    ],
    "listeners": [
        "Letters from the frequency - your messages answered",
        "The songs that changed your lives - listener stories",
        "Questions from the dark - what you've always wanted to know",
        "Dedications and confessions from the inbox",
        "Where are you listening from? - the geography of our audience",
    ],
}

# Guest characters for interview segments
INTERVIEW_GUESTS = [
    {"name": "a retired record store owner from Detroit", "context": "Spent 40 years curating vinyl for a neighborhood"},
    {"name": "a sound engineer who worked on legendary sessions", "context": "Was in the room when history was made on tape"},
    {"name": "a radio historian", "context": "Studies the golden age of pirate and community radio"},
    {"name": "a jazz archivist from a university collection", "context": "Cataloging a century of forgotten recordings"},
    {"name": "a night shift nurse who listens to us every night", "context": "Knows the hospital's secret soundtrack"},
    {"name": "a former musician who chose to listen instead of play", "context": "Understanding music differently from the audience"},
    {"name": "a street food vendor who works the late shift", "context": "The city's midnight economy and its soundtrack"},
    {"name": "a librarian who specializes in sound recordings", "context": "Preserving voices that time is trying to erase"},
]

REDDIT_USER_AGENT = os.environ.get(
    "WRIT_REDDIT_USER_AGENT",
    "WRIT-FM/1.0 (contact: admin@writ.fm)"
)
REDDIT_TIMEOUT_SECONDS = int(os.environ.get("WRIT_REDDIT_TIMEOUT", "10"))
REDDIT_COMMENT_LIMIT = int(os.environ.get("WRIT_REDDIT_COMMENT_LIMIT", "6"))
REDDIT_STORY_SUBREDDITS = {
    "nosleep",
    "prorevenge",
    "writingprompts",
    "writingprompt",
    "shortscarystories",
    "tifu",
    "amitheasshole",
    "offmychest",
    "pettyrevenge",
}


@dataclass
class SourceContext:
    source_type: str
    source_value: str
    title: str = ""
    topic: str = ""
    body: str = ""
    transcript: str = ""
    comments: list[str] = field(default_factory=list)
    source_material: str = ""
    format_instructions: str = ""
    subreddit: str = ""
    story_mode: bool = False
    channel: str = ""
    duration_seconds: float | None = None
    audio_path: str = ""
    transcript_source: str = ""


# =============================================================================
# CORE GENERATION
# =============================================================================


def select_topic(topic_focus: str, segment_type: str) -> str:
    """Pick a topic from the pool matching the show's focus."""
    pool = TOPIC_POOLS.get(topic_focus, [])
    if not pool:
        # Fall back to a combined pool
        all_topics = []
        for topics in TOPIC_POOLS.values():
            all_topics.extend(topics)
        pool = all_topics
    return random.choice(pool)


def _fetch_url(url: str, timeout: int = REDDIT_TIMEOUT_SECONDS) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": REDDIT_USER_AGENT,
            "Accept": "application/json,text/html,application/xhtml+xml",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def _reddit_listing_base() -> str:
    return os.environ.get("WRIT_REDDIT_LISTING_BASE", "https://old.reddit.com").rstrip("/")


def _reddit_thread_base() -> str:
    return os.environ.get("WRIT_REDDIT_THREAD_BASE", "https://www.reddit.com").rstrip("/")


def _pullpush_base() -> str:
    return os.environ.get("WRIT_REDDIT_PULLPUSH_BASE", "https://api.pullpush.io").rstrip("/")


def _clean_text_block(text: str) -> str:
    text = unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _normalize_reddit_user_mentions(text: str) -> str:
    text = text or ""
    text = re.sub(r"(?<!\w)/u/([A-Za-z0-9_-]+)", r"\1", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<!\w)u/([A-Za-z0-9_-]+)", r"\1", text, flags=re.IGNORECASE)
    return text


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _html_to_text(html: str, max_chars: int = 8000) -> str:
    html = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", html)
    html = re.sub(r"(?i)</(p|div|section|article|h1|h2|h3|li|br)>", "\n", html)
    text = _clean_text_block(html)
    return _truncate(text, max_chars)


def _fetch_web_source(url: str) -> tuple[str, str]:
    try:
        raw = _fetch_url(url).decode("utf-8", errors="ignore")
    except Exception as exc:
        log(f"Failed to fetch linked URL {url}: {exc}")
        return "", ""

    title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
    title = _clean_text_block(title_match.group(1)) if title_match else url
    body = _html_to_text(raw)
    return title, body


def _normalize_reddit_source(source_value: str) -> tuple[str, str]:
    value = (source_value or "").strip()
    if not value:
        return "", ""
    if value.startswith("/r/"):
        return "subreddit", value[3:].strip("/")
    if re.fullmatch(r"r/[A-Za-z0-9_]+", value):
        return "subreddit", value[2:].strip("/")
    if re.fullmatch(r"[A-Za-z0-9_]+", value):
        return "subreddit", value.strip("/")
    parsed = urllib.parse.urlparse(value if "://" in value else "https://" + value)
    path = parsed.path.rstrip("/")
    if "/comments/" in path:
        return "thread", urllib.parse.urlunparse(parsed._replace(query="", fragment=""))
    m = re.fullmatch(r"/r/([A-Za-z0-9_]+)", path)
    if m:
        return "subreddit", m.group(1)
    return "thread", urllib.parse.urlunparse(parsed._replace(query="", fragment=""))


def _canonical_source_key(source_type: str, source_value: str) -> str:
    source_type = (source_type or "").strip().lower()
    source_value = (source_value or "").strip()
    if not source_type or not source_value:
        return ""
    if source_type in {"reddit", "reddit_thread"}:
        kind, normalized = _normalize_reddit_source(source_value)
        return f"reddit:{kind}:{normalized.rstrip('/')}"
    if source_type == "reddit_subreddit":
        _, subreddit = _normalize_reddit_source(source_value)
        return f"reddit_subreddit:{subreddit.lower().lstrip('r/').lstrip('/')}"
    if source_type in {"youtube", "youtube_video"}:
        video_id = _youtube_video_id(source_value)
        return f"youtube_video:{video_id or source_value.rstrip('/')}"
    if source_type in {"youtube_channel", "youtube_playlist"}:
        return f"{source_type}:{source_value.rstrip('/')}"
    return f"{source_type}:{source_value.rstrip('/')}"


def _used_source_keys_for_show(show_id: str) -> set[str]:
    used: set[str] = set()
    if not SCRIPTS_DIR.exists():
        return used
    for path in SCRIPTS_DIR.glob("talk_*.json"):
        try:
            meta = json.loads(path.read_text())
        except Exception:
            continue
        if str(meta.get("show_id", "")).strip() != show_id:
            continue
        source_type = str(meta.get("source_type") or "").strip().lower()
        source_value = str(meta.get("source_value") or "").strip()
        key = _canonical_source_key(source_type, source_value)
        if key:
            used.add(key)
    return used


def _source_rule_sort_key(rule: dict) -> tuple:
    return (
        str(rule.get("type") or ""),
        str(rule.get("value") or ""),
        str(rule.get("segment_type") or ""),
    )


def _choose_source_rule_for_show(show, show_id: str, preferred_segment_type: str | None = None) -> dict | None:
    rules = [_normalize_source_rule(rule) for rule in getattr(show, "source_rules", []) if isinstance(rule, dict)]
    rules = [rule for rule in rules if rule["value"]]
    if not rules:
        return None

    if preferred_segment_type and preferred_segment_type != "random":
        matching = [rule for rule in rules if rule["segment_type"] == preferred_segment_type]
        if matching:
            rules = matching

    eligible: list[dict] = []
    for rule in rules:
        source_type = str(rule["type"])
        source_value = str(rule["value"])
        if source_type in {"reddit", "reddit_subreddit", "reddit_thread"}:
            if source_type == "reddit_subreddit":
                source_type = "reddit_subreddit"
            else:
                kind, _ = _normalize_reddit_source(source_value)
                source_type = "reddit_subreddit" if kind == "subreddit" else "reddit_thread"
        elif source_type in {"youtube", "youtube_video"}:
            if _is_youtube_collection_source(source_value):
                source_type = "youtube_channel"
            else:
                source_type = "youtube_video"
        key = _canonical_source_key(source_type, source_value)
        if key and key in _used_source_keys_for_show(show_id):
            continue
        eligible.append(rule)

    if not eligible:
        return None

    eligible = sorted(eligible, key=_source_rule_sort_key)
    if len(eligible) == 1:
        return eligible[0]

    history_path = SCRIPTS_DIR / f".{show_id}_source_rotation.json"
    last_key = ""
    try:
        if history_path.exists():
            last_key = str(json.loads(history_path.read_text()).get("last_key") or "")
    except Exception:
        last_key = ""

    def rule_key(rule: dict) -> str:
        src_type = str(rule["type"])
        src_value = str(rule["value"])
        if src_type in {"youtube", "youtube_video"} and _is_youtube_collection_source(src_value):
            src_type = "youtube_channel"
        elif src_type in {"reddit", "reddit_thread"}:
            kind, _ = _normalize_reddit_source(src_value)
            src_type = "reddit_subreddit" if kind == "subreddit" else "reddit_thread"
        return _canonical_source_key(src_type, src_value)

    if last_key:
        for idx, rule in enumerate(eligible):
            if rule_key(rule) == last_key:
                return eligible[(idx + 1) % len(eligible)]

    return random.choice(eligible)


def _record_source_rotation(show_id: str, rule: dict | None) -> None:
    if not rule:
        return
    try:
        SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        src_type = str(rule.get("type") or "")
        src_value = str(rule.get("value") or "")
        if src_type in {"youtube", "youtube_video"} and _is_youtube_collection_source(src_value):
            src_type = "youtube_channel"
        elif src_type in {"reddit", "reddit_thread"}:
            kind, _ = _normalize_reddit_source(src_value)
            src_type = "reddit_subreddit" if kind == "subreddit" else "reddit_thread"
        payload = {
            "last_key": _canonical_source_key(src_type, src_value),
            "updated_at": station_iso_now(),
        }
        (SCRIPTS_DIR / f".{show_id}_source_rotation.json").write_text(json.dumps(payload, indent=2))
    except Exception:
        pass


def _extract_reddit_comments(children: list[dict], limit: int = REDDIT_COMMENT_LIMIT) -> list[str]:
    comments: list[str] = []
    for child in children:
        data = (child or {}).get("data", {})
        if data.get("stickied"):
            continue
        body = _clean_text_block(data.get("body", ""))
        if not body:
            continue
        author = data.get("author", "unknown")
        score = data.get("score")
        score_text = f" ({score} upvotes)" if isinstance(score, int) else ""
        comments.append(f"- {author}{score_text}: {_truncate(_normalize_reddit_user_mentions(body), 400)}")
        if len(comments) >= limit:
            break
    return comments


def _fetch_reddit_thread_context(source_value: str) -> SourceContext:
    _, normalized = _normalize_reddit_source(source_value)
    json_url = normalized.rstrip("/") + ".json?limit=12&sort=top"
    payload = json.loads(_fetch_url(json_url).decode("utf-8", errors="ignore"))

    post_data = payload[0]["data"]["children"][0]["data"]
    comments_children = payload[1]["data"]["children"]

    subreddit = str(post_data.get("subreddit", "")).lower()
    story_mode = subreddit in REDDIT_STORY_SUBREDDITS
    title = _normalize_reddit_user_mentions(_clean_text_block(post_data.get("title", ""))) or "Untitled Reddit thread"
    selftext = _normalize_reddit_user_mentions(_clean_text_block(post_data.get("selftext", "")))
    permalink = "https://www.reddit.com" + post_data.get("permalink", "")
    author = post_data.get("author", "unknown")
    score = post_data.get("score", 0)
    comment_count = post_data.get("num_comments", 0)
    external_url = post_data.get("url_overridden_by_dest") or post_data.get("url") or ""

    comments = _extract_reddit_comments(comments_children)

    linked_summary = ""
    if external_url and "reddit.com" not in external_url and "redd.it" not in external_url:
        linked_title, linked_text = _fetch_web_source(external_url)
        if linked_text:
            linked_summary = (
                f"Linked material: {linked_title}\n"
                f"URL: {external_url}\n"
                f"{_truncate(linked_text, 3500)}"
            )

    source_material_parts = [
        f"Subreddit: r/{subreddit}" if subreddit else "",
        f"Thread title: {title}",
        f"Posted by: {author}",
        f"Score: {score} | Comments: {comment_count}",
        f"Permalink: {permalink}",
        "",
        "Original post:",
        selftext or "[No selftext body; rely on the title, comments, and any linked material.]",
    ]
    if comments:
        source_material_parts.extend(["", "Selected comments:"] + comments)
    if linked_summary:
        source_material_parts.extend(["", linked_summary])

    format_instructions = (
        "Tell this as a story first. Preserve the emotional arc, pacing, and key beats of the original post. "
        "Only after the retelling should the host step back and reflect on how the comments reframed or amplified it."
        if story_mode else
        "Balance retelling with commentary. Treat the post as the spine of the segment, then fold in the comments as a live chorus of reaction."
    )

    return SourceContext(
        source_type="reddit",
        source_value=source_value,
        title=title,
        topic=title,
        body=selftext,
        comments=comments,
        source_material="\n".join(part for part in source_material_parts if part is not None),
        format_instructions=format_instructions,
        subreddit=subreddit,
        story_mode=story_mode,
    )


def _reddit_context_from_listing_post(post_data: dict) -> SourceContext:
    subreddit = str(post_data.get("subreddit", "")).lower()
    story_mode = subreddit in REDDIT_STORY_SUBREDDITS
    title = _normalize_reddit_user_mentions(_clean_text_block(post_data.get("title", ""))) or "Untitled Reddit thread"
    selftext = _normalize_reddit_user_mentions(_clean_text_block(post_data.get("selftext", "")))
    permalink_path = str(post_data.get("permalink", "")).strip()
    permalink = f"{_reddit_thread_base()}{permalink_path}" if permalink_path.startswith("/") else permalink_path
    author = post_data.get("author", "unknown")
    score = post_data.get("score", 0)
    comment_count = post_data.get("num_comments", 0)
    external_url = post_data.get("url_overridden_by_dest") or post_data.get("url") or ""

    linked_summary = ""
    if external_url and "reddit.com" not in external_url and "redd.it" not in external_url:
        linked_title, linked_text = _fetch_web_source(external_url)
        if linked_text:
            linked_summary = (
                f"Linked material: {linked_title}\n"
                f"URL: {external_url}\n"
                f"{_truncate(linked_text, 3500)}"
            )

    source_material_parts = [
        f"Subreddit: r/{subreddit}" if subreddit else "",
        f"Thread title: {title}",
        f"Posted by: {author}",
        f"Score: {score} | Comments: {comment_count}",
        f"Permalink: {permalink}" if permalink else "",
        "",
        "Original post:",
        selftext or "[Thread JSON was unavailable. Rely on the title, listing metadata, and any linked material.]",
    ]
    if linked_summary:
        source_material_parts.extend(["", linked_summary])

    format_instructions = (
        "Tell this as a story first. Preserve the emotional arc and key beats from the post title and body. "
        "If comments are unavailable, focus on the original post and any linked material."
        if story_mode else
        "Balance retelling with commentary. Treat the post title and body as the spine of the segment. "
        "If Reddit comments are unavailable, do not invent them; reflect on the post itself and any linked material."
    )

    return SourceContext(
        source_type="reddit",
        source_value=permalink or title,
        title=title,
        topic=title,
        body=selftext,
        comments=[],
        source_material="\n".join(part for part in source_material_parts if part is not None),
        format_instructions=format_instructions,
        subreddit=subreddit,
        story_mode=story_mode,
    )


def _pullpush_fetch_subreddit_posts(
    subreddit: str,
    *,
    lookback_days: int = 7,
    selection_strategy: str = "latest",
) -> list[dict]:
    strategy = (selection_strategy or "latest").strip().lower()
    def _fetch(*, include_after: bool) -> list[dict]:
        params = {
            "subreddit": subreddit,
            "size": "50",
            "sort": "desc",
            "sort_type": "created_utc",
        }
        if include_after:
            after = int(time.time() - max(1, int(lookback_days)) * 86400)
            params["after"] = str(after)
        url = f"{_pullpush_base()}/reddit/search/submission/?{urllib.parse.urlencode(params)}"
        payload = json.loads(_fetch_url(url).decode("utf-8", errors="ignore"))
        data = payload.get("data") if isinstance(payload, dict) else []
        return [item for item in (data or []) if isinstance(item, dict)]

    posts = _fetch(include_after=True)
    if not posts:
        log(
            f"PullPush has no posts for r/{subreddit} within the last {lookback_days} day(s); "
            "retrying without the recency filter."
        )
        posts = _fetch(include_after=False)
    if strategy in {"top", "popular"}:
        posts.sort(key=lambda item: int(item.get("score") or 0), reverse=True)
    elif strategy == "random":
        random.shuffle(posts)
    else:
        posts.sort(key=lambda item: float(item.get("created_utc") or 0), reverse=True)
    return posts


def _fetch_reddit_subreddit_context(source_value: str) -> SourceContext:
    _, subreddit = _normalize_reddit_source(source_value)
    listing_url = f"{_reddit_listing_base()}/r/{subreddit}/hot.json?limit=8"
    posts = []
    try:
        payload = json.loads(_fetch_url(listing_url).decode("utf-8", errors="ignore"))
        for child in payload.get("data", {}).get("children", []):
            data = child.get("data", {})
            if data.get("stickied") or data.get("over_18"):
                continue
            title = _clean_text_block(data.get("title", ""))
            if not title:
                continue
            posts.append(data)
            if len(posts) >= 5:
                break
    except Exception as exc:
        log(f"Reddit hot listing blocked for r/{subreddit}; using PullPush fallback: {exc}")
        posts = _pullpush_fetch_subreddit_posts(subreddit, lookback_days=7, selection_strategy="popular")[:5]
    if not posts:
        raise RuntimeError(f"No usable posts found for r/{subreddit}")

    chosen = posts[0]
    permalink = f"{_reddit_thread_base()}{chosen.get('permalink', '')}"
    try:
        return _fetch_reddit_thread_context(permalink)
    except Exception as exc:
        log(f"Reddit thread fetch blocked for {permalink}; using listing fallback: {exc}")
        return _reddit_context_from_listing_post(chosen)


def _fetch_reddit_subreddit_context_with_strategy(
    source_value: str,
    lookback_days: int = 7,
    selection_strategy: str = "latest",
    used_source_keys: set[str] | None = None,
) -> SourceContext:
    _, subreddit = _normalize_reddit_source(source_value)
    strategy = (selection_strategy or "latest").strip().lower()
    sort = {
        "latest": "new",
        "top": "top",
        "popular": "hot",
        "random": "new",
    }.get(strategy, "new")
    url = f"{_reddit_listing_base()}/r/{subreddit}/{sort}.json?limit=25"
    if sort == "top":
        if lookback_days <= 1:
            t = "day"
        elif lookback_days <= 7:
            t = "week"
        elif lookback_days <= 30:
            t = "month"
        else:
            t = "year"
        url += f"&t={t}"

    cutoff = time.time() - max(1, int(lookback_days)) * 86400
    posts: list[dict] = []
    try:
        try:
            payload = json.loads(_fetch_url(url).decode("utf-8", errors="ignore"))
        except Exception as exc:
            fallback_url = f"{_reddit_listing_base()}/r/{subreddit}/hot.json?limit=25"
            log(f"Reddit listing fetch blocked for {url}; retrying hot listing: {exc}")
            payload = json.loads(_fetch_url(fallback_url).decode("utf-8", errors="ignore"))
        children = payload.get("data", {}).get("children", [])
        for child in children:
            data = (child or {}).get("data", {})
            if data.get("stickied") or data.get("over_18"):
                continue
            created = data.get("created_utc")
            if isinstance(created, (int, float)) and created < cutoff:
                continue
            title = _clean_text_block(data.get("title", ""))
            if not title:
                continue
            posts.append(data)
    except Exception as exc:
        log(f"Reddit listing API failed for r/{subreddit}; using PullPush fallback: {exc}")
        posts = _pullpush_fetch_subreddit_posts(
            subreddit,
            lookback_days=lookback_days,
            selection_strategy=selection_strategy,
        )
    if not posts:
        raise RuntimeError(f"No usable posts found for r/{subreddit}")
    used_source_keys = used_source_keys or set()
    chosen = None
    if strategy == "random":
        candidates = posts[:]
        random.shuffle(candidates)
        for post in candidates:
            permalink = f"{_reddit_thread_base()}{post.get('permalink', '')}"
            if _canonical_source_key("reddit_thread", permalink) not in used_source_keys:
                chosen = post
                break
    else:
        for post in posts:
            permalink = f"{_reddit_thread_base()}{post.get('permalink', '')}"
            if _canonical_source_key("reddit_thread", permalink) not in used_source_keys:
                chosen = post
                break
    if chosen is None:
        raise RuntimeError(f"No unused posts found for r/{subreddit}")
    permalink = f"{_reddit_thread_base()}{chosen.get('permalink', '')}"
    try:
        return _fetch_reddit_thread_context(permalink)
    except Exception as exc:
        log(f"Reddit thread fetch blocked for {permalink}; using listing fallback: {exc}")
        return _reddit_context_from_listing_post(chosen)


def _build_reddit_story_script(source_context: SourceContext) -> str:
    title = (source_context.title or "").strip()
    body = (source_context.body or "").strip()
    parts = []
    if title:
        parts.append(title)
    if body:
        parts.append(body)
    script = "\n\n".join(parts).strip()
    script = _normalize_reddit_story_text(script)
    return script or (source_context.source_material or source_context.title or "")


def _youtube_video_id(source_value: str) -> str:
    value = (source_value or "").strip()
    if not value:
        return ""
    parsed = urllib.parse.urlparse(value if "://" in value else "https://" + value)
    query = urllib.parse.parse_qs(parsed.query)
    if "v" in query and query["v"]:
        return query["v"][0]
    path_parts = [part for part in parsed.path.split("/") if part]
    if parsed.netloc.endswith("youtu.be") and path_parts:
        return path_parts[0]
    if "shorts" in path_parts:
        idx = path_parts.index("shorts")
        if idx + 1 < len(path_parts):
            return path_parts[idx + 1]
    if "embed" in path_parts:
        idx = path_parts.index("embed")
        if idx + 1 < len(path_parts):
            return path_parts[idx + 1]
    return path_parts[-1] if path_parts else value


def _normalize_youtube_source(source_value: str) -> str:
    value = (source_value or "").strip()
    if not value:
        return ""
    if "://" in value:
        return value
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        return f"https://www.youtube.com/watch?v={value}"
    return f"https://www.youtube.com/watch?v={value}"


def _is_youtube_collection_source(source_value: str) -> bool:
    value = (source_value or "").strip()
    if not value:
        return False
    parsed = urllib.parse.urlparse(value if "://" in value else "https://" + value)
    path_parts = [part for part in parsed.path.split("/") if part]
    if parsed.netloc.endswith("youtu.be"):
        return False
    if any(part.startswith("@") for part in path_parts) or "playlist" in path_parts or "channel" in path_parts:
        return True
    return False


def _youtube_collection_url(source_value: str) -> str:
    value = (source_value or "").strip()
    if not value:
        return ""
    parsed = urllib.parse.urlparse(value if "://" in value else "https://" + value)
    path_parts = [part for part in parsed.path.split("/") if part]
    if parsed.netloc.endswith("youtu.be"):
        return value
    if "playlist" in path_parts:
        return value
    if path_parts and path_parts[-1] == "videos":
        return value
    if any(part.startswith("@") for part in path_parts) or "channel" in path_parts:
        new_path = parsed.path.rstrip("/") + "/videos"
        return urllib.parse.urlunparse(parsed._replace(path=new_path))
    return value


def _show_value(show, key: str, default=None):
    if isinstance(show, dict):
        return show.get(key, default)
    return getattr(show, key, default)


def _run_yt_dlp(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "yt_dlp", *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _read_webvtt_text(path: Path) -> str:
    try:
        raw = path.read_text(errors="ignore")
    except Exception:
        return ""
    lines: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("WEBVTT"):
            continue
        if re.match(r"^\d+$", line):
            continue
        if "-->" in line:
            continue
        line = re.sub(r"<[^>]+>", "", line)
        if line:
            lines.append(line)
    text = re.sub(r"\s+", " ", " ".join(lines)).strip()
    return _truncate(text, 7000)


def _download_youtube_assets(source_value: str) -> tuple[dict, Path | None, str, str]:
    YOUTUBE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    source_url = _normalize_youtube_source(source_value)
    video_id = _youtube_video_id(source_value)
    video_dir = YOUTUBE_CACHE_DIR / video_id
    video_dir.mkdir(parents=True, exist_ok=True)
    info_path = video_dir / "info.json"

    if info_path.exists():
        try:
            info = json.loads(info_path.read_text())
        except Exception:
            info = {}
    else:
        info = {}

    if not info:
        meta = _run_yt_dlp(["--dump-single-json", "--no-playlist", "--skip-download", source_url], timeout=120)
        if meta.returncode != 0 or not meta.stdout.strip():
            raise RuntimeError(meta.stderr.strip() or "yt-dlp metadata fetch failed")
        info = json.loads(meta.stdout)
        info_path.write_text(json.dumps(info, indent=2))

    audio_path: Path | None = None
    cached_audio = info.get("_cached_audio_path", "")
    if cached_audio:
        cached_audio_path = Path(cached_audio)
        if cached_audio_path.exists():
            audio_path = cached_audio_path

    if audio_path is None:
        audio_tmpl = str(video_dir / f"{video_id}.%(ext)s")
        dl = _run_yt_dlp([
            "--no-playlist",
            "-x",
            "--audio-format",
            "mp3",
            "--audio-quality",
            "0",
            "-o",
            audio_tmpl,
            source_url,
        ], timeout=900)
        if dl.returncode != 0:
            raise RuntimeError(dl.stderr.strip() or "yt-dlp audio download failed")
        candidates = sorted(video_dir.glob(f"{video_id}.*"), key=lambda p: p.stat().st_mtime, reverse=True)
        audio_path = next((p for p in candidates if p.suffix.lower() in {".mp3", ".m4a", ".webm", ".opus", ".wav"}), None)
        if audio_path:
            info["_cached_audio_path"] = str(audio_path)
            info_path.write_text(json.dumps(info, indent=2))

    transcript = ""
    captions = sorted(video_dir.glob(f"{video_id}*.vtt"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not captions:
        sub_dl = _run_yt_dlp([
            "--no-playlist",
            "--skip-download",
            "--write-auto-subs",
            "--write-subs",
            "--sub-langs",
            "en.*,en",
            "--sub-format",
            "vtt",
            "-o",
            str(video_dir / f"{video_id}.%(ext)s"),
            source_url,
        ], timeout=300)
        if sub_dl.returncode == 0:
            captions = sorted(video_dir.glob(f"{video_id}*.vtt"), key=lambda p: p.stat().st_mtime, reverse=True)
    if captions:
        transcript = _read_webvtt_text(captions[0])

    return info, audio_path if audio_path and audio_path.exists() else None, transcript, video_id


def _select_youtube_video_url_from_collection(
    source_value: str,
    lookback_days: int = 7,
    selection_strategy: str = "latest",
    used_source_keys: set[str] | None = None,
) -> str:
    source_url = _youtube_collection_url(_normalize_youtube_source(source_value))
    fetch_args = [
        "--flat-playlist",
        "--dump-single-json",
        "--skip-download",
    ]
    if selection_strategy.strip().lower() == "random":
        fetch_args += ["--playlist-end", "25"]
    else:
        fetch_args += ["--playlist-end", "12"]
    fetch_args.append(source_url)
    meta = _run_yt_dlp(fetch_args, timeout=240)
    if meta.returncode != 0 or not meta.stdout.strip():
        raise RuntimeError(meta.stderr.strip() or "yt-dlp playlist fetch failed")
    payload = json.loads(meta.stdout)
    entries = payload.get("entries", []) if isinstance(payload, dict) else []
    cutoff = time.time() - max(1, int(lookback_days)) * 86400
    candidates: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("ie_key") == "YoutubeTab":
            continue
        title = _clean_text_block(entry.get("title", ""))
        if not title:
            continue
        timestamp = entry.get("timestamp")
        upload_date = entry.get("upload_date")
        if isinstance(timestamp, (int, float)) and timestamp < cutoff:
            continue
        if isinstance(upload_date, str) and re.fullmatch(r"\d{8}", upload_date):
            try:
                dt = datetime.strptime(upload_date, "%Y%m%d").timestamp()
                if dt < cutoff:
                    continue
            except Exception:
                pass
        candidates.append(entry)
    if not candidates:
        raise RuntimeError("No usable YouTube entries found")
    used_source_keys = used_source_keys or set()
    chosen = None
    if (selection_strategy or "latest").strip().lower() == "random":
        shuffled = candidates[:]
        random.shuffle(shuffled)
        for entry in shuffled:
            vid = entry.get("id") or entry.get("url") or entry.get("webpage_url") or ""
            url = vid if isinstance(vid, str) and vid.startswith("http") else f"https://www.youtube.com/watch?v={vid}" if vid else ""
            if url and _canonical_source_key("youtube_video", url) not in used_source_keys:
                chosen = entry
                break
    else:
        for entry in candidates:
            vid = entry.get("id") or entry.get("url") or entry.get("webpage_url") or ""
            url = vid if isinstance(vid, str) and vid.startswith("http") else f"https://www.youtube.com/watch?v={vid}" if vid else ""
            if url and _canonical_source_key("youtube_video", url) not in used_source_keys:
                chosen = entry
                break
    if chosen is None:
        raise RuntimeError("No unused YouTube entries found")
    vid = chosen.get("id") or chosen.get("url") or chosen.get("webpage_url") or ""
    if isinstance(vid, str) and vid.startswith("http"):
        return vid
    if isinstance(vid, str) and vid:
        return f"https://www.youtube.com/watch?v={vid}"
    raise RuntimeError("Could not resolve a YouTube video URL from the collection")


def _fetch_youtube_context(source_value: str) -> SourceContext:
    info, audio_path, transcript, video_id = _download_youtube_assets(source_value)
    title = _clean_text_block(info.get("title", "")) or f"YouTube video {video_id}"
    channel = _clean_text_block(info.get("channel") or info.get("uploader") or "")
    description = _clean_text_block(info.get("description", ""))
    duration = info.get("duration")
    webpage_url = info.get("webpage_url") or source_value

    source_parts = [
        f"Video title: {title}",
        f"Channel: {channel}" if channel else "",
        f"Duration: {int(duration)} seconds" if isinstance(duration, (int, float)) else "",
        f"URL: {webpage_url}",
        "",
        "Video description:",
        description or "[No description available.]",
    ]
    if transcript:
        source_parts.extend([
            "",
            "Transcript / captions:",
            transcript,
        ])
    if audio_path:
        source_parts.extend([
            "",
            f"Cached audio file: {audio_path}",
        ])

    return SourceContext(
        source_type="youtube",
        source_value=source_value,
        title=title,
        topic=title,
        body=transcript or description,
        transcript=transcript,
        source_material="\n".join(part for part in source_parts if part is not None),
        format_instructions=(
            "Use the video's transcript and metadata as source material. "
            "If the transcript is sparse or missing, ground the segment in the title, description, and audio-derived notes. "
            "Do not invent factual details that are not supported by the source."
        ),
        channel=channel,
        duration_seconds=float(duration) if isinstance(duration, (int, float)) else None,
        audio_path=str(audio_path) if audio_path else "",
        transcript_source="captions" if transcript else "",
    )


def _normalize_reddit_story_text(text: str) -> str:
    """Remove Reddit/Markdown noise while keeping the post content intact."""
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return ""

    # Turn markdown links into plain text labels.
    text = re.sub(r"\[([^\]]+)\]\((?:https?://|/)[^)]+\)", r"\1", text)
    # Drop fenced code blocks and inline code markers that TTS reads awkwardly.
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = text.replace("`", "")
    # Strip common Reddit quote and list markers from the start of lines.
    text = re.sub(r"(?m)^\s*>\s?", "", text)
    text = re.sub(r"(?m)^\s*[-*+]\s+", "", text)
    # Normalize repeated whitespace while keeping paragraph breaks.
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return _normalize_reddit_user_mentions(text.strip())


AUTO_SOURCE_SHOW_TYPES = {
    "hybrid",
    "content_ingest",
    "live_community",
    "news_current_events",
    "listener_driven",
}


def _normalize_source_rule(rule: dict | None) -> dict[str, str | int]:
    rule = rule or {}
    rule_type = str(rule.get("type") or rule.get("source_type") or "url").strip().lower() or "url"
    value = str(
        rule.get("value")
        or rule.get("url")
        or rule.get("source_value")
        or rule.get("description")
        or ""
    ).strip()
    try:
        lookback_days = max(1, int(rule.get("lookback_days", 7)))
    except Exception:
        lookback_days = 7
    selection_strategy = str(rule.get("selection_strategy") or rule.get("strategy") or "latest").strip().lower() or "latest"
    segment_type = str(rule.get("segment_type") or "").strip()
    return {
        "type": rule_type,
        "value": value,
        "lookback_days": lookback_days,
        "selection_strategy": selection_strategy,
        "segment_type": segment_type,
    }


def _auto_source_rule_for_show(show, preferred_segment_type: str | None = None) -> dict | None:
    show_type = getattr(show, "show_type", "research")
    if show_type not in AUTO_SOURCE_SHOW_TYPES:
        return None
    rules = [_normalize_source_rule(rule) for rule in getattr(show, "source_rules", []) if isinstance(rule, dict)]
    rules = [rule for rule in rules if rule["value"]]
    if not rules:
        return None

    if preferred_segment_type and preferred_segment_type != "random":
        matching = [rule for rule in rules if rule["segment_type"] == preferred_segment_type]
        if matching:
            rules = matching

    return rules[0]


def load_source_context(
    source_type: str,
    source_value: str,
    lookback_days: int = 7,
    selection_strategy: str = "latest",
    used_source_keys: set[str] | None = None,
) -> SourceContext | None:
    source_type = (source_type or "").strip().lower()
    source_value = (source_value or "").strip()
    if not source_type or not source_value:
        return None
    source_key = _canonical_source_key(source_type, source_value)
    if used_source_keys and source_key and source_key in used_source_keys:
        return None
    try:
        if source_type in {"reddit", "reddit_thread"}:
            kind, _ = _normalize_reddit_source(source_value)
            return (
                _fetch_reddit_subreddit_context_with_strategy(source_value, lookback_days, selection_strategy, used_source_keys)
                if kind == "subreddit"
                else _fetch_reddit_thread_context(source_value)
            )
        if source_type == "reddit_subreddit":
            return _fetch_reddit_subreddit_context_with_strategy(source_value, lookback_days, selection_strategy, used_source_keys)
        if source_type in {"youtube", "youtube_video"}:
            if _is_youtube_collection_source(source_value):
                selected_url = _select_youtube_video_url_from_collection(source_value, lookback_days, selection_strategy, used_source_keys)
                return _fetch_youtube_context(selected_url)
            if used_source_keys and _canonical_source_key("youtube_video", source_value) in used_source_keys:
                return None
            return _fetch_youtube_context(source_value)
        if source_type in {"youtube_channel", "youtube_playlist"}:
            selected_url = _select_youtube_video_url_from_collection(source_value, lookback_days, selection_strategy, used_source_keys)
            return _fetch_youtube_context(selected_url)
        if source_type == "url":
            if used_source_keys and _canonical_source_key("url", source_value) in used_source_keys:
                return None
            title, body = _fetch_web_source(source_value)
            if not body:
                return None
            return SourceContext(
                source_type="url",
                source_value=source_value,
                title=title or source_value,
                topic=title or source_value,
                body=body,
                source_material=f"Source URL: {source_value}\nTitle: {title or source_value}\n\n{body}",
                format_instructions=(
                    "Use the source material as reporting context. Do not read it verbatim; translate it into natural radio language."
                ),
            )
    except Exception as exc:
        log(f"Failed to load source '{source_type}' from '{source_value}': {exc}")
    return None


def _host_label(host_id: str) -> str:
    try:
        return get_host(host_id).get("name", host_id)
    except Exception:
        return host_id.replace("_", " ").title()


def _primary_host_assignment(show) -> dict:
    for host in show.hosts:
        if host.get("role") == "primary":
            return host
    return show.hosts[0] if show.hosts else {
        "id": show.host,
        "role": "primary",
        "tts_backend": getattr(show, "tts_backend", "kokoro"),
        "voice_kokoro": show.voices.get("host", "am_michael"),
        "voice_minimax": "Deep_Voice_Man",
        "voice_google": "Kore",
    }


def _secondary_host_assignment(show, primary: dict | None = None) -> dict | None:
    primary = primary or _primary_host_assignment(show)
    for host in show.hosts:
        if host is primary:
            continue
        if host.get("role") in {"co-host", "secondary", "guest", "call-in"}:
            return host
    for host in show.hosts:
        if host is not primary:
            return host
    return None


def _uses_secondary_host_dialogue(show, segment_type: str) -> bool:
    return _secondary_host_assignment(show) is not None


def _selected_guest(show) -> dict:
    env_name = os.environ.get("WRIT_GUEST_NAME", "").strip()
    if env_name:
        return {
            "name": env_name,
            "context": os.environ.get("WRIT_GUEST_CONTEXT", "").strip(),
            "tts_backend": os.environ.get("WRIT_GUEST_TTS_BACKEND", "kokoro"),
            "voice_kokoro": os.environ.get("WRIT_GUEST_VOICE_KOKORO", "af_bella"),
            "voice_minimax": os.environ.get("WRIT_GUEST_VOICE_MINIMAX", "Wise_Woman"),
            "voice_google": os.environ.get("WRIT_GUEST_VOICE_GOOGLE", "Puck"),
        }
    if getattr(show, "guests", None):
        guest = random.choice(show.guests)
        return {
            "name": guest.get("name", "a guest from the station orbit"),
            "context": guest.get("expertise", ""),
            "tts_backend": guest.get("tts_backend", "kokoro"),
            "voice_kokoro": guest.get("voice_kokoro", "af_bella"),
            "voice_minimax": guest.get("voice_minimax", "Wise_Woman"),
            "voice_google": guest.get("voice_google", "Puck"),
        }
    return random.choice(INTERVIEW_GUESTS)


def _voice_for_assignment(assignment: dict | None, backend: str, fallback: str) -> str:
    if not assignment:
        return fallback
    host_id = assignment.get("id", "")
    roster_host = None
    if host_id:
        try:
            roster_host = get_host(host_id)
        except Exception:
            roster_host = None
    if backend == "minimax":
        return assignment.get("voice_minimax") or (roster_host or {}).get("voice_minimax") or fallback
    if backend == "google":
        return assignment.get("voice_google") or (roster_host or {}).get("voice_google") or fallback
    return assignment.get("voice_kokoro") or (roster_host or {}).get("tts_voice") or fallback


def _pace_wpm_for_assignment(assignment: dict | None, backend: str = "kokoro", fallback_wpm: int = 130) -> int:
    if not assignment:
        return fallback_wpm
    backend_key = f"speaking_pace_wpm_{backend}"
    if assignment.get(backend_key):
        try:
            return int(assignment[backend_key])
        except (TypeError, ValueError):
            pass
    if assignment.get("speaking_pace_wpm"):
        try:
            return int(assignment["speaking_pace_wpm"])
        except (TypeError, ValueError):
            pass
    host_id = assignment.get("id", "")
    if host_id:
        try:
            host = get_host(host_id)
            pace = host.get(backend_key)
            if pace:
                return int(pace)
            pace = host.get("speaking_pace_wpm")
            if pace:
                return int(pace)
        except Exception:
            pass
    return fallback_wpm


def _voice_plan(show, segment_type: str, backend: str) -> tuple[dict[str, str], dict[str, str]]:
    primary = _primary_host_assignment(show)
    secondary = _secondary_host_assignment(show, primary)
    primary_voice = _voice_for_assignment(
        primary,
        backend,
        "Deep_Voice_Man" if backend == "minimax" else "Kore" if backend == "google" else "am_michael",
    )

    override = os.environ.get("WRIT_HOST_VOICE", "").strip()
    if override:
        primary_voice = override

    labels = {"primary_host_name": _host_label(primary.get("id", show.host))}
    voices = {
        "host": primary_voice,
        "host_speed": _kokoro_speed_from_wpm(_pace_wpm_for_assignment(primary, backend)),
        "host_wpm": _pace_wpm_for_assignment(primary, backend),
    }

    if _uses_secondary_host_dialogue(show, segment_type):
        secondary_voice = _voice_for_assignment(
            secondary,
            backend,
            "Wise_Woman" if backend == "minimax" else "Puck" if backend == "google" else "af_bella",
        )
        secondary_name = None
        if secondary:
            if secondary.get("id") != primary.get("id"):
                secondary_name = _host_label(secondary.get("id"))
            elif secondary.get("role") in {"guest", "call-in"}:
                secondary_name = "a guest voice from the station orbit"
        labels["secondary_host_name"] = secondary_name or "a trusted second voice from the station"
        voices["guest"] = secondary_voice
        voices["guest_speed"] = _kokoro_speed_from_wpm(_pace_wpm_for_assignment(secondary, backend))
        voices["guest_wpm"] = _pace_wpm_for_assignment(secondary, backend)
        return labels, voices

    if segment_type == "interview":
        guest = _selected_guest(show)
        labels["guest_name"] = guest.get("name", "a guest from the station orbit")
        if guest.get("context"):
            labels["guest_context"] = guest["context"]
        voices["guest"] = _voice_for_assignment(
            guest,
            backend,
            "Wise_Woman" if backend == "minimax" else "Puck" if backend == "google" else "af_bella",
        )
        voices["guest_speed"] = _kokoro_speed_from_wpm(_pace_wpm_for_assignment(guest, backend))
        voices["guest_wpm"] = _pace_wpm_for_assignment(guest, backend)
        return labels, voices

    return labels, voices


def _two_host_prompt_prefix(show, segment_type: str, speaker_labels: dict[str, str], source_context: SourceContext | None = None) -> str:
    primary_name = speaker_labels.get("primary_host_name", "the primary host")
    secondary_name = speaker_labels.get("secondary_host_name", "the co-host")
    instructions = [
        f"Write this as a two-host radio conversation between {primary_name} and {secondary_name}.",
        "Format every spoken line with HOST_A: or HOST_B: markers.",
        "Give both hosts distinct reactions, questions, and points of emphasis.",
        "Keep the segment's original purpose, structure, and factual grounding intact.",
        "Do not collapse the script back into a monologue.",
    ]
    if segment_type == "reddit_storytelling":
        instructions.append(
            "Stay close to the original Reddit post, but let the two hosts share the narration and reaction instead of reading it as a single uninterrupted monologue."
        )
    elif segment_type == "youtube":
        instructions.append(
            "Treat the source video as material the hosts are unpacking together, not raw audio to replay verbatim."
        )
    elif segment_type == "station_id":
        instructions.append(
            "Use exactly two very short lines, one from each host, totaling roughly 18-28 words."
        )
        instructions.append(
            "Keep the exchange extremely tight and on-air friendly so it still works as a station ID."
        )
    elif segment_type in {"show_intro", "show_outro"}:
        instructions.append(
            "Use a concise back-and-forth with one to three short turns per host, not a long conversation."
        )
    elif source_context and source_context.source_type == "reddit":
        instructions.append(
            "Let the hosts react differently to the post and comments, and build toward a shared view."
        )
    return "\n".join(instructions)


def _fallback_two_host_script(show, segment_type: str, speaker_labels: dict[str, str]) -> str | None:
    primary_name = speaker_labels.get("primary_host_name", _host_label(show.host))
    secondary_name = speaker_labels.get("secondary_host_name", "the co-host")
    if segment_type == "station_id":
        return (
            f"HOST_A: This is {STATION_NAME}, with {show.name}.\n\n"
            f"HOST_B: {primary_name} and {secondary_name}, still on the line."
        )
    return None


def _slugify_topic(value: str, max_len: int = 30) -> str:
    slug = (value or "").strip().lower()[:max_len]
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = "_".join(filter(None, slug.split("_")))
    return slug or "segment"


def _minimax_performance_instructions(segment_type: str) -> str:
    instructions = [
        "When it helps the performance, you may use MiniMax speech interjection tags sparingly.",
        "Allowed tags: (laughs), (chuckle), (coughs), (clear-throat), (groans), (breath), (pant), (inhale), (exhale), (gasps), (sniffs), (sighs), (snorts), (burps), (lip-smacking), (humming), (hissing), (emm), (sneezes).",
        "Use them only occasionally at natural emotional beats. Do not stack tags or overuse them.",
    ]
    if segment_type in {"panel", "interview", "reddit_post", "reddit_storytelling", "youtube"}:
        instructions.append("In conversation, let the tags appear naturally inside a speaker's line, not as standalone stage directions.")
    return "\n".join(instructions)


def build_generation_prompt(
    show,
    segment_type: str,
    topic: str,
    speaker_labels: dict[str, str],
    backend: str = "kokoro",
    station_name: str = STATION_NAME,
    source_context: SourceContext | None = None,
) -> str:
    """Build the full prompt for content generation."""
    show_context = {
        "show_name": show.name,
        "show_description": show.description,
        "topic_focus": show.topic_focus,
        "segment_type": segment_type,
        "station_name": station_name,
    }
    primary = _primary_host_assignment(show)
    base = build_host_prompt(primary.get("id", show.host), show_context)

    min_words, max_words = segment_word_targets(segment_type)
    config = get_segment_type_definition(segment_type)
    prompt_template = config.get("prompt_template", DEFAULT_SEGMENT_TYPES["deep_dive"]["prompt_template"])

    special_context = {
        "show_name": show.name,
        "show_description": show.description,
        "topic_focus": show.topic_focus,
        "station_name": station_name,
        "primary_host_name": speaker_labels.get("primary_host_name", _host_label(show.host)),
        "secondary_host_name": speaker_labels.get("secondary_host_name", "a second voice"),
        "guest_name": speaker_labels.get("guest_name", "a guest from the station orbit"),
        "guest_context": speaker_labels.get("guest_context", ""),
        "headlines": "",
        "source_title": source_context.title if source_context else "",
        "source_subreddit": f"r/{source_context.subreddit}" if source_context and source_context.subreddit else "",
        "source_instructions": source_context.format_instructions if source_context else "",
    }

    if source_context and source_context.source_type in {"reddit", "youtube"}:
        topic = source_context.title or topic

    if segment_type == "news_analysis":
        headlines = fetch_headlines()
        special_context["headlines"] = (
            format_headlines(headlines)
            if headlines else
            "No headlines available - discuss the nature of news itself."
        )
    elif segment_type == "interview":
        guest_context = speaker_labels.get("guest_context")
        if guest_context:
            topic = f"{topic} (Guest context: {guest_context})"

    try:
        prompt_template = prompt_template.format(**special_context)
    except KeyError as exc:
        missing = exc.args[0]
        log(f"Segment template '{segment_type}' referenced missing variable '{missing}'")

    if _uses_secondary_host_dialogue(show, segment_type):
        prompt_template = (
            f"{_two_host_prompt_prefix(show, segment_type, special_context, source_context)}\n\n"
            f"{prompt_template}"
        )

    # For long-form segments, add a hard length reminder at the end of the prompt
    is_long_form = min_words >= 500
    length_reminder = (
        f"\n\nLENGTH REQUIREMENT: You MUST write at least {min_words} words. "
        f"Do not summarise, do not wrap up early. "
        f"Keep developing ideas, adding examples, and exploring tangents until you have reached the minimum. "
        f"A response under {min_words} words is incomplete and will be rejected."
    ) if is_long_form else ""
    if (backend or "").strip().lower() == "minimax":
        length_reminder += f"\n\n{_minimax_performance_instructions(segment_type)}"

    source_block = ""
    if source_context and source_context.source_material:
        if source_context.source_type == "reddit" and segment_type == "reddit_storytelling":
            original_post = source_context.source_material.split("\n\nSelected comments:", 1)[0].strip()
            source_block = (
                f"\n\nSOURCE TYPE: {source_context.source_type}\n"
                f"SOURCE VALUE: {source_context.source_value}\n"
                f"SOURCE MATERIAL:\n{original_post}\n"
                f"\nSOURCE-SPECIFIC INSTRUCTIONS:\nRead the post closely. Do not summarize or discuss comments.\n"
            )
        elif source_context.source_type == "youtube" and segment_type == "youtube":
            source_block = (
                f"\n\nSOURCE TYPE: {source_context.source_type}\n"
                f"SOURCE VALUE: {source_context.source_value}\n"
                f"SOURCE MATERIAL:\n{source_context.source_material}\n"
                f"\nSOURCE-SPECIFIC INSTRUCTIONS:\nUse the transcript and audio-derived notes as primary evidence. "
                f"If the transcript is thin, lean on the title, description, and channel metadata. "
                f"Do not invent facts that are not supported by the source.\n"
            )
        else:
            source_block = (
                f"\n\nSOURCE TYPE: {source_context.source_type}\n"
                f"SOURCE VALUE: {source_context.source_value}\n"
                f"SOURCE MATERIAL:\n{source_context.source_material}\n"
            )
            if source_context.format_instructions:
                source_block += f"\nSOURCE-SPECIFIC INSTRUCTIONS:\n{source_context.format_instructions}\n"

    if segment_type == "station_id":
        prompt = f"""{base}

SEGMENT: {segment_type}
TARGET LENGTH: {min_words}-{max_words} words

{prompt_template}{source_block}{length_reminder}"""
    elif topic:
        prompt = f"""{base}

SEGMENT: {segment_type}
TOPIC: {topic}
TARGET LENGTH: {min_words}-{max_words} words

{prompt_template}{source_block}{length_reminder}"""
    else:
        prompt = f"""{base}

SEGMENT: {segment_type}
TARGET LENGTH: {min_words}-{max_words} words

{prompt_template}{source_block}{length_reminder}"""

    return prompt


def run_generation(prompt: str, segment_type: str) -> str | None:
    """Run LLM to generate the script."""
    min_words, max_words = segment_word_targets(segment_type)
    timeout = 120 if max_words < 200 else 300
    min_acceptable = int(min_words * 0.8)
    temperature = 0.8
    num_predict = 8192
    multi_host_prompt = "HOST_A:" in prompt or "HOST_B:" in prompt

    if segment_type in {"station_id", "show_intro", "show_outro"}:
        timeout = min(timeout, 60)
    if segment_type == "station_id":
        temperature = 0.25 if multi_host_prompt else 0.15
        num_predict = 160 if multi_host_prompt else 96
    elif segment_type in {"show_intro", "show_outro"}:
        temperature = 0.4 if multi_host_prompt else 0.35
        num_predict = 384 if multi_host_prompt else 256

    script = run_claude(
        prompt,
        timeout=timeout,
        temperature=temperature,
        num_predict=num_predict,
    )
    if not script:
        return None

    # If the model undershoots the nominal minimum, try one corrective pass
    # before falling back to the looser 80% acceptance floor.
    word_count = len(script.split())
    if word_count < min_words:
        repaired_prompt = f"""{prompt}

The draft below is too short for this segment.
Target length: {min_words}-{max_words} words.
Current length: {word_count} words.

Revise the full script so it reaches at least {min_words} words.
Preserve the same topic, structure, tone, and voice.
Do not add commentary about the revision.
Do not summarize the draft.
Output ONLY the revised spoken text.

CURRENT DRAFT:
{script}
"""
        repaired_script = run_claude(
            repaired_prompt,
            timeout=timeout,
            temperature=max(0.35, temperature - 0.15),
            num_predict=num_predict,
        )
        if repaired_script:
            repaired_word_count = len(repaired_script.split())
            if repaired_word_count > word_count:
                log(
                    "Script expanded from "
                    f"{word_count} to {repaired_word_count} words "
                    f"(target {min_words}-{max_words})"
                )
                script = repaired_script
                word_count = repaired_word_count

    # Quality gate: keep the existing 80% floor, but reject anything too short
    # before it reaches TTS.
    if word_count < min_acceptable:
        log(f"Script too short: {word_count} words (need {min_acceptable}+, target {min_words}-{max_words})")
        return None

    return script


# =============================================================================
# TTS RENDERING
# =============================================================================


try:
    from kokoro import KPipeline
    import soundfile as sf
    import numpy as np
    KOKORO_AVAILABLE = True
except ImportError:
    KOKORO_AVAILABLE = False

_KOKORO_PIPELINE = None
_DIALOGUE_MARKER_PATTERN = (
    r"((?:HOST|GUEST|HOST_A|HOST_B|HOSTA|HOSTB|[A-Z][A-Z\s.]+)"
    r"(?:\s*\([^)]+\))?:)"
)
_DIALOGUE_SPEAKER_PATTERN = re.compile(
    r"^(?P<speaker>(?:HOST|GUEST|HOST_A|HOST_B|HOSTA|HOSTB|[A-Z][A-Z\s.]+))"
    r"(?:\s*\([^)]+\))?:$"
)

def get_kokoro_pipeline():
    global _KOKORO_PIPELINE
    if _KOKORO_PIPELINE is None:
        if not KOKORO_AVAILABLE:
            log("Kokoro not available in current environment")
            return None
        log("Loading Kokoro pipeline...")
        try:
            # Use CUDA for GPU acceleration
            _KOKORO_PIPELINE = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M", device="cuda")
        except Exception as e:
            log(f"Failed to load Kokoro: {e}")
            return None
    return _KOKORO_PIPELINE


def _normalize_dialogue_speaker(marker: str) -> str | None:
    match = _DIALOGUE_SPEAKER_PATTERN.match((marker or "").strip())
    if not match:
        return None
    return match.group("speaker").strip()


def _kokoro_speed_from_wpm(wpm: int | float | None, baseline_wpm: float = 130.0) -> float:
    try:
        value = float(wpm)
    except (TypeError, ValueError):
        value = baseline_wpm
    if value <= 0:
        value = baseline_wpm
    return max(0.75, min(1.35, value / baseline_wpm))

def _parse_dialogue_parts(script: str) -> list[tuple[str, str]]:
    segments = re.split(_DIALOGUE_MARKER_PATTERN, script)
    parts: list[tuple[str, str]] = []
    current_speaker = None
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        normalized = _normalize_dialogue_speaker(seg)
        if normalized:
            current_speaker = normalized
        elif current_speaker:
            parts.append((current_speaker, seg))
        else:
            parts.append(("HOST", seg))
    return parts


def _split_tts_text(text: str, max_chars: int = 900) -> list[str]:
    text = text.strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""

    def flush_current():
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
        current = ""

    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            flush_current()
            sentences = re.split(r"(?<=[.!?])\s+", paragraph)
            sent_buf = ""
            for sentence in sentences:
                sentence = sentence.strip()
                if not sentence:
                    continue
                candidate = f"{sent_buf} {sentence}".strip() if sent_buf else sentence
                if len(candidate) <= max_chars:
                    sent_buf = candidate
                else:
                    if sent_buf:
                        chunks.append(sent_buf)
                        sent_buf = ""
                    if len(sentence) <= max_chars:
                        sent_buf = sentence
                    else:
                        for i in range(0, len(sentence), max_chars):
                            chunks.append(sentence[i:i + max_chars].strip())
            if sent_buf:
                chunks.append(sent_buf)
            continue

        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= max_chars:
            current = candidate
        else:
            flush_current()
            current = paragraph

    flush_current()
    return [c for c in chunks if c.strip()]


def render_single_voice(
    script: str,
    output_path: Path,
    voice: str,
    backend: str = "kokoro",
    speed: float = 1.0,
    wpm: int | None = None,
) -> bool:
    """Render a single-voice script to audio."""
    if backend == "minimax":
        try:
            from minimax_tts import generate_speech
        except Exception as e:
            log(f"  MiniMax import error: {e}")
            return False
        log(f"  Rendering single MiniMax voice '{voice}' to {output_path.name}...")
        try:
            success = generate_speech(script, output_path, voice_id=voice)
            if success:
                log("  Finished MiniMax render.")
            return success
        except Exception as e:
            log(f"  MiniMax rendering error: {e}")
            return False
    if backend == "google":
        try:
            from google_tts import generate_speech
        except Exception as e:
            log(f"  Google TTS import error: {e}")
            return False
        log(f"  Rendering single Google voice '{voice}' to {output_path.name}...")
        try:
            success = generate_speech(
                script,
                output_path,
                voice_id=voice,
                wpm=wpm or int(round(130 * speed)),
            )
            if success:
                log("  Finished Google render.")
            return success
        except Exception as e:
            log(f"  Google rendering error: {e}")
            return False

    pipe = get_kokoro_pipeline()
    if not pipe:
        return False

    if len(script) > 1400 or "\n\n" in script:
        return render_single_voice_chunked(script, output_path, voice, backend=backend, speed=speed)

    log(f"  Rendering single voice '{voice}' to {output_path.name}...")
    try:
        generator = pipe(script, voice=voice, speed=speed)
        
        with sf.SoundFile(str(output_path), mode='w', samplerate=24000, channels=1) as f:
            chunk_count = 0
            for _, _, audio in generator:
                if audio is not None:
                    f.write(audio)
                    chunk_count += 1
                    if chunk_count % 10 == 0:
                        log(f"    ...rendered {chunk_count} segments")
                        
        if chunk_count > 0:
            log(f"  Finished rendering {chunk_count} segments.")
            return True
        else:
            log("  No audio generated.")
            return False
            
    except Exception as e:
        log(f"  Kokoro rendering error: {e}")
        return False


def render_single_voice_chunked(
    script: str,
    output_path: Path,
    voice: str,
    backend: str = "kokoro",
    speed: float = 1.0,
) -> bool:
    """Render a long single-voice script in smaller pieces."""
    if backend != "kokoro":
        return render_single_voice(script, output_path, voice, backend=backend, speed=speed)

    pipe = get_kokoro_pipeline()
    if not pipe:
        return False

    parts = _split_tts_text(script)
    if not parts:
        return False

    log(f"  Rendering {len(parts)} text chunks to {output_path.name}...")
    gap_audio = np.zeros(int(24000 * 0.25), dtype=np.float32)

    try:
        with sf.SoundFile(str(output_path), mode="w", samplerate=24000, channels=1) as f:
            total_chunks = 0
            for i, part in enumerate(parts):
                text = preprocess_for_tts(part, backend=backend)
                if not text.strip():
                    continue
                if i > 0:
                    f.write(gap_audio)
                generator = pipe(text, voice=voice, speed=speed)
                for _, _, audio in generator:
                    if audio is not None:
                        f.write(audio)
                        total_chunks += 1
                        if total_chunks % 10 == 0:
                            log(f"    ...rendered {total_chunks} segments")
        if total_chunks > 0:
            log(f"  Finished rendering {len(parts)} chunks.")
            return True
        log("  No audio generated from chunked render.")
        return False
    except Exception as e:
        log(f"  Kokoro chunked rendering error: {e}")
        return False


def render_multi_voice(script: str, output_path: Path, voices: dict[str, str], backend: str = "kokoro") -> bool:
    """Render a multi-voice script (panel/interview) to audio."""
    if backend == "minimax":
        try:
            from minimax_tts import generate_speech
        except Exception as e:
            log(f"  MiniMax import error: {e}")
            return False

        parts = _parse_dialogue_parts(script)
        host_voice = voices.get("host", "Deep_Voice_Man")
        guest_voice = voices.get("guest", "Wise_Woman")
        host_speed = float(voices.get("host_speed", 1.0))
        guest_speed = float(voices.get("guest_speed", 1.0))
        if not parts:
            flattened = re.sub(
                r'^(?:HOST|GUEST|HOST_A|HOST_B|HOSTA|HOSTB|[A-Z][A-Z\s.]+)(?:\s*\([^)]+\))?:\s*',
                '',
                script,
                flags=re.MULTILINE,
            )
            return render_single_voice(
                flattened,
                output_path,
                host_voice,
                backend="minimax",
                speed=host_speed,
            )

        voice_map = {}
        for key in ("HOST", "HOST_A", "HOSTA"):
            voice_map[key] = host_voice
        for key in ("GUEST", "HOST_B", "HOSTB"):
            voice_map[key] = guest_voice
        speed_map = {}
        for key in ("HOST", "HOST_A", "HOSTA"):
            speed_map[key] = host_speed
        for key in ("GUEST", "HOST_B", "HOSTB"):
            speed_map[key] = guest_speed

        log(f"  Rendering {len(parts)} MiniMax dialogue segments to {output_path.name}...")
        try:
            with tempfile.TemporaryDirectory(prefix="writ_minimax_dialogue_") as tmpdir:
                tmp = Path(tmpdir)
                concat_entries: list[Path] = []
                silence_path = tmp / "gap.mp3"
                silence_cmd = [
                    "ffmpeg", "-y",
                    "-f", "lavfi",
                    "-i", "anullsrc=r=32000:cl=mono",
                    "-t", "0.3",
                    "-q:a", "9",
                    str(silence_path),
                ]
                silence_proc = subprocess.run(silence_cmd, capture_output=True, text=True)
                if silence_proc.returncode != 0:
                    log(f"  Failed to create MiniMax silence gap: {(silence_proc.stderr or '').strip()[:200]}")
                    return False

                rendered_parts = 0
                for i, (speaker, text) in enumerate(parts):
                    voice = voice_map.get(speaker, host_voice)
                    speed = speed_map.get(speaker, host_speed)
                    text = preprocess_for_tts(text, backend=backend)
                    if not text.strip():
                        continue
                    if rendered_parts > 0:
                        concat_entries.append(silence_path)
                    segment_path = tmp / f"segment_{i:03d}.mp3"
                    success = generate_speech(
                        text,
                        segment_path,
                        voice_id=voice,
                        speed=speed,
                    )
                    if not success or not segment_path.exists():
                        log(f"  MiniMax rendering failed for dialogue segment {i + 1}")
                        return False
                    concat_entries.append(segment_path)
                    rendered_parts += 1

                if not concat_entries:
                    log("  No dialogue parts rendered")
                    return False

                concat_list = tmp / "concat.txt"
                concat_list.write_text(
                    "".join(f"file '{path.as_posix()}'\n" for path in concat_entries),
                    encoding="utf-8",
                )
                concat_cmd = [
                    "ffmpeg", "-y",
                    "-f", "concat",
                    "-safe", "0",
                    "-i", str(concat_list),
                    "-c", "copy",
                    str(output_path),
                ]
                concat_proc = subprocess.run(concat_cmd, capture_output=True, text=True)
                if concat_proc.returncode != 0:
                    log(f"  MiniMax concat failed: {(concat_proc.stderr or '').strip()[:200]}")
                    return False
                log(f"  Finished rendering {rendered_parts} MiniMax dialogue segments.")
                return True
        except Exception as e:
            log(f"  MiniMax multi-voice rendering error: {e}")
            return False
    if backend == "google":
        try:
            from google_tts import generate_speech
        except Exception as e:
            log(f"  Google TTS import error: {e}")
            return False

        parts = _parse_dialogue_parts(script)
        host_voice = voices.get("host", "Kore")
        guest_voice = voices.get("guest", "Puck")
        if not parts:
            flattened = re.sub(
                r'^(?:HOST|GUEST|HOST_A|HOST_B|HOSTA|HOSTB|[A-Z][A-Z\s.]+)(?:\s*\([^)]+\))?:\s*',
                '',
                script,
                flags=re.MULTILINE,
            )
            return render_single_voice(
                flattened,
                output_path,
                host_voice,
                backend="google",
                wpm=int(voices.get("host_wpm", 130)),
            )

        voice_map = {}
        for key in ("HOST", "HOST_A", "HOSTA"):
            voice_map[key] = host_voice
        for key in ("GUEST", "HOST_B", "HOSTB"):
            voice_map[key] = guest_voice

        log(f"  Rendering {len(parts)} Google dialogue segments to {output_path.name}...")
        try:
            with tempfile.TemporaryDirectory(prefix="writ_google_dialogue_") as tmpdir:
                tmp = Path(tmpdir)
                concat_entries: list[Path] = []
                silence_path = tmp / "gap.wav"
                silence_cmd = [
                    "ffmpeg", "-y",
                    "-f", "lavfi",
                    "-i", "anullsrc=r=24000:cl=mono",
                    "-t", "0.3",
                    "-c:a", "pcm_s16le",
                    str(silence_path),
                ]
                silence_proc = subprocess.run(silence_cmd, capture_output=True, text=True)
                if silence_proc.returncode != 0:
                    log(f"  Failed to create Google silence gap: {(silence_proc.stderr or '').strip()[:200]}")
                    return False

                rendered_parts = 0
                for i, (speaker, text) in enumerate(parts):
                    voice = voice_map.get(speaker, host_voice)
                    text = preprocess_for_tts(text, backend=backend)
                    if not text.strip():
                        continue
                    if rendered_parts > 0:
                        concat_entries.append(silence_path)
                    segment_path = tmp / f"segment_{i:03d}.wav"
                    current_wpm = int(voices.get("host_wpm", 130)) if speaker in {"HOST", "HOST_A", "HOSTA"} else int(voices.get("guest_wpm", 130))
                    success = generate_speech(
                        text,
                        segment_path,
                        voice_id=voice,
                        wpm=current_wpm,
                    )
                    if not success or not segment_path.exists():
                        log(f"  Google rendering failed for dialogue segment {i + 1}")
                        return False
                    concat_entries.append(segment_path)
                    rendered_parts += 1

                if not concat_entries:
                    log("  No dialogue parts rendered")
                    return False

                concat_list = tmp / "concat.txt"
                concat_list.write_text(
                    "".join(f"file '{path.as_posix()}'\n" for path in concat_entries),
                    encoding="utf-8",
                )
                concat_cmd = [
                    "ffmpeg", "-y",
                    "-f", "concat",
                    "-safe", "0",
                    "-i", str(concat_list),
                    "-c", "copy",
                    str(output_path),
                ]
                concat_proc = subprocess.run(concat_cmd, capture_output=True, text=True)
                if concat_proc.returncode != 0:
                    log(f"  Google concat failed: {(concat_proc.stderr or '').strip()[:200]}")
                    return False
                log(f"  Finished rendering {rendered_parts} Google dialogue segments.")
                return True
        except Exception as e:
            log(f"  Google multi-voice rendering error: {e}")
            return False

    pipe = get_kokoro_pipeline()
    if not pipe:
        return False

    parts = _parse_dialogue_parts(script)

    host_voice = voices.get("host", "am_michael")
    host_speed = float(voices.get("host_speed", 1.0))
    if not parts:
        return render_single_voice(script, output_path, host_voice, backend=backend, speed=host_speed)

    guest_voice = voices.get("guest", "af_bella")
    guest_speed = float(voices.get("guest_speed", 1.0))
    voice_map = {}
    for key in ("HOST", "HOST_A", "HOSTA"):
        voice_map[key] = host_voice
    for key in ("GUEST", "HOST_B", "HOSTB"):
        voice_map[key] = guest_voice
    speed_map = {}
    for key in ("HOST", "HOST_A", "HOSTA"):
        speed_map[key] = host_speed
    for key in ("GUEST", "HOST_B", "HOSTB"):
        speed_map[key] = guest_speed

    log(f"  Rendering {len(parts)} dialogue segments to {output_path.name}...")
    
    gap_audio = np.zeros(int(24000 * 0.3), dtype=np.float32)

    try:
        with sf.SoundFile(str(output_path), mode='w', samplerate=24000, channels=1) as f:
            total_chunks = 0
            for i, (speaker, text) in enumerate(parts):
                voice = voice_map.get(speaker, host_voice)
                speed = speed_map.get(speaker, host_speed)
                
                text = preprocess_for_tts(text, backend=backend)
                if not text.strip():
                    continue
                
                # Add gap between speakers
                if i > 0:
                    f.write(gap_audio)
                    
                generator = pipe(text, voice=voice, speed=speed)
                for _, _, audio in generator:
                    if audio is not None:
                        f.write(audio)
                        total_chunks += 1
                        if total_chunks % 10 == 0:
                            log(f"    ...rendered {total_chunks} total segments")

        if total_chunks > 0:
            log(f"  Finished rendering {total_chunks} total segments.")
            return True
        else:
            log("  No dialogue parts rendered")
            return False
            
    except Exception as e:
        log(f"  Kokoro multi-voice rendering error: {e}")
        return False


def get_duration(filepath: Path) -> float | None:
    """Get audio duration in seconds."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(filepath)],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return None


# =============================================================================
# MAIN GENERATION PIPELINE
# =============================================================================


def generate_segment(
    show,
    segment_type: str,
    topic: str | None = None,
    source_type: str = "",
    source_value: str = "",
    source_lookback_days: int = 7,
    source_selection_strategy: str = "latest",
    source_context: SourceContext | None = None,
    used_source_keys: set[str] | None = None,
    station_name: str = STATION_NAME,
    include_topic: bool = True,
) -> Path | None:
    """Generate a single talk segment with audio."""
    show_id = show.show_id
    explicit_source_agnostic_types = {"station_id", "show_intro", "show_outro"}
    if segment_type in explicit_source_agnostic_types:
        source_type = ""
        source_value = ""
        source_context = None
    if source_context is None:
        source_context = load_source_context(
            source_type,
            source_value,
            lookback_days=source_lookback_days,
            selection_strategy=source_selection_strategy,
            used_source_keys=used_source_keys,
        )
    if source_type and source_value and source_context is None:
        log(f"  Source '{source_type}' at '{source_value}' was unavailable or already used.")
        return None
    if source_context and used_source_keys:
        selected_key = _canonical_source_key(source_context.source_type, source_context.source_value)
        if selected_key and selected_key in used_source_keys:
            log(f"  Source '{source_context.source_type}' at '{source_context.source_value}' was already used.")
            return None
    if source_context and source_context.source_type in {"reddit", "youtube"}:
        topic = source_context.title or topic
    elif source_context and not topic:
        topic = source_context.topic or source_context.title
    if segment_type == "station_id":
        topic = show.name
    if not include_topic and not topic:
        topic = ""
    if topic is None:
        topic = select_topic(show.topic_focus, segment_type)

    if source_context and source_context.source_type == "reddit" and source_context.story_mode:
        segment_type = "reddit_storytelling"
    elif source_context and source_context.source_type == "reddit" and segment_type == "random":
        segment_type = "reddit_post"
    elif source_context and source_context.source_type == "youtube" and segment_type == "random":
        segment_type = "youtube"

    min_words, max_words = segment_word_targets(segment_type)
    primary = _primary_host_assignment(show)
    backend = os.environ.get("WRIT_TTS_BACKEND", "").strip() or primary.get("tts_backend", getattr(show, "tts_backend", "kokoro"))
    long_async_enabled = os.environ.get("WRIT_MINIMAX_LONG_ASYNC", "").strip() == "1"
    backend_used = backend
    speaker_labels, voices = _voice_plan(show, segment_type, backend)

    log(f"=== Generating {segment_type} for {show.name} ===")
    log(f"  Topic: {topic[:80]}...")
    log(f"  Target: {min_words}-{max_words} words")
    log(f"  Host: {primary.get('id', show.host)} (voice: {voices.get('host', 'am_michael')})")
    if _uses_secondary_host_dialogue(show, segment_type):
        secondary = _secondary_host_assignment(show, primary)
        if secondary:
            log(f"  Co-host: {secondary.get('id', 'guest')} (voice: {voices.get('guest', 'af_bella')})")
    log(f"  TTS backend: {backend}")
    if source_context:
        log(f"  Source: {source_context.source_type} ({source_context.title or source_context.source_value})")
        if source_context.subreddit:
            log(f"  Subreddit: r/{source_context.subreddit}")
        if source_context.story_mode:
            log("  Source mode: direct read-through")
        elif source_context.source_type == "youtube":
            log(f"  Channel: {source_context.channel or 'unknown'}")
            if source_context.duration_seconds:
                log(f"  Duration: {int(source_context.duration_seconds)}s")
            if source_context.audio_path:
                log(f"  Cached audio: {source_context.audio_path}")

    script = None
    if (
        source_context
        and source_context.source_type == "reddit"
        and segment_type == "reddit_storytelling"
        and not _uses_secondary_host_dialogue(show, segment_type)
    ):
        script = _build_reddit_story_script(source_context)
        log("  Using direct Reddit story read-through; skipping LLM generation.")
    elif (
        source_context
        and source_context.source_type == "youtube"
        and segment_type == "youtube"
        and not _uses_secondary_host_dialogue(show, segment_type)
    ):
        if not source_context.audio_path:
            log("  YouTube source has no cached audio file; cannot ingest.")
            return None

        log("  Using direct YouTube audio ingest; skipping LLM and TTS.")
        source_audio = Path(source_context.audio_path)
        if not source_audio.exists():
            log(f"  Cached audio file missing: {source_audio}")
            return None

        show_dir = OUTPUT_DIR / show_id
        show_dir.mkdir(parents=True, exist_ok=True)

        timestamp = station_now().strftime("%Y%m%d_%H%M%S")
        topic_slug = _slugify_topic(topic)

        output_path = show_dir / f"{segment_type}_{topic_slug}_{timestamp}{source_audio.suffix or '.mp3'}"
        shutil.copy2(source_audio, output_path)

        script = source_context.transcript or source_context.source_material or source_context.title or ""
        word_count = len(script.split()) if script else 0
        backend_used = "youtube_ingest"
        duration = get_duration(output_path)
        duration_str = f"{int(duration // 60)}:{int(duration % 60):02d}" if duration else "?"

        SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        meta_path = SCRIPTS_DIR / f"talk_{segment_type}_{timestamp}.json"
        meta = {
            "type": segment_type,
            "show_id": show_id,
            "show_name": _show_value(show, "name", show_id),
            "host": primary.get("id", _show_value(show, "host", "")),
            "host_name": primary.get("display_name", _host_label(primary.get("id", _show_value(show, "host", "")))),
            "topic": topic,
            "script": script,
            "word_count": word_count,
            "duration_seconds": duration,
            "voices": voices,
            "voice": "" if backend_used == "youtube_ingest" else voices.get("host", ""),
            "speaker_labels": speaker_labels,
            "source_type": source_context.source_type,
            "source_value": source_context.source_value,
            "source_title": source_context.title,
            "source_subreddit": source_context.subreddit,
            "source_story_mode": bool(source_context.story_mode),
            "source_audio_path": source_context.audio_path,
            "transcript_source": source_context.transcript_source,
            "tts_backend": backend_used,
            "audio_backend": backend_used,
            "backend_origin": "local" if backend_used == "kokoro" else "cloud" if backend_used in {"minimax", "minimax_async", "google"} else "source" if backend_used == "youtube_ingest" else backend_used,
            "generated_at": station_iso_now(),
        }
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2)

        sidecar = output_path.with_suffix(".json")
        with open(sidecar, "w") as f:
            json.dump({
                "type": segment_type,
                "show_id": show_id,
                "show_name": _show_value(show, "name", show_id),
                "host": _show_value(show, "host", ""),
                "topic": topic,
                "word_count": word_count,
                "voices": voices,
                "voice": "" if backend_used == "youtube_ingest" else voices.get("host", ""),
                "voice_details": ", ".join(f"{role}: {value}" for role, value in voices.items() if value) or ("" if backend_used == "youtube_ingest" else voices.get("host", "")),
                "speaker_labels": speaker_labels,
                "source_type": source_context.source_type,
                "source_value": source_context.source_value,
                "source_title": source_context.title,
                "source_subreddit": source_context.subreddit,
                "source_story_mode": bool(source_context.story_mode),
                "source_audio_path": source_context.audio_path,
                "tts_backend": backend_used,
                "audio_backend": backend_used,
                "backend_origin": "local" if backend_used == "kokoro" else "cloud" if backend_used in {"minimax", "minimax_async", "google"} else "source" if backend_used == "youtube_ingest" else backend_used,
                "generated_at": station_iso_now(),
            }, f, indent=2)

        log(f"  Ingested audio: {output_path.name} ({duration_str})")
        return output_path
    else:
        # Build prompt and generate script
        prompt = build_generation_prompt(
            show=show,
            segment_type=segment_type,
            topic=topic,
            speaker_labels=speaker_labels,
            backend=backend,
            station_name=station_name,
            source_context=source_context,
        )

        # Try generation with up to 2 retries
        for attempt in range(3):
            script = run_generation(prompt, segment_type)
            if script:
                break
            if attempt < 2:
                log(f"  Retrying generation (attempt {attempt + 2}/3)...")
                time.sleep(3)

        if not script:
            if _uses_secondary_host_dialogue(show, segment_type):
                script = _fallback_two_host_script(show, segment_type, speaker_labels)
                if script:
                    log("  Using deterministic two-host fallback script.")
                else:
                    log("  Failed to generate script")
                    return None
            else:
                log("  Failed to generate script")
                return None

    word_count = len(script.split())
    est_minutes = word_count / 130
    log(f"  Generated {word_count} words (~{est_minutes:.1f} min)")

    # Prepare output
    show_dir = OUTPUT_DIR / show_id
    show_dir.mkdir(parents=True, exist_ok=True)

    timestamp = station_now().strftime("%Y%m%d_%H%M%S")
    topic_slug = _slugify_topic(topic)

    ext = ".mp3" if backend == "minimax" else ".wav"
    output_path = show_dir / f"{segment_type}_{topic_slug}_{timestamp}{ext}"

    # Preprocess for TTS
    processed = preprocess_for_tts(script, backend=backend)
    processed_word_count = len(processed.split())
    min_acceptable = int(min_words * 0.8)
    if processed_word_count < min_acceptable:
        log(
            "  Processed script too short for TTS: "
            f"{processed_word_count} words (need {min_acceptable}+, target {min_words}-{max_words})"
        )
        return None

    # Render audio
    log("  Rendering audio...")
    is_multi_voice = bool(get_segment_type_definition(segment_type).get("multi_voice", False))
    if not is_multi_voice and re.search(
        r"^(?:HOST|GUEST|HOST_A|HOST_B|HOSTA|HOSTB|[A-Z][A-Z\s.]+)(?:\s*\([^)]+\))?:",
        processed,
        re.MULTILINE,
    ):
        is_multi_voice = True

    use_minimax_final = backend == "minimax" and long_async_enabled and not is_multi_voice
    if backend == "minimax" and not is_multi_voice and not use_minimax_final and word_count >= 500:
        log("  Long-form MiniMax is disabled for this run; using Kokoro render only.")

    if use_minimax_final:
        validate_voice = primary.get("voice_kokoro", show.voices.get("host", "am_michael"))
        validate_path = output_path.with_suffix(".wav")
        log(f"  Kokoro validation pass first using voice '{validate_voice}'...")
        validation_ok = render_single_voice(
            processed,
            validate_path,
            validate_voice,
            backend="kokoro",
            speed=float(voices.get("host_speed", 1.0)),
            wpm=int(voices.get("host_wpm", 130)),
        )
        if not validation_ok or not validate_path.exists():
            log("  Kokoro validation failed")
            return None

        log("  Kokoro validation passed; requesting MiniMax async final render...")
        async_ok = False
        async_voice = voices.get("host", "Deep_Voice_Man")
        try:
            from minimax_tts import generate_speech_async
            async_ok = generate_speech_async(
                processed,
                output_path,
                voice_id=async_voice,
                timeout=max(120.0, float(os.environ.get("MINIMAX_TTS_ASYNC_TIMEOUT", "900"))),
            )
        except Exception as exc:
            log(f"  MiniMax async import/render error: {exc}")

        if async_ok and output_path.exists():
            validate_path.unlink(missing_ok=True)
            backend_used = "minimax_async"
            success = True
        else:
            log("  MiniMax async final render failed; keeping Kokoro-validated output.")
            output_path = validate_path
            backend_used = "kokoro_validated"
            success = True
    elif is_multi_voice:
        success = render_multi_voice(processed, output_path, voices, backend=backend)
    elif backend == "minimax" and word_count >= 500 and not long_async_enabled:
        fallback_voice = primary.get("voice_kokoro", show.voices.get("host", "am_michael"))
        log(f"  Long-form MiniMax is disabled for this run; rendering Kokoro voice '{fallback_voice}' only.")
        success = render_single_voice(
            processed,
            output_path.with_suffix(".wav"),
            fallback_voice,
            backend="kokoro",
            speed=float(voices.get("host_speed", 1.0)),
            wpm=int(voices.get("host_wpm", 130)),
        )
        if success:
            output_path = output_path.with_suffix(".wav")
            backend_used = "kokoro_longform"
    elif backend == "minimax":
        host_voice = voices.get("host", "Deep_Voice_Man")
        success = render_single_voice(
            processed,
            output_path,
            host_voice,
            backend="minimax",
            speed=float(voices.get("host_speed", 1.0)),
            wpm=int(voices.get("host_wpm", 130)),
        )
        if not success:
            fallback_voice = primary.get("voice_kokoro", show.voices.get("host", "am_michael"))
            fallback_path = output_path.with_suffix(".wav")
            log(f"  MiniMax render failed; falling back to Kokoro voice '{fallback_voice}'")
            success = render_single_voice(
                processed,
                fallback_path,
                fallback_voice,
                backend="kokoro",
                speed=float(voices.get("host_speed", 1.0)),
                wpm=int(voices.get("host_wpm", 130)),
            )
            if success:
                output_path = fallback_path
                backend_used = "kokoro_fallback"
    else:
        host_voice = voices.get("host", "am_michael")
        success = render_single_voice(
            processed,
            output_path,
            host_voice,
            backend=backend,
            speed=float(voices.get("host_speed", 1.0)),
            wpm=int(voices.get("host_wpm", 130)),
        )

    if not success or not output_path.exists():
        log("  TTS rendering failed")
        return None

    # Get duration and save metadata
    duration = get_duration(output_path)
    duration_str = f"{int(duration // 60)}:{int(duration % 60):02d}" if duration else "?"

    SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    meta_path = SCRIPTS_DIR / f"talk_{segment_type}_{timestamp}.json"
    meta = {
        "type": segment_type,
        "show_id": show_id,
        "show_name": show.name,
        "host": primary.get("id", show.host),
        "host_name": primary.get("display_name", _host_label(primary.get("id", show.host))),
        "topic": topic,
        "script": script,
        "word_count": word_count,
        "duration_seconds": duration,
        "voices": voices,
        "voice": "" if backend_used == "youtube_ingest" else voices.get("host", ""),
        "speaker_labels": speaker_labels,
        "source_type": source_context.source_type if source_context else "",
        "source_value": source_context.source_value if source_context else "",
        "source_title": source_context.title if source_context else "",
        "source_subreddit": source_context.subreddit if source_context else "",
        "source_story_mode": bool(source_context.story_mode) if source_context else False,
        "tts_backend": backend_used,
        "audio_backend": backend_used,
        "backend_origin": "local" if backend_used == "kokoro" else "cloud" if backend_used in {"minimax", "minimax_async", "google"} else "source" if backend_used == "youtube_ingest" else backend_used,
        "generated_at": station_iso_now(),
    }
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    # Write a lightweight sidecar next to the audio file for the admin library
    sidecar = output_path.with_suffix(".json")
    with open(sidecar, "w") as f:
        json.dump({
            "type": segment_type,
            "show_id": show_id,
            "show_name": _show_value(show, "name", show_id),
            "host": _show_value(show, "host", ""),
            "topic": topic,
            "word_count": word_count,
            "voices": voices,
            "voice": "" if backend_used == "youtube_ingest" else voices.get("host", ""),
            "voice_details": ", ".join(f"{role}: {value}" for role, value in voices.items() if value) or ("" if backend_used == "youtube_ingest" else voices.get("host", "")),
            "speaker_labels": speaker_labels,
            "source_type": source_context.source_type if source_context else "",
            "source_value": source_context.source_value if source_context else "",
            "source_title": source_context.title if source_context else "",
            "source_subreddit": source_context.subreddit if source_context else "",
            "source_story_mode": bool(source_context.story_mode) if source_context else False,
            "source_audio_path": source_context.audio_path if source_context else "",
            "tts_backend": backend_used,
            "audio_backend": backend_used,
            "backend_origin": "local" if backend_used == "kokoro" else "cloud" if backend_used in {"minimax", "minimax_async", "google"} else "source" if backend_used == "youtube_ingest" else backend_used,
            "generated_at": station_iso_now(),
        }, f, indent=2)

    log(f"  Created: {output_path.name} ({duration_str})")
    return output_path


def generate_for_show(
    show_id: str,
    schedule: StationSchedule,
    count: int = 3,
    segment_type: str | None = None,
    topic: str | None = None,
    source_type: str = "",
    source_value: str = "",
    source_lookback_days: int = 7,
    source_selection_strategy: str = "latest",
    include_topic: bool = True,
) -> int:
    """Generate segments for a specific show."""
    if show_id not in schedule.shows:
        log(f"Unknown show: {show_id}")
        log(f"Available: {', '.join(schedule.shows.keys())}")
        return 0

    show = schedule.shows[show_id]
    station_name = schedule.station_name
    base_source_type = source_type
    base_source_value = source_value
    base_source_lookback_days = source_lookback_days
    base_source_selection_strategy = source_selection_strategy
    requires_source = show.show_type in {"content_ingest", "news_current_events"}
    if requires_source and not (base_source_type and base_source_value) and not getattr(show, "source_rules", []):
        log(f"  No source rules configured for source-led show type '{show.show_type}'")
        return 0
    used_source_keys = _used_source_keys_for_show(show_id)
    if not include_topic:
        topic = ""

    log(f"\n{'='*60}")
    log(f"Generating {count} segments for: {show.name}")
    log(f"{'='*60}")

    success = 0
    for i in range(count):
        iter_source_type = base_source_type
        iter_source_value = base_source_value
        iter_source_lookback_days = base_source_lookback_days
        iter_source_selection_strategy = base_source_selection_strategy
        selected_source_rule = None
        selected_source_context: SourceContext | None = None
        requested_segment_type = segment_type or "random"
        auto_source = (
            not iter_source_type
            and not iter_source_value
            and requested_segment_type in {"random", "reddit_post", "reddit_storytelling", "youtube"}
        )
        if auto_source:
            selected_source_rule = _choose_source_rule_for_show(show, show_id, requested_segment_type)
            if selected_source_rule:
                selected_source_context = load_source_context(
                    selected_source_rule["type"],
                    selected_source_rule["value"],
                    lookback_days=int(selected_source_rule["lookback_days"]),
                    selection_strategy=str(selected_source_rule["selection_strategy"]),
                    used_source_keys=used_source_keys,
                )
                if selected_source_context:
                    iter_source_type = str(selected_source_rule["type"])
                    iter_source_value = str(selected_source_rule["value"])
                    iter_source_lookback_days = int(selected_source_rule["lookback_days"])
                    iter_source_selection_strategy = str(selected_source_rule["selection_strategy"])
            if not selected_source_context and requires_source:
                log(f"  No unused source items available for show '{show.name}'.")
                break

        # Pick segment type
        if segment_type:
            st = segment_type
        elif selected_source_rule and selected_source_rule.get("segment_type"):
            st = str(selected_source_rule["segment_type"])
        elif iter_source_type in {"reddit", "reddit_subreddit", "reddit_thread"}:
            st = "reddit_post"
        elif iter_source_type in {"youtube", "youtube_channel", "youtube_playlist", "youtube_video"}:
            st = "youtube"
        else:
            st = random.choice(show.segment_types)

        log(f"\n[{i+1}/{count}]")

        result = generate_segment(
            show,
            st,
            topic=topic,
            source_type=iter_source_type,
            source_value=iter_source_value,
            source_lookback_days=iter_source_lookback_days,
            source_selection_strategy=iter_source_selection_strategy,
            source_context=selected_source_context,
            used_source_keys=used_source_keys,
            station_name=station_name,
            include_topic=include_topic,
        )
        if result:
            success += 1
            if selected_source_context:
                used_key = _canonical_source_key(
                    selected_source_context.source_type,
                    selected_source_context.source_value,
                )
                if used_key:
                    used_source_keys.add(used_key)
            _record_source_rotation(show_id, selected_source_rule)

        if i < count - 1:
            time.sleep(2)

    return success


def generate_for_current(schedule: StationSchedule, count: int = 3) -> int:
    """Generate segments for the currently active show."""
    resolved = schedule.resolve()
    return generate_for_show(resolved.show_id, schedule, count)


def generate_all(schedule: StationSchedule, count_per_show: int = 3) -> dict[str, int]:
    """Generate content for all shows."""
    log("=== WRIT-FM Full Talk Content Generation ===")
    log(f"Generating {count_per_show} segments per show")

    results = {}
    for show_id in schedule.shows:
        results[show_id] = generate_for_show(show_id, schedule, count_per_show)
        time.sleep(3)

    log("\n=== Generation Complete ===")
    total = 0
    for show_id, count in results.items():
        show = schedule.shows[show_id]
        log(f"  {show.name}: {count}/{count_per_show}")
        total += count

    log(f"Total: {total} segments generated")
    return results


def count_segments() -> dict[str, int]:
    """Count existing segments per show."""
    counts = {}
    if OUTPUT_DIR.exists():
        for show_dir in OUTPUT_DIR.iterdir():
            if show_dir.is_dir():
                wavs = list(show_dir.glob("*.wav"))
                counts[show_dir.name] = len(wavs)
    return counts


# =============================================================================
# CLI
# =============================================================================


def main():
    parser = argparse.ArgumentParser(description="WRIT-FM Talk Segment Generator")
    parser.add_argument("--show", help="Show ID to generate for (default: current show)")
    parser.add_argument("--type", dest="segment_type", help="Specific segment type")
    parser.add_argument("--topic", help="Specific topic")
    parser.add_argument("--no-topic", action="store_true", help="Disable automatic topic selection")
    parser.add_argument("--source-type", help="Optional source type (url, reddit, youtube)")
    parser.add_argument("--source-value", help="Optional source value (URL, Reddit thread/subreddit, or YouTube link)")
    parser.add_argument("--count", type=int, default=3, help="Segments to generate (default: 3)")
    parser.add_argument("--all", action="store_true", help="Generate for all shows")
    parser.add_argument("--status", action="store_true", help="Show segment counts per show")
    parser.add_argument("--list-types", action="store_true", help="List segment types")
    parser.add_argument("--list-topics", help="List topics for a focus area")

    args = parser.parse_args()

    if args.list_types:
        print("\n=== Segment Types ===\n")
        short = []
        long = []
        for st, cfg in SEGMENT_DEFINITIONS.items():
            mn, mx = segment_word_targets(st)
            bucket = short if mx < 500 else long
            bucket.append((st, mn, mx, bool(cfg.get("multi_voice", False))))
        print("Long-form (primary content):")
        for st, mn, mx, multi in sorted(long):
            suffix = " [multi]" if multi else ""
            print(f"  {st:20s} {mn}-{mx} words{suffix}")
        print("\nShort-form (transitions):")
        for st, mn, mx, multi in sorted(short):
            suffix = " [multi]" if multi else ""
            print(f"  {st:20s} {mn}-{mx} words{suffix}")
        return 0

    if args.list_topics:
        focus = args.list_topics
        pool = TOPIC_POOLS.get(focus)
        if not pool:
            print(f"Unknown focus: {focus}")
            print(f"Available: {', '.join(TOPIC_POOLS.keys())}")
            return 1
        print(f"\n=== Topics: {focus} ===\n")
        for i, topic in enumerate(pool, 1):
            print(f"  {i:2d}. {topic}")
        return 0

    # Load schedule
    try:
        schedule = load_schedule(SCHEDULE_PATH)
        log(f"Loaded schedule with {len(schedule.shows)} shows")
    except Exception as e:
        log(f"Failed to load schedule: {e}")
        return 1

    if args.status:
        counts = count_segments()
        print("\n=== Talk Segment Inventory ===\n")
        for show_id, show in schedule.shows.items():
            c = counts.get(show_id, 0)
            status = "OK" if c >= 6 else "LOW" if c >= 3 else "EMPTY"
            print(f"  {show.name:30s} {c:3d} segments  [{status}]")
        total = sum(counts.values())
        print(f"\n  Total: {total} segments")
        return 0

    # Generate
    if args.all:
        generate_all(schedule, args.count)
    elif args.show:
        generated = generate_for_show(
            args.show,
            schedule,
            args.count,
            args.segment_type,
            args.topic,
            args.source_type or "",
            args.source_value or "",
            include_topic=not args.no_topic,
        )
        if generated == 0:
            log(f"ERROR: 0/{args.count} segments generated successfully")
            return 1
        if generated < args.count:
            log(f"WARNING: Only {generated}/{args.count} segments generated successfully")
    else:
        if args.segment_type or args.topic:
            resolved = schedule.resolve()
            generate_for_show(
                resolved.show_id,
                schedule,
                args.count,
                args.segment_type,
                args.topic,
                args.source_type or "",
                args.source_value or "",
                include_topic=not args.no_topic,
            )
        else:
            generate_for_current(schedule, args.count)

    return 0


if __name__ == "__main__":
    sys.exit(main())
