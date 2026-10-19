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
