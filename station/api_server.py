#!/usr/bin/env python3
"""
WRIT-FM Now Playing API

HTTP server that exposes current track info, schedule, history, and more.
Runs as a daemon thread inside the streamer process.
"""

import hashlib
import hmac
import http.server
import ipaddress
import json
import os
import secrets
import socketserver
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
import sys
from pathlib import Path

# On-demand module (lazy import to avoid circular issues at startup)
_ondemand = None
_ondemand_lock = threading.Lock()


def _od():
    global _ondemand
    if _ondemand is None:
        with _ondemand_lock:
            if _ondemand is None:
                import ondemand as _mod
                _ondemand = _mod
    return _ondemand

PROJECT_ROOT = Path(__file__).resolve().parents[1]
from station.time_utils import station_now, station_iso_now, station_from_timestamp
from shared.settings import icecast_status_url, message_cooldown_seconds

# Import play history
try:
    from play_history import get_history
    HISTORY_ENABLED = True
except ImportError:
    HISTORY_ENABLED = False

# Import Discogs lookup and QR generation
try:
    from discogs_lookup import search_discogs, DiscogsResult, HAS_CREDENTIALS as DISCOGS_HAS_CREDS
    DISCOGS_ENABLED = True
except ImportError:
    DISCOGS_ENABLED = False
    DISCOGS_HAS_CREDS = False

try:
    from qr_generator import generate_qr_png, generate_qr_data_url, HAS_QRCODE
    QR_ENABLED = HAS_QRCODE
except ImportError:
    QR_ENABLED = False

# Discogs lookup cache to avoid repeated lookups for the same track
_discogs_cache: dict[str, dict | None] = {}
_discogs_last_track: str | None = None
_live_queue_getter = None
_live_queue_lock = threading.Lock()
_live_queue_state: dict = {
    "show_id": None,
    "show_name": None,
    "host": None,
    "current_item": None,
    "upcoming": [],
    "display_queue": [],
    "queue_length": 0,
    "updated_at": None,
}
_live_command_queue: list[dict] = []

# Live show override state — set via POST /override, consulted by stream_gapless
_live_override_lock = threading.Lock()
_live_override: dict | None = None  # {"show_id": str, "end_at": ISO-datetime str}

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MESSAGES_FILE = Path.home() / ".writ" / "messages.json"

# Rate limiting for messages
MESSAGE_COOLDOWN = message_cooldown_seconds()
last_message_times: dict[str, float] = {}

PORT = int(os.environ.get("WRIT_NOW_PLAYING_PORT", "8001"))
ICECAST_STATUS_URL = icecast_status_url()

# ---------------------------------------------------------------------------
# Listener auth
# ---------------------------------------------------------------------------
_LISTENER_AUTH = os.environ.get("WRIT_LISTENER_AUTH", "").strip() == "1"
_LISTENER_COOKIE = "writ_listener_token"
_LISTENER_TOKENS_FILE = PROJECT_ROOT / "output" / "listener_tokens.json"
_LISTENER_TOKENS_LOCK = threading.Lock()
_LISTENER_EXEMPT = {"/stream", "/health", "/auth", "/logout", "/favicon.svg", "/favicon.ico"}

# Parse LAN bypass ranges at startup
_LAN_NETS: list = []
for _cidr in os.environ.get("WRIT_LISTENER_LAN_RANGES", "10.0.0.0/23").split(","):
    _cidr = _cidr.strip()
    if _cidr:
        try:
            _LAN_NETS.append(ipaddress.ip_network(_cidr, strict=False))
        except ValueError:
            pass

_AUTH_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WRIT-FM – Access Required</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;background:#111;color:#f0f0f0;display:flex;align-items:center;justify-content:center;height:100vh}
.card{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:12px;padding:36px 40px;width:340px}
h1{font-size:20px;font-weight:700;margin-bottom:4px;letter-spacing:-.02em}
.sub{font-size:13px;color:#9ca3af;margin-bottom:22px}
input{width:100%;padding:9px 12px;border-radius:7px;border:1px solid #2a2a2a;background:#111;color:#f0f0f0;font-size:14px;outline:none;margin-bottom:14px;font-family:inherit;letter-spacing:.05em}
button{width:100%;padding:10px;border-radius:7px;border:none;background:oklch(0.52 0.21 16);color:#fff;font-size:14px;font-weight:500;cursor:pointer;font-family:inherit}
.err{color:#f87171;font-size:13px;margin-bottom:12px;display:none}
</style>
</head>
<body>
<div class="card">
  <h1>WRIT&#8209;FM</h1>
  <p class="sub">Enter your invite token to listen.</p>
  <div class="err" id="err">Invalid token — please try again.</div>
  <form method="POST" action="/auth">
    <input type="text" name="token" placeholder="Invite token" autofocus autocomplete="off" spellcheck="false">
    <button type="submit">Access</button>
  </form>
</div>
<script>if(new URLSearchParams(location.search).get('error'))document.getElementById('err').style.display='block';</script>
</body>
</html>"""


def _load_listener_tokens() -> list[dict]:
    if not _LISTENER_TOKENS_FILE.exists():
        return []
    try:
        return json.loads(_LISTENER_TOKENS_FILE.read_text()) or []
    except Exception:
        return []


def _is_valid_token(token: str) -> bool:
    if not token:
        return False
    for entry in _load_listener_tokens():
        stored = entry.get("token", "")
        if stored and hmac.compare_digest(stored, token):
            return True
    return False


def _touch_last_used(token: str) -> None:
    try:
        with _LISTENER_TOKENS_LOCK:
            tokens = _load_listener_tokens()
            now = datetime.now(timezone.utc).isoformat()
            for entry in tokens:
                if entry.get("token") == token:
                    entry["last_used"] = now
                    break
            _LISTENER_TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
            _LISTENER_TOKENS_FILE.write_text(json.dumps(tokens, indent=2))
    except Exception:
        pass


def _is_lan_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        return addr.is_loopback or any(addr in net for net in _LAN_NETS)
    except ValueError:
        return False


def _get_cookies(handler) -> dict[str, str]:
    raw = handler.headers.get("Cookie", "")
    if not raw:
        return {}
    c = SimpleCookie()
    try:
        c.load(raw)
    except Exception:
        return {}
    return {k: v.value for k, v in c.items()}


def _check_listener_auth(handler) -> bool:
    if not _LISTENER_AUTH:
        return True
    path = urllib.parse.urlparse(handler.path).path.rstrip("/") or "/"
    if path in _LISTENER_EXEMPT:
        return True
    if _is_lan_ip(handler.client_address[0]):
        return True
    return _is_valid_token(_get_cookies(handler).get(_LISTENER_COOKIE, ""))


def _deny(handler) -> None:
    path = urllib.parse.urlparse(handler.path).path.rstrip("/") or "/"
    if path.startswith("/api/"):
        body = json.dumps({"error": "Authentication required"}).encode()
        handler.send_response(401)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        try:
            handler.wfile.write(body)
        except BrokenPipeError:
            pass
    else:
        handler.send_response(302)
        handler.send_header("Location", "/auth")
        handler.send_header("Content-Length", "0")
        handler.end_headers()


# Shared state — set by start_api_thread()
_track_info: dict = {}
_encoder_getter = None
_listener_fn = None

# Server start time for uptime tracking
SERVER_START_TIME = time.time()
TRACKS_PLAYED = 0
TOTAL_LISTENERS_SERVED = 0
LAST_TRACK = None


class NowPlayingHandler(http.server.BaseHTTPRequestHandler):
    def _send_json(self, data, cache_control=None):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        self.end_headers()
        try:
            self.wfile.write(json.dumps(data).encode())
        except BrokenPipeError:
            pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"

        # Listener auth guard
        if not _check_listener_auth(self):
            return _deny(self)

        # GET /auth — serve token entry page
        if path == "/auth":
            body = _AUTH_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass
            return

        # GET /logout — clear cookie, redirect to /auth
        if path == "/logout":
            self.send_response(302)
            self.send_header(
                "Set-Cookie",
                f"{_LISTENER_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax",
            )
            self.send_header("Location", "/auth")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/now-playing":
            data = get_now_playing()
            track_stats_update(data)
            self._send_json(data, "no-cache, no-store, must-revalidate")
        elif path in ("/", "/index.html"):
            try:
                with open(PROJECT_ROOT / "listener-app" / "index.html", "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(content)
            except Exception as e:
                self._send_error(500, str(e))
        elif path in ("/favicon.svg", "/favicon.ico"):
            try:
                with open(PROJECT_ROOT / "listener-app" / "favicon.svg", "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(content)
            except Exception as e:
                self._send_error(500, str(e))
        elif path == "/health":
            self._send_json(get_health_status())
        elif path == "/stats":
            self._send_json(get_stats())
        elif path == "/schedule":
            self._send_json(get_schedule_info())
        elif path == "/override":
            self._send_json({"override": get_live_override()})
        elif path == "/upcoming":
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            hours = int(qs.get("hours", ["12"])[0])
            self._send_json({"upcoming": get_upcoming_shows(hours=hours), "override": get_live_override()})
        elif path == "/queue":
            self._send_json(get_live_queue())
        elif path.startswith("/history"):
            self._send_json(get_play_history())
        elif path == "/messages":
            self._send_json(get_messages())
        elif path == "/discogs":
            self._send_json(get_discogs_info())
        elif path == "/qr":
            qr_bytes = get_qr_code()
            if qr_bytes:
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "public, max-age=60")
                self.end_headers()
                try:
                    self.wfile.write(qr_bytes)
                except BrokenPipeError:
                    pass
            else:
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                try:
                    self.wfile.write(json.dumps({"error": "No Discogs info available"}).encode())
                except BrokenPipeError:
                    pass
        elif path == "/api/briefings":
            self._send_json(get_briefings())
        elif path == "/api/abs/sources":
            self._send_json(get_abs_sources())
        elif path == "/api/ondemand/sources":
            self._send_json(get_ondemand_sources())
        elif path.startswith("/api/ondemand/items"):
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            source = qs.get("source", [None])[0]
            self._send_json(get_ondemand_items(source))
        elif path == "/api/ondemand/state":
            self._send_json(get_ondemand_state())
        elif path.startswith("/api/ondemand/audio/"):
            item_id = path[len("/api/ondemand/audio/"):]
            item_id = urllib.parse.unquote(item_id)
            range_header = self.headers.get("Range")
            self._serve_ondemand_audio(item_id, range_header)
        elif path == "/stream":
            self._proxy_stream()
        else:
            self.send_response(404)
            self.end_headers()

    def _proxy_stream(self):
        """Proxy the Icecast stream so HTTPS frontends don't need direct port 8000 access."""
        import socket as _socket
        icecast_host = os.environ.get("ICECAST_HOST", "localhost")
        icecast_port = int(os.environ.get("ICECAST_PORT", "8000"))
        icecast_path = os.environ.get("ICECAST_MOUNT", "/stream")
        try:
            upstream = urllib.request.urlopen(
                f"http://{icecast_host}:{icecast_port}{icecast_path}",
                timeout=10,
            )
        except Exception as e:
            self._send_error(502, f"Stream unavailable: {e}")
            return
        self.send_response(200)
        self.send_header("Content-Type", upstream.headers.get("Content-Type", "audio/mpeg"))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while True:
                chunk = upstream.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, _socket.error):
            pass
        finally:
            upstream.close()

    def _send_error(self, code, msg):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"error": msg}).encode())

    def _serve_ondemand_audio(self, item_id: str, range_header: str | None):
        """Stream on-demand audio with byte-range support."""
        od = _od()
        if item_id.startswith("abs:"):
            abs_item_id = item_id[4:]
            client = od._make_abs_client()
            if client is None:
                return self._send_error(503, "ABS not configured")
            try:
                status, headers, body = client.proxy_audio(abs_item_id, range_header)
            except Exception as e:
                return self._send_error(502, str(e))
            self.send_response(status)
            for k, v in headers.items():
                if v:
                    self.send_header(k, v)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass
            return

        if item_id.startswith("upload:"):
            audio_path = od.get_upload_path(item_id)
            if audio_path is None:
                return self._send_error(404, "Item not found")
            mime = od._mime_for(audio_path)
            file_size = audio_path.stat().st_size
            start, end = 0, file_size - 1
            if range_header:
                try:
                    rng = range_header.replace("bytes=", "")
                    parts = rng.split("-")
                    start = int(parts[0]) if parts[0] else 0
                    end = int(parts[1]) if parts[1] else file_size - 1
                except Exception:
                    pass
            length = end - start + 1
            with open(audio_path, "rb") as f:
                f.seek(start)
                data = f.read(length)
            if range_header:
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                self.wfile.write(data)
            except BrokenPipeError:
                pass
            return

        if item_id.startswith("show:"):
            audio_path = od.get_segment_path(item_id)
            if audio_path is None:
                return self._send_error(404, "Item not found")
            mime = od._mime_for(audio_path)
            file_size = audio_path.stat().st_size
            start, end = 0, file_size - 1
            if range_header:
                try:
                    rng = range_header.replace("bytes=", "")
                    parts = rng.split("-")
                    start = int(parts[0]) if parts[0] else 0
                    end = int(parts[1]) if parts[1] else file_size - 1
                except Exception:
                    pass
            length = end - start + 1
            with open(audio_path, "rb") as f:
                f.seek(start)
                data = f.read(length)
            if range_header:
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                self.wfile.write(data)
            except BrokenPipeError:
                pass
            return

        self._send_error(400, "Unknown item type")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")

        # POST /auth — validate token, set cookie, redirect
        if path == "/auth":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(content_length).decode("utf-8")
                params = urllib.parse.parse_qs(raw)
                token = params.get("token", [""])[0].strip()
            except Exception:
                token = ""
            if _is_valid_token(token):
                _touch_last_used(token)
                self.send_response(302)
                self.send_header(
                    "Set-Cookie",
                    f"{_LISTENER_COOKIE}={token}; Path=/; Max-Age={30 * 86400}; HttpOnly; SameSite=Lax",
                )
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_response(302)
                self.send_header("Location", "/auth?error=1")
                self.send_header("Content-Length", "0")
                self.end_headers()
            return

        # Auth guard for all other POST endpoints
        if not _check_listener_auth(self):
            return _deny(self)

        if path == "/api/ondemand/state":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(content_length).decode("utf-8") or "{}")
                item_id = data.get("item_id")
                if not item_id:
                    return self._send_error(400, "item_id required")
                store = _od().get_store()
                if data.get("listened"):
                    store.mark_listened(item_id)
                elif "position_s" in data:
                    store.set_position(item_id, float(data["position_s"]), data.get("duration_s"))
                self._send_json({"ok": True})
            except Exception as e:
                self._send_error(500, str(e))
            return

        if path not in ("/message", "/control", "/override"):
            self.send_response(404)
            self.end_headers()
            return

        if path == "/override":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(content_length).decode("utf-8") or "{}")
                show_id = data.get("show_id")  # None → skip to next scheduled show
                from schedule import load_schedule as _load_sched
                sched = _load_sched(PROJECT_ROOT / "config")
                now = station_now()
                current_show_id = sched.resolve(now).show_id
                if show_id is None:
                    # Skip: find next show and run it for its full normal slot
                    next_start = _find_next_show_start(sched, now, current_show_id)
                    if next_start is None:
                        return self._send_error(400, "Could not determine next show start")
                    next_show = sched.resolve(next_start)
                    next_end = _find_show_end(sched, next_start, next_show.show_id)
                    if next_end is None:
                        return self._send_error(400, "Could not determine next show end time")
                    target_id, end_at = next_show.show_id, next_end
                else:
                    # Replace: chosen show runs until current slot ends
                    if show_id not in sched.shows:
                        return self._send_error(400, f"Unknown show: {show_id!r}")
                    current_end = _find_show_end(sched, now, current_show_id)
                    if current_end is None:
                        return self._send_error(400, "Could not determine current show end time")
                    target_id, end_at = show_id, current_end
                set_live_override(target_id, end_at.isoformat())
                # Also skip the current segment so playback cuts over immediately
                enqueue_live_command({"action": "skip"})
                self._send_json({"ok": True, "show_id": target_id, "end_at": end_at.isoformat()})
            except Exception as e:
                self._send_error(500, str(e))
            return

        if path == "/control":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(content_length).decode("utf-8") or "{}")
                if not isinstance(data, dict) or not data.get("action"):
                    return self._send_error(400, "Invalid control command")
                enqueue_live_command(data)
                self._send_json({"ok": True, "queued": data.get("action")})
            except Exception as e:
                self._send_error(500, str(e))
            return

        client_ip = self.client_address[0]
        now = time.time()
        if client_ip in last_message_times and now - last_message_times[client_ip] < MESSAGE_COOLDOWN:
            wait_time = int(MESSAGE_COOLDOWN - (now - last_message_times[client_ip]))
            return self._send_error(429, f"Please wait {wait_time}s")

        try:
            content_length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(content_length).decode('utf-8'))
            message = data.get('message', '').strip()
            if not message or len(message) > 280:
                return self._send_error(400, "Invalid message")

            save_message(message, client_ip)
            last_message_times[client_ip] = now
            self._send_json({"status": "received"})
        except Exception as e:
            self._send_error(500, str(e))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress logging


def check_process(name: str) -> bool:
    """Check if process is running."""
    try:
        return subprocess.run(["pgrep", "-f", name], capture_output=True, timeout=5).returncode == 0
    except:
        return False


def check_url(url: str, timeout: int = 2) -> bool:
    """Check if URL responds."""
    try:
        return urllib.request.urlopen(url, timeout=timeout).status == 200
    except:
        return False


def get_health_status() -> dict:
    """Get comprehensive health status of all components."""
    icecast_ok = check_url(ICECAST_STATUS_URL)
    encoder = _encoder_getter() if _encoder_getter else None
    streamer_ok = encoder is not None and encoder.poll() is None
    tunnel_ok = check_process("cloudflared")
    return {
        "status": "healthy" if icecast_ok and streamer_ok and tunnel_ok else "degraded",
        "timestamp": station_iso_now(),
        "components": {
            "icecast": {"status": "up" if icecast_ok else "down"},
            "streamer": {"status": "up" if streamer_ok else "down"},
            "tunnel": {"status": "up" if tunnel_ok else "down"},
            "api": {"status": "up"},
        },
        "uptime_seconds": int(time.time() - SERVER_START_TIME),
    }


def get_stats() -> dict:
    """Get server statistics."""
    uptime = int(time.time() - SERVER_START_TIME)
    hours = uptime // 3600
    minutes = (uptime % 3600) // 60
    listeners = _listener_fn() if _listener_fn else 0

    return {
        "uptime": f"{hours}h {minutes}m",
        "uptime_seconds": uptime,
        "tracks_played": TRACKS_PLAYED,
        "total_listeners_served": TOTAL_LISTENERS_SERVED,
        "current_listeners": listeners,
        "api_started": station_from_timestamp(SERVER_START_TIME).isoformat(),
    }


def track_stats_update(data: dict):
    """Update track statistics."""
    global TRACKS_PLAYED, TOTAL_LISTENERS_SERVED, LAST_TRACK

    current_track = data.get("track")
    if current_track and current_track != LAST_TRACK:
        TRACKS_PLAYED += 1
        LAST_TRACK = current_track

    listeners = data.get("listeners", 0)
    if listeners > 0:
        TOTAL_LISTENERS_SERVED += listeners


def get_play_history() -> dict:
    """Get play history from database."""
    if not HISTORY_ENABLED:
        return {"enabled": False, "message": "History tracking not available"}

    try:
        history = get_history()
        return {
            "enabled": True,
            "recent": history.get_recent_plays(50),
            "stats": history.get_stats(),
            "most_played": history.get_most_played(10),
        }
    except Exception as e:
        return {"enabled": True, "error": str(e)}


def save_message(message: str, ip: str):
    """Save a listener message to the queue."""
    MESSAGES_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Load existing messages
    messages = []
    if MESSAGES_FILE.exists():
        try:
            with open(MESSAGES_FILE) as f:
                messages = json.load(f)
        except:
            messages = []

    # Add new message
    messages.append({
        "message": message,
        "ip": ip,
        "timestamp": station_iso_now(),
        "read": False,
    })

    # Keep only last 100 messages
    messages = messages[-100:]

    # Save
    with open(MESSAGES_FILE, "w") as f:
        json.dump(messages, f, indent=2)


def get_messages(limit: int = 20) -> list[dict]:
    """Get recent messages."""
    if not MESSAGES_FILE.exists():
        return []

    try:
        with open(MESSAGES_FILE) as f:
            messages = json.load(f)
        # Return newest first, hide IP
        return [
            {"message": m["message"], "timestamp": m["timestamp"], "read": m.get("read", False)}
            for m in reversed(messages[-limit:])
        ]
    except:
        return []


def get_now_playing() -> dict:
    """Read current track info from shared in-memory state."""
    data = dict(_track_info)
    data["listeners"] = _listener_fn() if _listener_fn else 0
    return data


def get_live_queue() -> dict:
    if _live_queue_getter:
        try:
            state = _live_queue_getter() or {}
            if isinstance(state, dict):
                return state
        except Exception:
            pass
    with _live_queue_lock:
        state = dict(_live_queue_state)
        state["upcoming"] = list(_live_queue_state.get("upcoming", []))
        state["display_queue"] = list(_live_queue_state.get("display_queue", []))
        return state


def set_live_queue(state: dict) -> None:
    with _live_queue_lock:
        _live_queue_state.clear()
        _live_queue_state.update(state or {})


def enqueue_live_command(command: dict) -> None:
    with _live_queue_lock:
        _live_command_queue.append(dict(command))


def pop_live_command(actions: set[str] | list[str] | tuple[str, ...] | None = None) -> dict | None:
    with _live_queue_lock:
        if not _live_command_queue:
            return None
        if actions is None:
            return _live_command_queue.pop(0)
        wanted = {str(action).strip().lower() for action in actions if str(action).strip()}
        for idx, command in enumerate(_live_command_queue):
            action = str(command.get("action", "")).strip().lower()
            if action in wanted:
                return _live_command_queue.pop(idx)
        return None


# ---------------------------------------------------------------------------
# Live show override helpers
# ---------------------------------------------------------------------------

def get_live_override() -> dict | None:
    with _live_override_lock:
        return dict(_live_override) if _live_override else None


def set_live_override(show_id: str, end_at: str) -> None:
    global _live_override
    with _live_override_lock:
        _live_override = {"show_id": show_id, "end_at": end_at}


def clear_live_override() -> None:
    global _live_override
    with _live_override_lock:
        _live_override = None


def _find_next_show_start(sched, now, current_show_id: str, max_hours: int = 6) -> "datetime | None":
    """Walk forward minute-by-minute until the schedule changes show."""
    probe = now + timedelta(minutes=1)
    horizon = now + timedelta(hours=max_hours)
    while probe < horizon:
        try:
            if sched.resolve(probe).show_id != current_show_id:
                return probe.replace(second=0, microsecond=0)
        except Exception:
            break
        probe += timedelta(minutes=1)
    return None


def _find_show_end(sched, start, show_id: str, max_hours: int = 6) -> "datetime | None":
    """Walk forward minute-by-minute from start until show_id changes."""
    probe = start + timedelta(minutes=1)
    horizon = start + timedelta(hours=max_hours)
    while probe < horizon:
        try:
            if sched.resolve(probe).show_id != show_id:
                return probe.replace(second=0, microsecond=0)
        except Exception:
            break
        probe += timedelta(minutes=1)
    return None


def get_upcoming_shows(hours: int = 12) -> list[dict]:
    """Return upcoming show blocks for the next N hours, respecting any active override."""
    try:
        from schedule import load_schedule as _load_sched
        sched = _load_sched(PROJECT_ROOT / "config")
        now = station_now()
        tz = now.tzinfo
        horizon = now + timedelta(hours=hours)

        override = get_live_override()
        override_end: "datetime | None" = None
        override_show_id: str | None = None
        if override:
            try:
                oe = datetime.fromisoformat(override["end_at"])
                if tz and oe.tzinfo is None:
                    oe = oe.replace(tzinfo=tz)
                elif tz and oe.tzinfo is not None:
                    oe = oe.astimezone(tz)
                if oe > now:
                    override_end = oe
                    override_show_id = override["show_id"]
            except (ValueError, TypeError, KeyError):
                pass

        blocks: list[dict] = []

        if override_end and override_show_id:
            show = sched.shows.get(override_show_id)
            end = min(override_end, horizon)
            blocks.append({
                "show_id": override_show_id,
                "name": show.name if show else override_show_id,
                "start": now.isoformat(),
                "end": end.isoformat(),
                "is_override": True,
            })
            probe = min(override_end, horizon)
        else:
            probe = now

        while probe < horizon:
            try:
                resolved = sched.resolve(probe)
            except Exception:
                probe += timedelta(minutes=1)
                continue
            # Find end of this slot
            block_end = probe + timedelta(minutes=1)
            while block_end < horizon:
                try:
                    if sched.resolve(block_end).show_id != resolved.show_id:
                        break
                except Exception:
                    break
                block_end += timedelta(minutes=1)
            block_end = min(block_end, horizon)
            blocks.append({
                "show_id": resolved.show_id,
                "name": resolved.name,
                "start": probe.isoformat(),
                "end": block_end.isoformat(),
                "is_override": False,
            })
            probe = block_end

        return blocks
    except Exception:
        return []


def get_schedule_info() -> dict:
    """Get current and upcoming show schedule."""
    try:
        from schedule import load_schedule
        schedule = load_schedule(PROJECT_ROOT / "config")
        now = station_now()
        current = schedule.resolve(now)

        # Find upcoming shows (next 4 hours)
        upcoming = []
        for minutes_ahead in range(30, 241, 30):
            from datetime import timedelta
            future = now + timedelta(minutes=minutes_ahead)
            try:
                future_show = schedule.resolve(future)
                if not upcoming or upcoming[-1]["show_id"] != future_show.show_id:
                    upcoming.append({
                        "show_id": future_show.show_id,
                        "name": future_show.name,
                        "host": future_show.host,
                        "starts_around": future.strftime("%H:%M"),
                    })
            except Exception:
                pass

        return {
            "station_name": schedule.station_name if hasattr(schedule, "station_name") else "",
            "tagline": schedule.tagline if hasattr(schedule, "tagline") else "AI generated radio",
            "current": {
                "show_id": current.show_id,
                "name": current.name,
                "description": current.description,
                "host": current.host,
                "segment_types": current.segment_types,
                "bumper_style": current.bumper_style,
            },
            "upcoming": upcoming[:4],
            "timestamp": now.isoformat(),
        }
    except Exception as e:
        return {"error": str(e)}


def _qr_data_url_for(discogs_data: dict | None) -> str | None:
    if not QR_ENABLED or not discogs_data or not discogs_data.get("url"):
        return None
    return generate_qr_data_url(discogs_data["url"])


def get_discogs_info() -> dict:
    """Get Discogs info for the currently playing track.

    Returns a dict with Discogs release info, or an error/status message.
    For AI-generated bumpers, returns the generation metadata instead.
    Caches results to avoid repeated API calls for the same track.
    """
    global _discogs_cache, _discogs_last_track

    # Get current track
    now_playing = get_now_playing()
    track_name = now_playing.get("track")
    track_type = now_playing.get("type")

    # AI-generated bumper: return generation metadata instead of Discogs
    if track_type == "bumper" and now_playing.get("ai_generated"):
        return {
            "enabled": True,
            "ai_generated": True,
            "track": track_name,
            "caption": now_playing.get("caption"),
            "model": "ACE-Step (music-gen.server)",
            "show": now_playing.get("show"),
        }

    if not DISCOGS_ENABLED:
        return {"enabled": False, "message": "Discogs lookup not available"}

    if not DISCOGS_HAS_CREDS:
        return {
            "enabled": False,
            "message": "Discogs API requires authentication",
            "setup": "Set DISCOGS_TOKEN env var. Get token at https://www.discogs.com/settings/developers"
        }

    vibe = now_playing.get("vibe")

    # Only look up music tracks, not segments or podcasts
    if not track_name or track_type != "music":
        return {"enabled": True, "track": track_name, "discogs": None, "reason": "Not a music track"}

    # Check cache
    if track_name in _discogs_cache:
        cached = _discogs_cache[track_name]
        if cached is None:
            return {"enabled": True, "track": track_name, "discogs": None, "reason": "Not found on Discogs"}
        return {
            "enabled": True,
            "track": track_name,
            "discogs": cached,
            "qr_data_url": _qr_data_url_for(cached),
        }

    # Perform lookup (only if track changed)
    if track_name != _discogs_last_track:
        _discogs_last_track = track_name
        result = search_discogs(track_name, vibe)

        if result:
            discogs_data = {
                "release_id": result.release_id,
                "title": result.title,
                "artist": result.artist,
                "year": result.year,
                "url": result.url,
                "thumb_url": result.thumb_url,
                "label": result.label,
                "format": result.format,
            }
            _discogs_cache[track_name] = discogs_data
            return {
                "enabled": True,
                "track": track_name,
                "discogs": discogs_data,
                "qr_data_url": _qr_data_url_for(discogs_data),
            }
        else:
            _discogs_cache[track_name] = None
            return {"enabled": True, "track": track_name, "discogs": None, "reason": "Not found on Discogs"}

    # Track hasn't changed, return cached or pending
    if track_name in _discogs_cache:
        cached = _discogs_cache[track_name]
        if cached is None:
            return {"enabled": True, "track": track_name, "discogs": None, "reason": "Not found on Discogs"}
        return {
            "enabled": True,
            "track": track_name,
            "discogs": cached,
            "qr_data_url": _qr_data_url_for(cached),
        }

    return {"enabled": True, "track": track_name, "discogs": None, "reason": "Lookup pending"}


def get_qr_code() -> bytes | None:
    """Get QR code PNG for the current track's Discogs page."""
    if not QR_ENABLED:
        return None
    discogs_data = get_discogs_info().get("discogs")
    if not discogs_data or not discogs_data.get("url"):
        return None
    return generate_qr_png(discogs_data["url"])


def get_briefings() -> dict:
    """Return briefing categories (show:briefing_*) with all items, newest first, merged with playback state."""
    try:
        from collections import defaultdict
        od = _od()
        items = od.get_show_segment_items(show_prefix="briefing_")
        store = od.get_store()
        states = store.get_all_states()

        by_source: dict[str, list] = defaultdict(list)
        for item in items:
            by_source[item.source].append(item)

        categories = []
        for source_id in sorted(by_source.keys()):
            items_list = sorted(by_source[source_id], key=lambda i: i.created_at or "", reverse=True)
            if not items_list:
                continue
            cat_items = []
            for item in items_list:
                state = states.get(item.id, {})
                d = item.to_dict()
                d["position_s"] = state.get("position_s", 0.0)
                d["listened"] = bool(state.get("listened", 0))
                cat_items.append(d)
            categories.append({
                "source_id": source_id,
                "show_id": source_id[5:],
                "name": items_list[0].source_name,
                "items": cat_items,
            })
        return {"categories": categories}
    except Exception as e:
        return {"categories": [], "error": str(e)}


def get_ondemand_sources() -> dict:
    """Return on-demand sources: non-briefing show segments + upload buckets (no ABS)."""
    try:
        od = _od()
        cfg = od.load_config()
        sources = []
        for s in od.get_show_sources():
            if not s["id"].startswith("show:briefing_"):
                sources.append(s)
        for src in cfg.get("upload_sources", []):
            src_dir = od.UPLOADS_DIR / src["id"]
            if src_dir.exists() and any(
                f.suffix.lower() in od.AUDIO_EXTS for f in src_dir.iterdir() if f.is_file()
            ):
                sources.append({"id": f"upload:{src['id']}", "name": src["name"], "type": "upload"})
        return {"sources": sources}
    except Exception as e:
        return {"sources": [], "error": str(e)}


def get_abs_sources() -> dict:
    """Return ABS library sources from config (no network call)."""
    try:
        od = _od()
        cfg = od.load_config()
        sources = [
            {"id": f"abs:{lib['id']}", "name": lib["name"], "type": "abs"}
            for lib in cfg.get("abs", {}).get("libraries", [])
        ]
        return {"sources": sources}
    except Exception as e:
        return {"sources": [], "error": str(e)}


def get_ondemand_items(source: str | None = None) -> dict:
    """Return inventory items merged with playback state.

    ABS sources go through the full cached inventory (network calls).
    Local sources (show segments + uploads) use a fast filesystem-only path.
    """
    try:
        od = _od()
        if source and source.startswith("abs:"):
            items = od.get_inventory()
        else:
            items = od.get_show_segment_items() + od.get_upload_items()
        if source:
            items = [i for i in items if i.source == source]
        store = od.get_store()
        states = store.get_all_states()
        result = []
        for item in items:
            d = item.to_dict()
            state = states.get(item.id, {})
            d["position_s"] = state.get("position_s", 0.0)
            d["listened"] = bool(state.get("listened", 0))
            result.append(d)
        return {"items": result}
    except Exception as e:
        return {"items": [], "error": str(e)}


def get_ondemand_state() -> dict:
    """Return all playback state rows."""
    try:
        od = _od()
        return {"state": od.get_store().get_all_states()}
    except Exception as e:
        return {"state": {}, "error": str(e)}


class ReusableTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_api_thread(track_info: dict, encoder_getter, listener_fn, queue_getter=None) -> threading.Thread:
    """Start the HTTP API server in a daemon thread.

    Args:
        track_info: Mutable dict shared with the streamer (mutated in-place).
        encoder_getter: Callable returning the current encoder subprocess.
        listener_fn: Callable returning the current listener count.
    """
    global _track_info, _encoder_getter, _listener_fn, SERVER_START_TIME
    global _live_queue_getter
    _track_info = track_info
    _encoder_getter = encoder_getter
    _listener_fn = listener_fn
    _live_queue_getter = queue_getter
    SERVER_START_TIME = time.time()

    def _warm_and_refresh():
        """Pre-populate the show segment cache at startup, then keep it warm."""
        import ondemand as _od_mod
        while True:
            try:
                _od_mod.get_show_segment_items()
            except Exception:
                pass
            time.sleep(50)  # Refresh 10s before the 60s TTL expires

    threading.Thread(target=_warm_and_refresh, daemon=True).start()

    def _serve():
        try:
            with ReusableTCPServer(("", PORT), NowPlayingHandler) as httpd:
                httpd.serve_forever()
        except OSError as e:
            from stream_gapless import log
            log(f"API server failed to start: {e}")

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    return t
