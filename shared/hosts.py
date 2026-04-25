from __future__ import annotations

from typing import Any, Callable

from shared.settings import default_voice_for_backend


RosterLookup = Callable[[str], dict[str, Any] | None]


def _show_value(show: Any, key: str, default: Any = None) -> Any:
    if isinstance(show, dict):
        return show.get(key, default)
    return getattr(show, key, default)


def host_label(host_id: str, roster_lookup: RosterLookup | None = None) -> str:
    if roster_lookup and host_id:
        try:
            host = roster_lookup(host_id) or {}
            if host.get("name"):
                return str(host["name"])
        except Exception:
            pass
    return host_id.replace("_", " ").title()


def primary_host_assignment(show: Any, roster_lookup: RosterLookup | None = None) -> dict[str, Any]:
    hosts = _show_value(show, "hosts", []) or []
    show_backend = str(_show_value(show, "tts_backend", "") or "").strip()
    for host in hosts:
        if host.get("role") == "primary":
            assignment = dict(host)
            if show_backend:
                assignment["tts_backend"] = show_backend
            elif not assignment.get("tts_backend"):
                assignment["tts_backend"] = "kokoro"
            return assignment
    if hosts:
        assignment = dict(hosts[0])
        if show_backend:
            assignment["tts_backend"] = show_backend
        elif not assignment.get("tts_backend"):
            assignment["tts_backend"] = "kokoro"
        return assignment

    host_id = _show_value(show, "host", "liminal_operator")
    tts_backend = show_backend or str(_show_value(show, "tts_backend", "kokoro") or "kokoro").strip() or "kokoro"
    legacy_voices = _show_value(show, "voices", {}) or {}
    legacy_host_voice = legacy_voices.get("host") if isinstance(legacy_voices, dict) else None

    assignment = {
        "id": host_id,
        "role": "primary",
        "tts_backend": tts_backend,
        "voice_kokoro": default_voice_for_backend("kokoro", "host"),
        "voice_minimax": default_voice_for_backend("minimax", "host"),
        "voice_google": default_voice_for_backend("google", "host"),
    }

    if roster_lookup and host_id:
        try:
            roster_host = roster_lookup(host_id) or {}
            assignment["voice_kokoro"] = roster_host.get("tts_voice", assignment["voice_kokoro"])
            assignment["voice_minimax"] = roster_host.get("voice_minimax", assignment["voice_minimax"])
            assignment["voice_google"] = roster_host.get("voice_google", assignment["voice_google"])
        except Exception:
            pass

    assignment["tts_backend"] = tts_backend
    if tts_backend == "minimax" and legacy_host_voice:
        assignment["voice_minimax"] = legacy_host_voice
    elif tts_backend == "google" and legacy_host_voice:
        assignment["voice_google"] = legacy_host_voice
    elif legacy_host_voice:
        assignment["voice_kokoro"] = legacy_host_voice
    return assignment


def secondary_host_assignment(show: Any, primary: dict[str, Any] | None = None) -> dict[str, Any] | None:
    primary = primary or primary_host_assignment(show)
    hosts = _show_value(show, "hosts", []) or []
    primary_id = str((primary or {}).get("id") or "").strip()
    for host in hosts:
        if primary_id and str(host.get("id") or "").strip() == primary_id:
            continue
        if host.get("role") in {"co-host", "secondary", "guest", "call-in"}:
            return dict(host)
    for host in hosts:
        if primary_id and str(host.get("id") or "").strip() == primary_id:
            continue
        if host is not primary:
            return dict(host)
    return None


def assignment_voice(
    assignment: dict[str, Any] | None,
    backend: str,
    *,
    role: str = "host",
    roster_lookup: RosterLookup | None = None,
) -> str:
    fallback = default_voice_for_backend(backend, role)
    if not assignment:
        return fallback
    host_id = assignment.get("id", "")
    roster_host = None
    if roster_lookup and host_id:
        try:
            roster_host = roster_lookup(host_id) or {}
        except Exception:
            roster_host = None
    if backend == "minimax":
        return assignment.get("voice_minimax") or (roster_host or {}).get("voice_minimax") or fallback
    if backend == "google":
        return assignment.get("voice_google") or (roster_host or {}).get("voice_google") or fallback
    return assignment.get("voice_kokoro") or (roster_host or {}).get("tts_voice") or fallback


def assignment_wpm(
    assignment: dict[str, Any] | None,
    backend: str = "kokoro",
    *,
    fallback_wpm: int = 130,
    roster_lookup: RosterLookup | None = None,
) -> int:
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
    if roster_lookup and host_id:
        try:
            host = roster_lookup(host_id) or {}
            pace = host.get(backend_key)
            if pace:
                return int(pace)
            pace = host.get("speaking_pace_wpm")
            if pace:
                return int(pace)
        except Exception:
            pass
    return fallback_wpm
