"""WRIT_GEN_WINDOW: scheduled talk generation only in a nightly window, serial, briefings last."""

from __future__ import annotations

from datetime import datetime

import pytest

import scheduler as sch


def _at(hh, mm):
    return datetime(2026, 10, 20, hh, mm)


def test_window_parsing_and_bounds(monkeypatch):
    monkeypatch.setenv("WRIT_GEN_WINDOW", "03:00-05:00")
    w = sch._generation_window()
    assert w == (180, 300)
    assert sch._window_minutes_left(w, _at(2, 59)) is None
    assert sch._window_minutes_left(w, _at(3, 0)) == 120
    assert sch._window_minutes_left(w, _at(4, 50)) == 10
    assert sch._window_minutes_left(w, _at(5, 0)) is None
    assert sch._window_minutes_left((23 * 60, 60), _at(0, 30)) == 30     # crosses midnight
    monkeypatch.setenv("WRIT_GEN_WINDOW", "nonsense")
    assert sch._generation_window() is None


@pytest.fixture
def harness(monkeypatch):
    shows = {
        "briefing_news": {"generation": {"talk": {"enabled": True, "cadence": "daily", "time_after": "07:30",
                                                  "min_inventory": 0, "target_inventory": 1}}},
        "sysadmin": {"generation": {"talk": {"enabled": True, "cadence": "continuous", "min_inventory": 8, "target_inventory": 12}}},
        "nosleep": {"generation": {"talk": {"enabled": True, "cadence": "continuous", "min_inventory": 8, "target_inventory": 12}}},
    }
    inventory = {"briefing_news": 0, "sysadmin": 10, "nosleep": 12}
    started = []
    monkeypatch.setattr(sch, "_load_schedule", lambda: {"shows": shows})
    monkeypatch.setattr(sch, "_build_generation_env", lambda: {})
    monkeypatch.setattr(sch, "_count_inventory", lambda d, sid: inventory[sid])
    monkeypatch.setattr(sch.state, "backoff_state", lambda *a: (None, 0))
    monkeypatch.setattr(sch.state, "add_log", lambda *a, **k: None)
    monkeypatch.setattr(sch.state, "active_jobs", {})
    monkeypatch.setattr(sch.state, "last_run", lambda *a: None)

    class FakeThread:
        def __init__(self, target, args, daemon):
            self.args = args
        def start(self):
            started.append(self.args[0])

    monkeypatch.setattr(sch.threading, "Thread", FakeThread)
    monkeypatch.setenv("WRIT_GEN_WINDOW", "03:00-05:00")
    return inventory, started


def test_outside_the_window_nothing_is_scheduled(monkeypatch, harness):
    inventory, started = harness
    monkeypatch.setattr(sch, "_station_now", lambda: _at(14, 0))
    inventory["sysadmin"] = 2
    sch._check_and_generate({})
    assert started == []


def test_inside_the_window_one_stock_show_at_a_time_and_briefings_wait(monkeypatch, harness):
    inventory, started = harness
    monkeypatch.setattr(sch, "_station_now", lambda: _at(3, 5))
    sch._check_and_generate({})
    assert started == ["sysadmin"]          # 10 < 12 fills to target; the briefing waits its turn


def test_briefings_run_once_stock_is_full(monkeypatch, harness):
    inventory, started = harness
    inventory["sysadmin"] = 12
    monkeypatch.setattr(sch, "_station_now", lambda: _at(3, 40))
    sch._check_and_generate({})
    assert started == ["briefing_news"]      # time_after 07:30 does not apply inside the window


def test_briefings_get_the_last_minutes_even_if_stock_is_short(monkeypatch, harness):
    inventory, started = harness
    monkeypatch.setattr(sch, "_station_now", lambda: _at(4, 0))      # 60 min left: stock first
    sch._check_and_generate({})
    assert started == ["sysadmin"]
    started.clear()
    monkeypatch.setattr(sch, "_station_now", lambda: _at(4, 30))     # 30 min left: briefings first
    sch._check_and_generate({})
    assert started == ["briefing_news"]


def test_a_running_talk_job_holds_the_window(monkeypatch, harness):
    inventory, started = harness
    monkeypatch.setattr(sch, "_station_now", lambda: _at(3, 10))
    monkeypatch.setattr(sch.state, "active_jobs", {"j": {"show_id": "nosleep", "type": "talk"}})
    sch._check_and_generate({})
    assert started == []


def test_unset_window_keeps_round_the_clock_behaviour(monkeypatch, harness):
    inventory, started = harness
    monkeypatch.delenv("WRIT_GEN_WINDOW")
    monkeypatch.setattr(sch, "_station_now", lambda: _at(14, 0))
    inventory["sysadmin"] = 8
    sch._check_and_generate({})
    assert "sysadmin" in started


def _music(cadence="weekly"):
    return {"enabled": True, "cadence": cadence, "min_inventory": 4, "target_inventory": 6}


def test_outside_the_window_music_waits(monkeypatch, harness):
    inventory, started = harness
    sch._load_schedule()["shows"]["sysadmin"]["generation"]["music"] = _music()
    monkeypatch.setattr(sch, "_station_now", lambda: _at(10, 20))
    sch._check_and_generate({})
    assert started == []


def test_inside_the_window_music_goes_first_one_show_at_a_time(monkeypatch, harness):
    inventory, started = harness
    shows = sch._load_schedule()["shows"]
    shows["sysadmin"]["generation"]["music"] = _music()
    shows["nosleep"]["generation"]["music"] = _music()
    monkeypatch.setattr(sch, "_station_now", lambda: _at(3, 0))
    sch._check_and_generate({})
    assert started == ["sysadmin"]          # its music, not its talk; nosleep\x27s music and all talk wait


def test_talk_waits_while_a_music_job_runs_then_resumes(monkeypatch, harness):
    inventory, started = harness
    monkeypatch.setattr(sch, "_station_now", lambda: _at(3, 1))
    sch.state.active_jobs["m"] = {"show_id": "nosleep", "type": "music"}
    sch._check_and_generate({})
    assert started == []
    sch.state.active_jobs.clear()
    sch._check_and_generate({})
    assert started == ["sysadmin"]


def test_music_due_mid_window_waits_for_the_running_talk_job(monkeypatch, harness):
    inventory, started = harness
    sch._load_schedule()["shows"]["nosleep"]["generation"]["music"] = _music()
    monkeypatch.setattr(sch, "_station_now", lambda: _at(3, 30))
    sch.state.active_jobs["t"] = {"show_id": "sysadmin", "type": "talk"}
    sch._check_and_generate({})
    assert started == []


def test_without_a_window_music_runs_whenever_it_is_due(monkeypatch, harness):
    inventory, started = harness
    monkeypatch.delenv("WRIT_GEN_WINDOW")
    shows = sch._load_schedule()["shows"]
    shows["nosleep"]["generation"]["music"] = _music()
    monkeypatch.setattr(sch, "_station_now", lambda: _at(10, 20))
    sch._check_and_generate({})
    assert "nosleep" in started


def test_a_failed_music_run_does_not_restart_the_cadence(monkeypatch, harness):
    recorded = []
    monkeypatch.setattr(sch.state, "record_run", lambda *a: recorded.append(a))
    monkeypatch.setattr(sch.state, "record_failure", lambda *a: 900)
    monkeypatch.setattr(sch.state, "record_success", lambda *a: recorded.append(("success",)))

    class Proc:
        stdout = iter(["[ace-step] HTTP 503: not enough memory"])
        returncode = 1
        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(sch.subprocess, "Popen", lambda *a, **k: Proc())
    sch._run_music_generation("sysadmin", 2, "ambient", {}, {})
    assert recorded == []
    Proc.returncode = 0
    Proc.stdout = iter(["Saved"])
    sch._run_music_generation("sysadmin", 2, "ambient", {}, {})
    assert recorded == [("sysadmin", "music"), ("success",)]
