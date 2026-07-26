#!/usr/bin/env python3
"""
Shared helpers for WRIT-FM content generators.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from shared.settings import ollama_model as default_ollama_model
from station.time_utils import station_now

DEFAULT_NEWS_FEEDS = (
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://feeds.npr.org/1001/rss.xml",
)
NEWS_CACHE_TTL_SECONDS = int(os.environ.get("WRIT_NEWS_CACHE_TTL", "600"))
NEWS_TIMEOUT_SECONDS = int(os.environ.get("WRIT_NEWS_TIMEOUT", "6"))

_NEWS_CACHE: dict[str, object] = {"timestamp": 0.0, "items": []}


def log(msg: str) -> None:
    ts = station_now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def get_time_of_day(hour: int | None = None, profile: str = "default") -> str:
    if hour is None:
        hour = station_now().hour

    if profile == "extended":
        if 6 <= hour < 10:
            return "morning"
        if 10 <= hour < 14:
            return "daytime"
        if 14 <= hour < 15:
            return "early_afternoon"
        if 15 <= hour < 18:
            return "afternoon"
        if 18 <= hour < 24:
            return "evening"
        return "late_night"

    if 6 <= hour < 10:
        return "morning"
    if 10 <= hour < 18:
        return "daytime"
    if 18 <= hour < 24:
        return "evening"
    return "late_night"


# All interjection tags natively supported by MiniMax speech-2.8-hd.
_MM_INTERJECTIONS: frozenset[str] = frozenset({
    "(laughs)", "(chuckle)", "(sighs)", "(coughs)", "(clear-throat)",
    "(groans)", "(breath)", "(pant)", "(inhale)", "(exhale)", "(gasps)",
    "(sniffs)", "(snorts)", "(burps)", "(lip-smacking)", "(humming)",
    "(hissing)", "(emm)", "(sneezes)", "(yawns)",
})


# MiniMax native pause markup, e.g. "<#0.5#>". It reaches non-MiniMax backends two
# ways: the model emits it directly on a MiniMax show, or a MiniMax render falls back
# to another backend. Kokoro has no idea what it is and reads it aloud as
# "number zero point five number", so every non-MiniMax path must neutralise it.
_MM_PAUSE = re.compile(r"<\s*#\s*\d+(?:\.\d+)?\s*#\s*>")


# Matches speaker-label prefixes like "Host:", "HOST_A:", "Dr. Resonance:", "Vector (laughing):"
_SPEAKER_PREFIX = re.compile(
    r"^[A-Z][A-Za-z_]*(?:\s+[A-Z][A-Za-z_]*){0,3}(?:\s*\([^)]+\))?:\s+",
    re.MULTILINE,
)


def preprocess_for_tts(text: str, *, include_cough: bool = True, backend: str = "kokoro") -> str:
    backend = (backend or "kokoro").strip().lower()

    if backend == "minimax":
        text = text or ""
        # Strip speaker-label prefixes — MiniMax reads them as literal text.
        text = _SPEAKER_PREFIX.sub("", text)
        # speech-2.8-hd supports: <#n#> timed pauses and (interjection) tags.
        # Translate generic script cues to native MiniMax formats.
        tag_map = {
            "[pause]":   "<#0.5#>",
            "[laugh]":   "(laughs)",
            "[chuckle]": "(chuckle)",
            "[sigh]":    "(sighs)",
            "[cough]":   "(coughs)",
            "[gasp]":    "(gasps)",
            "[breath]":  "(breath)",
            "[groan]":   "(groans)",
            "[exhale]":  "(exhale)",
            "[inhale]":  "(inhale)",
            "[hum]":     "(humming)",
            "[yawn]":    "(yawns)",
            "[sniff]":   "(sniffs)",
        }
        for src, dst in tag_map.items():
            text = re.sub(re.escape(src), dst, text, flags=re.IGNORECASE)
        # Strip remaining unknown square-bracket production cues.
        text = re.sub(r"\[[^\]]+\]", " ", text)
        # Keep only known interjection tags; replace all other parentheticals with a space.
        text = re.sub(
            r"\([^)]+\)",
            lambda m: m.group(0) if m.group(0).lower() in _MM_INTERJECTIONS else " ",
            text,
        )
        text = text.replace('"', "")
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    if backend == "google":
        text = text or ""
        # Strip speaker-label prefixes — Gemini reads them as literal text.
        text = _SPEAKER_PREFIX.sub("", text)
        text = _MM_PAUSE.sub("...", text)
        # Gemini TTS is instruction-following: short parenthetical acting notes
        # embedded in the text are interpreted as spoken directions.
        tag_map = {
            "[pause]":   "...",
            "[laugh]":   "(laughs)",
            "[chuckle]": "(chuckle)",
            "[sigh]":    "(sighs)",
            "[cough]":   "(clears throat)",
            "[gasp]":    "(gasps)",
            "[breath]":  "(exhales)",
            "[groan]":   "(groans)",
            "[exhale]":  "(exhales)",
            "[inhale]":  "(inhales)",
            "[hum]":     "(hums)",
            "[yawn]":    "(yawns)",
            "[sniff]":   "(sniffs)",
            "[whisper]": "(whispers)",
        }
        for src, dst in tag_map.items():
            text = re.sub(re.escape(src), dst, text, flags=re.IGNORECASE)
        # Strip remaining unknown square-bracket production cues.
        text = re.sub(r"\[[^\]]+\]", " ", text)
        # Preserve short standalone parentheticals — Gemini interprets them as
        # acting directions. Strip only long ones (production notes).
        lines = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("(") and line.endswith(")") and len(line) > 20:
                continue
            lines.append(raw_line)
        text = "\n".join(lines)
        # Strip long inline parentheticals (production notes, not acting cues).
        text = re.sub(r"\s*\([^)]{25,}\)", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    # --- Kokoro (local) path: no native syntax — translate to spoken approximations ---

    # Strip speaker-label prefixes from line starts (e.g. "Zara:", "HOST_A:",
    # "Liminal Operator:").  Kokoro reads them literally as text.
    text = _SPEAKER_PREFIX.sub("", text or "")
    text = _MM_PAUSE.sub("...", text)

    # Drop standalone stage-direction lines.
    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("(") and line.endswith(")"):
            continue
        lines.append(raw_line)
    text = "\n".join(lines)

    # Strip inline stage-direction parentheticals.
    text = re.sub(r"\s*\([a-z][a-z,\s]{0,50}\)", " ", text)

    # Translate known cues to spoken approximations; strip everything else.
    tag_map_kokoro = {
        "[pause]":   "...",
        "[laugh]":   "heh...",
        "[chuckle]": "heh...",
        "[sigh]":    "hmm...",
        "[gasp]":    "oh...",
        "[groan]":   "ugh...",
        "[exhale]":  "...",
        "[inhale]":  "...",
        "[hum]":     "hmm...",
        "[yawn]":    "...",
        "[sniff]":   "...",
        "[whisper]": "",
    }
    if include_cough:
        tag_map_kokoro["[cough]"] = "ahem..."
    for src, dst in tag_map_kokoro.items():
        text = re.sub(re.escape(src), dst, text, flags=re.IGNORECASE)
    # Strip any remaining unknown bracket cues.
    text = re.sub(r"\[[^\]]+\]", " ", text)
    text = text.replace("[breath]", "")
    text = text.replace('"', "")
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_claude_output(text: str, *, strip_quotes: bool = True) -> str:
    cleaned = text.replace("*", "").replace("_", "").strip()
    if strip_quotes and cleaned.startswith('"') and cleaned.endswith('"'):
        cleaned = cleaned[1:-1].strip()
    return cleaned


SHORT_TITLE_MAX = 50


def make_short_title(topic: str, channel: str = "") -> str:
    """Return a display-safe short title (≤ SHORT_TITLE_MAX chars), title only.

    The channel is stored separately (source_channel) and rendered as a badge
    in the UI — it is NOT embedded in the returned string.
    If the topic already fits, return it as-is.
    Otherwise call the LLM for a compressed title; fall back to truncation.
    """
    if not topic:
        return ""

    if len(topic) <= SHORT_TITLE_MAX:
        return topic

    short = _llm_shorten(topic, max_chars=SHORT_TITLE_MAX)
    return short


def _llm_shorten(text: str, max_chars: int) -> str:
    """Use the LLM to compress text to ≤ max_chars, with truncation fallback."""
    prompt = (
        f"Rewrite the following title as a concise label of {max_chars} characters or fewer. "
        "Keep the most important nouns and concepts. Output ONLY the shortened title, nothing else.\n\n"
        f"Title: {text}"
    )
    try:
        # think=False: a reasoning model spends all 64 tokens thinking and returns nothing.
        result = run_claude(prompt, timeout=20, temperature=0.2, num_predict=64,
                            strip_quotes=True, think=False)
        if result:
            result = result.strip().strip('"').strip("'")
            if result and len(result) <= max_chars + 5:
                return result[:max_chars]
    except Exception:
        pass
    # Truncation fallback
    return text[: max_chars - 1] + "…"


def run_claude(
    prompt: str,
    *,
    timeout: int = 120,
    model: str | None = None,
    min_length: int = 0,
    strip_quotes: bool = True,
    temperature: float = 0.8,
    num_predict: int = 8192,
    num_ctx: int | None = None,
    think: bool | None = None,
) -> str | None:
    # 1. Try Ollama (if configured)
    import json
    ollama_url = os.environ.get("OLLAMA_URL")
    if ollama_url:
        ollama_model = os.environ.get("OLLAMA_MODEL") or default_ollama_model()
        try:
            options: dict = {
                "num_predict": num_predict,
                "temperature": temperature,
            }
            # num_ctx must cover prompt tokens + output tokens; auto-size if not given.
            ctx = num_ctx or (max(16384, len(prompt) // 3 + num_predict))
            options["num_ctx"] = ctx
            payload = {
                "model": ollama_model,
                "prompt": prompt,
                "stream": False,
                "options": options,
            }
            # Only sent when the caller is explicit. On reasoning models (qwen3.x) a small
            # num_predict is consumed entirely by thinking tokens, leaving `response` empty —
            # so short utility calls pass think=False. Long-form script calls must NOT: with
            # reasoning disabled the model runs to the num_predict cap and loops badly
            # (measured 6.7k-7.9k words against a 1200-2200 target, one 12-gram repeated 94x).
            if think is not None:
                payload["think"] = think
            req = urllib.request.Request(
                f"{ollama_url.rstrip('/')}/api/generate",
                data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json'}
            )
            with urllib.request.urlopen(req, timeout=timeout) as response:
                result = json.loads(response.read().decode('utf-8'))
                if "response" in result:
                    script = clean_claude_output(result["response"], strip_quotes=strip_quotes)
                    if len(script) > min_length:
                        return script
        except Exception as e:
            log(f"Ollama error: {e}")

    # 2. Try Claude
    args = ["claude", "-p", prompt]
    if model:
        args.extend(["--model", model])

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0 and result.stdout.strip():
            script = clean_claude_output(result.stdout, strip_quotes=strip_quotes)
            if len(script) > min_length:
                return script
    except Exception:
        pass

    # 3. Fallback to Gemini CLI
    if shutil.which("gemini") is None:
        return None
    args = ["gemini", "--approval-mode", "plan", "-p", prompt]
    if model:
        args.extend(["--model", model])

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0 and result.stdout.strip():
            script = clean_claude_output(result.stdout, strip_quotes=strip_quotes)
            if len(script) > min_length:
                return script
    except Exception as exc:
        log(f"LLM error: {exc}")

    return None


def _strip_namespace(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _find_child_text(elem: ET.Element, name: str) -> str:
    for child in elem:
        if _strip_namespace(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def _extract_source_title(root: ET.Element, fallback: str) -> str:
    tag = _strip_namespace(root.tag)
    if tag == "rss":
        for child in root:
            if _strip_namespace(child.tag) == "channel":
                title = _find_child_text(child, "title")
                return title or fallback
    if tag == "feed":
        title = _find_child_text(root, "title")
        return title or fallback
    return fallback


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def fetch_headlines(max_items: int | None = None) -> list[dict]:
    now = time.time()
    cached_items = _NEWS_CACHE.get("items", [])
    if cached_items and now - float(_NEWS_CACHE.get("timestamp", 0.0)) < NEWS_CACHE_TTL_SECONDS:
        return list(cached_items)

    max_items = max_items or int(os.environ.get("WRIT_NEWS_MAX_ITEMS", "8"))
    feed_env = os.environ.get("WRIT_NEWS_FEEDS")
    feeds = [f.strip() for f in feed_env.split(",")] if feed_env else list(DEFAULT_NEWS_FEEDS)
    feeds = [f for f in feeds if f]

    headlines: list[dict] = []
    seen: set[str] = set()

    for feed_url in feeds:
        try:
            with urllib.request.urlopen(feed_url, timeout=NEWS_TIMEOUT_SECONDS) as response:
                content = response.read()
            root = ET.fromstring(content)
        except Exception:
            continue

        fallback = urllib.parse.urlparse(feed_url).netloc or "Unknown Source"
        source = _extract_source_title(root, fallback)

        for elem in root.iter():
            tag = _strip_namespace(elem.tag)
            if tag not in ("item", "entry"):
                continue
            title = _find_child_text(elem, "title")
            if not title:
                continue
            norm = _normalize_title(title)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            headlines.append({"title": title, "source": source})
            if len(headlines) >= max_items:
                break
        if len(headlines) >= max_items:
            break

    _NEWS_CACHE["timestamp"] = now
    _NEWS_CACHE["items"] = list(headlines)
    return headlines


def format_headlines(headlines: list[dict], max_items: int | None = None) -> str:
    if not headlines:
        return ""
    max_items = max_items or len(headlines)
    lines = []
    for item in headlines[:max_items]:
        title = item.get("title", "").strip()
        source = item.get("source", "").strip() or "Source"
        if title:
            lines.append(f"- [{source}] {title}")
    return "\n".join(lines)
