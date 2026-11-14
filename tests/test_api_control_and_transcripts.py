"""Control-endpoint restriction, transcript lookup and on-demand item IDs (api_server)."""
from __future__ import annotations

import json
from email.message import Message

import pytest

import api_server
import ondemand


def _handler(ip: str, **headers: str):
    msg = Message()
    for name, value in headers.items():
        msg[name.replace("_", "-")] = value

    class FakeHandler:
        client_address = (ip, 50000)

    FakeHandler.headers = msg
    return FakeHandler()


# --- /control and /override -------------------------------------------------

def test_admin_on_loopback_is_allowed(monkeypatch):
    monkeypatch.setattr(api_server, "_CONTROL_TOKEN", "")
    assert api_server._is_control_authorized(_handler("127.0.0.1"))
    assert api_server._is_control_authorized(_handler("::1"))


@pytest.mark.parametrize("header", ["CF_Connecting_IP", "X_Forwarded_For", "X_Real_IP", "Forwarded"])
def test_proxied_loopback_requests_are_refused(monkeypatch, header):
    # cloudflared / reverse proxies connect from loopback but forward the visitor.
    monkeypatch.setattr(api_server, "_CONTROL_TOKEN", "")
    assert not api_server._is_control_authorized(_handler("127.0.0.1", **{header: "203.0.113.9"}))


def test_lan_listeners_are_refused(monkeypatch):
    # LAN clients bypass listener auth, but must not control the broadcast.
    monkeypatch.setattr(api_server, "_CONTROL_TOKEN", "")
    assert not api_server._is_control_authorized(_handler("10.0.0.60"))


def test_control_token_allows_remote_admin(monkeypatch):
    monkeypatch.setattr(api_server, "_CONTROL_TOKEN", "s3cret")
    assert api_server._is_control_authorized(_handler("10.0.1.5", X_Writ_Control_Token="s3cret"))
    assert not api_server._is_control_authorized(_handler("10.0.1.5", X_Writ_Control_Token="wrong"))
    assert not api_server._is_control_authorized(_handler("10.0.1.5"))


def test_empty_token_never_matches(monkeypatch):
    monkeypatch.setattr(api_server, "_CONTROL_TOKEN", "")
    assert not api_server._is_control_authorized(_handler("10.0.1.5", X_Writ_Control_Token=""))


# --- item IDs ---------------------------------------------------------------

def test_item_id_for_talk_segment_paths():
    path = "/code/writ-fm/output/talk_segments/sysadmin/reddit_post_x_20261111_030737.wav"
    assert api_server._item_id_for_path(path) == "show:sysadmin:reddit_post_x_20261111_030737"
    assert api_server._item_id_for_path("/code/writ-fm/output/music_bumpers/sysadmin/b.mp3") is None
    assert api_server._item_id_for_path(None) is None


def test_queue_item_ids_do_not_mutate_streamer_state():
    seg = "/x/output/talk_segments/nosleep/story_a_20261111_000000.wav"
    state = {"current_item": {"path": seg}, "upcoming": [{"path": seg}, {"kind": "bumper_slot"}]}
    out = api_server._queue_with_item_ids(state)
    assert out["current_item"]["item_id"] == "show:nosleep:story_a_20261111_000000"
    assert out["upcoming"][0]["item_id"] == "show:nosleep:story_a_20261111_000000"
    assert "item_id" not in out["upcoming"][1]
    assert "item_id" not in state["current_item"]


# --- transcripts ------------------------------------------------------------

@pytest.fixture
def station_dirs(tmp_path, monkeypatch):
    segments = tmp_path / "talk_segments"
    scripts = tmp_path / "scripts"
    (segments / "sysadmin").mkdir(parents=True)
    scripts.mkdir()
    monkeypatch.setattr(ondemand, "SEGMENTS_DIR", segments)
    monkeypatch.setattr(api_server, "SCRIPTS_DIR", scripts)
    monkeypatch.setattr(api_server, "_ondemand", ondemand)
    return segments, scripts


def _make_segment(segments, scripts, show="sysadmin", stem="reddit_post_cve_20261114_033500",
                  seg_type="reddit_post", record_show=None, script="ROOT: Hello.\n\nSUDO: Hi."):
    (segments / show).mkdir(exist_ok=True)
    (segments / show / f"{stem}.wav").write_bytes(b"RIFF")
    (segments / show / f"{stem}.json").write_text(json.dumps({"type": seg_type, "topic": "CVE thread"}))
    stamp = stem[-15:]
    (scripts / f"talk_{seg_type}_{stamp}.json").write_text(json.dumps({
        "show_id": record_show or show, "topic": "CVE thread", "host_name": "Root",
        "script": script, "source_value": "https://reddit.com/r/sysadmin/x",
    }))
    return f"show:{show}:{stem}"


def test_transcript_found(station_dirs):
    item_id = _make_segment(*station_dirs)
    status, body = api_server.get_transcript(item_id)
    assert status == 200
    assert body["script"].startswith("ROOT: Hello.")
    assert body["title"] == "CVE thread"
    assert body["source_value"] == "https://reddit.com/r/sysadmin/x"
    assert body["is_source_transcript"] is False


def test_transcript_rejects_record_from_another_show(station_dirs):
    item_id = _make_segment(*station_dirs, record_show="nosleep")
    assert api_server.get_transcript(item_id)[0] == 404


def test_transcript_missing_or_empty(station_dirs):
    segments, scripts = station_dirs
    assert api_server.get_transcript("show:sysadmin:nope_20261114_000000")[0] == 404
    item_id = _make_segment(segments, scripts, stem="joke_x_20261114_010101", seg_type="joke", script="  ")
    assert api_server.get_transcript(item_id)[0] == 404
    assert api_server.get_transcript("")[0] == 400


@pytest.mark.parametrize("item_id", [
    "show:..:..",
    "show:../..:etc",
    "show:sysadmin:../../secret",
    "show:sysadmin:..",
    "show::stem",
])
def test_segment_path_refuses_traversal(station_dirs, item_id):
    segments, _ = station_dirs
    (segments.parent / "secret.wav").write_bytes(b"RIFF")
    assert ondemand.get_segment_path(item_id) is None


# --- admin devices (per-token "admin" flag) ---------------------------------

@pytest.fixture
def token_file(tmp_path, monkeypatch):
    path = tmp_path / "listener_tokens.json"
    path.write_text(json.dumps([
        {"token": "admin-tok", "label": "Mark iPhone", "admin": True},
        {"token": "plain-tok", "label": "Friend"},
    ]))
    monkeypatch.setattr(api_server, "_LISTENER_TOKENS_FILE", path)
    monkeypatch.setattr(api_server, "_CONTROL_TOKEN", "")
    return path


def test_admin_token_allows_control_through_the_tunnel(token_file):
    h = _handler("10.0.0.200", Cookie="writ_listener_token=admin-tok", CF_Connecting_IP="203.0.113.9")
    assert api_server._is_control_authorized(h)
    assert api_server._control_actor(h) == "listener 'Mark iPhone'"


def test_plain_and_unknown_tokens_are_refused(token_file):
    assert not api_server._is_control_authorized(_handler("10.0.0.200", Cookie="writ_listener_token=plain-tok"))
    assert not api_server._is_control_authorized(_handler("10.0.0.200", Cookie="writ_listener_token=nope"))


def test_admin_flag_must_be_literally_true(token_file):
    token_file.write_text(json.dumps([{"token": "t", "label": "x", "admin": "yes"}]))
    assert not api_server._is_control_authorized(_handler("10.0.0.200", Cookie="writ_listener_token=t"))


def test_me_reports_admin_status(token_file):
    assert api_server.get_me(_handler("10.0.0.200", Cookie="writ_listener_token=admin-tok")) == \
        {"label": "Mark iPhone", "admin": True}
    assert api_server.get_me(_handler("10.0.0.200", Cookie="writ_listener_token=plain-tok")) == \
        {"label": "Friend", "admin": False}
    assert api_server.get_me(_handler("10.0.0.60")) == {"label": None, "admin": False}


def test_scheduled_shows_excludes_unscheduled_and_reports_current(monkeypatch):
    import schedule as schedule_module
    from types import SimpleNamespace as NS

    def show(show_id, name):
        return NS(show_id=show_id, name=name, description=f"{name} desc", host="host")

    fake = NS(
        shows={"nosleep": show("nosleep", "/r/nosleep"), "sysadmin": show("sysadmin", "/r/sysadmin"),
               "briefing_ai": show("briefing_ai", "AI Briefing")},
        base=[NS(show_id="sysadmin"), NS(show_id="nosleep")],
        overrides=[],
        resolve=lambda now: NS(show_id="sysadmin"),
    )
    monkeypatch.setattr(schedule_module, "load_schedule", lambda _config: fake)
    monkeypatch.setattr(api_server, "get_live_override", lambda: None)
    result = api_server.get_scheduled_shows()
    assert [s["show_id"] for s in result["shows"]] == ["nosleep", "sysadmin"]
    assert result["current_show_id"] == "sysadmin"
