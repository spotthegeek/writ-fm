from __future__ import annotations

import os


DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "qwen3.5:4b"

DEFAULT_INVENTORY_CACHE_TTL = 300.0
DEFAULT_MESSAGE_COOLDOWN_SECONDS = 300

DEFAULT_MINIMAX_MUSIC_MODEL = "music-2.6"
DEFAULT_MINIMAX_TTS_MODEL = "speech-2.8-hd"
DEFAULT_GOOGLE_TTS_MODEL = "gemini-3.1-flash-tts-preview"
DEFAULT_GOOGLE_TTS_SAMPLE_RATE = 24000
DEFAULT_GOOGLE_TTS_TIMEOUT_SECONDS = 300.0
DEFAULT_GOOGLE_TTS_MAX_RETRIES = 3

DEFAULT_VOICE_BY_BACKEND_AND_ROLE = {
    "kokoro": {
        "host": "am_michael",
        "guest": "af_bella",
    },
    "minimax": {
        "host": "Deep_Voice_Man",
        "guest": "Wise_Woman",
    },
    "google": {
        "host": "Kore",
        "guest": "Puck",
    },
}


def inventory_cache_ttl() -> float:
    return float(os.environ.get("WRIT_INVENTORY_CACHE_TTL", str(DEFAULT_INVENTORY_CACHE_TTL)))


def message_cooldown_seconds() -> int:
    return int(os.environ.get("WRIT_MESSAGE_COOLDOWN", str(DEFAULT_MESSAGE_COOLDOWN_SECONDS)))


def ollama_url() -> str:
    return os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL)


def ollama_model() -> str:
    return os.environ.get("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)


def minimax_music_model() -> str:
    return os.environ.get("MINIMAX_MUSIC_MODEL", DEFAULT_MINIMAX_MUSIC_MODEL)


def minimax_tts_model() -> str:
    return os.environ.get("MINIMAX_TTS_MODEL", DEFAULT_MINIMAX_TTS_MODEL)


def google_tts_model() -> str:
    return os.environ.get("GOOGLE_TTS_MODEL", DEFAULT_GOOGLE_TTS_MODEL)


def google_tts_sample_rate() -> int:
    return int(os.environ.get("GOOGLE_TTS_SAMPLE_RATE", str(DEFAULT_GOOGLE_TTS_SAMPLE_RATE)))


def google_tts_timeout_seconds() -> float:
    return float(os.environ.get("GOOGLE_TTS_TIMEOUT", str(DEFAULT_GOOGLE_TTS_TIMEOUT_SECONDS)))


def google_tts_max_retries() -> int:
    return max(1, int(os.environ.get("GOOGLE_TTS_MAX_RETRIES", str(DEFAULT_GOOGLE_TTS_MAX_RETRIES))))


def google_tts_api_key() -> str:
    return (
        os.environ.get("GOOGLE_TTS_API_KEY", "")
        or os.environ.get("GEMINI_API_KEY", "")
        or os.environ.get("GOOGLE_API_KEY", "")
    )


def default_voice_for_backend(backend: str, role: str = "host") -> str:
    backend_key = (backend or "kokoro").strip().lower()
    role_key = "guest" if role == "guest" else "host"
    backend_defaults = DEFAULT_VOICE_BY_BACKEND_AND_ROLE.get(
        backend_key,
        DEFAULT_VOICE_BY_BACKEND_AND_ROLE["kokoro"],
    )
    return backend_defaults.get(role_key, DEFAULT_VOICE_BY_BACKEND_AND_ROLE["kokoro"][role_key])


def icecast_status_url() -> str:
    return os.environ.get(
        "ICECAST_STATUS_URL",
        f"http://{os.environ.get('ICECAST_HOST', 'localhost')}:{int(os.environ.get('ICECAST_PORT', '8000'))}/status-json.xsl",
    )
