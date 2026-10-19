from __future__ import annotations

import os


DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "gemma3:12b"

DEFAULT_INVENTORY_CACHE_TTL = 300.0
DEFAULT_MESSAGE_COOLDOWN_SECONDS = 300

DEFAULT_MINIMAX_MUSIC_MODEL = "music-2.6"
DEFAULT_MINIMAX_TTS_MODEL = "speech-2.8-hd"
DEFAULT_GOOGLE_TTS_MODEL = "gemini-3.1-flash-tts-preview"
DEFAULT_GOOGLE_TTS_SAMPLE_RATE = 24000
DEFAULT_GOOGLE_TTS_TIMEOUT_SECONDS = 300.0
DEFAULT_GOOGLE_TTS_MAX_RETRIES = 3

# Music bumper generation. Lyria bills per 30s of output, so a 15-30s bumper costs
# one unit; MiniMax always renders ~130s regardless of the requested duration.
DEFAULT_MUSIC_BACKEND = "lyria"
DEFAULT_LYRIA_MODEL = "lyria-002"
DEFAULT_VERTEX_LOCATION = "us-central1"

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
    # crouch-tts on mac01: voices designed once and named on the service (GET /v1/voices).
    "breeze": {
        "host": "silas",
        "guest": "wren",
    },
    "qwen": {
        "host": "silas",
        "guest": "wren",
    },
}

TTS_BACKENDS = ("breeze", "qwen", "kokoro", "minimax", "google")
MAC_TTS_BACKENDS = ("breeze", "qwen")          # served by crouch-tts on mac01
DEFAULT_TTS_BACKEND = "breeze"
DEFAULT_MAC_TTS_TIMEOUT_SECONDS = 600.0
DEFAULT_MAC_TTS_MAX_RETRIES = 2


def inventory_cache_ttl() -> float:
    return float(os.environ.get("WRIT_INVENTORY_CACHE_TTL", str(DEFAULT_INVENTORY_CACHE_TTL)))


def message_cooldown_seconds() -> int:
    return int(os.environ.get("WRIT_MESSAGE_COOLDOWN", str(DEFAULT_MESSAGE_COOLDOWN_SECONDS)))


def ollama_url() -> str:
    return os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL)


def ollama_model() -> str:
    return os.environ.get("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)


def ollama_think() -> bool | None:
    """OLLAMA_THINK: the model's reasoning mode when a caller does not choose one.

    Unset leaves it to the model (qwen3.x must keep thinking on for long scripts, or it
    loops). "false" is for models such as Gemma 4, which otherwise spend the whole
    num_predict budget on hidden thinking and return an empty response."""
    value = os.environ.get("OLLAMA_THINK", "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return None


def minimax_music_model() -> str:
    return os.environ.get("MINIMAX_MUSIC_MODEL", DEFAULT_MINIMAX_MUSIC_MODEL)


def music_backend() -> str:
    """Which provider generates music bumpers: 'lyria' or 'minimax'."""
    return (os.environ.get("WRIT_MUSIC_BACKEND") or DEFAULT_MUSIC_BACKEND).strip().lower()


def lyria_model() -> str:
    return os.environ.get("LYRIA_MODEL", DEFAULT_LYRIA_MODEL)


def vertex_location() -> str:
    return os.environ.get("GOOGLE_VERTEX_LOCATION", DEFAULT_VERTEX_LOCATION)


def vertex_project() -> str:
    """Vertex AI needs a GCP project; Gemini API keys do not carry one."""
    return (
        os.environ.get("GOOGLE_VERTEX_PROJECT", "").strip()
        or os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    )


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


def default_tts_backend() -> str:
    """The backend a show or host uses when it names none (WRIT_DEFAULT_TTS_BACKEND, else breeze)."""
    value = os.environ.get("WRIT_DEFAULT_TTS_BACKEND", "").strip().lower()
    return value if value in TTS_BACKENDS else DEFAULT_TTS_BACKEND


def tts_fallback_backend() -> str:
    """WRIT_TTS_FALLBACK_BACKEND: re-render with this backend when a crouch-tts render fails (unset = none)."""
    value = os.environ.get("WRIT_TTS_FALLBACK_BACKEND", "").strip().lower()
    return value if value in TTS_BACKENDS else ""


def mac_tts_service_url() -> str:
    return os.environ.get("MAC_TTS_SERVICE_URL", "").strip().rstrip("/")


def mac_tts_timeout_seconds() -> float:
    return float(os.environ.get("MAC_TTS_TIMEOUT", str(DEFAULT_MAC_TTS_TIMEOUT_SECONDS)))


def mac_tts_max_retries() -> int:
    return max(1, int(os.environ.get("MAC_TTS_MAX_RETRIES", str(DEFAULT_MAC_TTS_MAX_RETRIES))))


def voice_field(backend: str) -> str:
    """The per-host voice field for a backend in shows.yaml host entries (voice_breeze, voice_kokoro, ...)."""
    return f"voice_{(backend or default_tts_backend()).strip().lower()}"


def roster_voice_field(backend: str) -> str:
    """The same field in hosts.yaml, where Kokoro's is the older `tts_voice`."""
    key = (backend or default_tts_backend()).strip().lower()
    return "tts_voice" if key == "kokoro" else f"voice_{key}"


def default_voice_for_backend(backend: str, role: str = "host") -> str:
    backend_key = (backend or default_tts_backend()).strip().lower()
    role_key = "guest" if role == "guest" else "host"
    backend_defaults = DEFAULT_VOICE_BY_BACKEND_AND_ROLE.get(
        backend_key,
        DEFAULT_VOICE_BY_BACKEND_AND_ROLE[DEFAULT_TTS_BACKEND],
    )
    return backend_defaults.get(role_key, DEFAULT_VOICE_BY_BACKEND_AND_ROLE[DEFAULT_TTS_BACKEND][role_key])


def icecast_status_url() -> str:
    return os.environ.get(
        "ICECAST_STATUS_URL",
        f"http://{os.environ.get('ICECAST_HOST', 'localhost')}:{int(os.environ.get('ICECAST_PORT', '8000'))}/status-json.xsl",
    )


def admin_password() -> str:
    return os.environ.get("WRIT_ADMIN_PASSWORD", "")


def admin_session_days() -> int:
    return int(os.environ.get("WRIT_ADMIN_SESSION_DAYS", "30"))


def listener_auth_enabled() -> bool:
    return os.environ.get("WRIT_LISTENER_AUTH", "").strip() == "1"


def listener_lan_ranges() -> str:
    return os.environ.get("WRIT_LISTENER_LAN_RANGES", "10.0.0.0/23")


def kokoro_service_url() -> str:
    return os.environ.get("KOKORO_SERVICE_URL", "")
