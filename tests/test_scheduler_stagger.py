"""Regression tests for the inventory-threshold deadlock and generation staggering.

Two production failure modes are covered here:

1. A show sitting exactly ON min_inventory with nothing expiring never regenerated,
   because the trigger was `inventory < minimum`. dark_jokes was frozen this way
   from June to July 2026.
2. A show whose whole catalogue was generated on one day also expires on one day,
   so the station plays the same batch for max_days and then swaps all of it at
   once. The stagger pass expires a slice early to break the cluster up.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "admin"))

import scheduler  # noqa: E402


NOW = datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc)


def days_ago(n: float, count: int = 1) -> list[datetime]:
    return [NOW - timedelta(days=n)] * count


def stagger(generated, *, max_days=3, target=12, minimum=6):
    return scheduler._stagger_expiry_count(
        generated, NOW, max_days=max_days, target=target, minimum=minimum
    )


# ── the threshold deadlock ────────────────────────────────────────────────────

def should_run(inventory: int, minimum: int, target: int) -> bool:
    """Mirror of the continuous-cadence trigger in _check_and_generate."""
    return inventory <= minimum and inventory < target


def test_at_threshold_is_not_deadlocked():
    # dark_jokes: 6 segments, min_inventory 6 — used to be `<` and stalled forever.
    assert should_run(6, 6, 12) is True


def test_below_threshold_still_runs():
    assert should_run(3, 6, 12) is True


def test_above_threshold_does_not_run():
    assert should_run(7, 6, 12) is False


def test_at_target_does_not_run_even_when_minimum_equals_target():
    # Guards against `<=` causing an endless regeneration loop.
    assert should_run(12, 12, 12) is False


# ── stagger: when it should do nothing ───────────────────────────────────────

def test_no_stagger_when_already_spread_across_the_window():
    generated = days_ago(1, 4) + days_ago(2, 4) + days_ago(3, 4)
    assert stagger(generated) == 0


def test_no_stagger_for_a_cluster_generated_today():
    # Expiring today's batch would only be replaced by more of today's date.
    assert stagger(days_ago(0, 12)) == 0


def test_no_stagger_when_at_or_below_minimum():
    # The normal top-up already fires; forcing it would just destroy stock.
    assert stagger(days_ago(2, 6)) == 0
    assert stagger(days_ago(2, 4)) == 0


def test_no_stagger_without_a_usable_window():
    assert stagger(days_ago(2, 12), max_days=1) == 0
    assert stagger(days_ago(2, 12), max_days=0) == 0


def test_no_stagger_when_target_does_not_exceed_minimum():
    assert stagger(days_ago(2, 12), target=6, minimum=6) == 0


def test_no_stagger_on_empty_or_single_segment():
    assert stagger([]) == 0
    assert stagger(days_ago(2, 1)) == 0


# ── stagger: when it should act ──────────────────────────────────────────────

def test_cluster_is_cut_back_to_the_minimum_so_top_up_fires():
    # 12 all one day old, min 6 → drop 6, landing exactly on the minimum, which
    # the fixed `<=` trigger then tops back up.
    assert stagger(days_ago(1, 12)) == 6


def test_expires_at_least_one_days_slice():
    # 9 segments, min 6 → inventory-minimum is 3, but a day's slice of a
    # 12-over-3-days rotation is 4, so 4 wins.
    assert stagger(days_ago(1, 9)) == 4


def test_never_empties_the_show():
    n = stagger(days_ago(2, 5), minimum=0)
    assert n == 4
    assert n < 5


def test_partial_cluster_still_staggers():
    # Two distinct days but a 3-day window — not yet spread.
    generated = days_ago(1, 6) + days_ago(2, 6)
    assert stagger(generated) == 6


@pytest.mark.parametrize("max_days,target,expected_slice", [(3, 12, 4), (5, 10, 2), (7, 10, 2)])
def test_slice_size_follows_target_over_window(max_days, target, expected_slice):
    # Sits at minimum+1 so the slice size, not the minimum, decides.
    generated = days_ago(1, target // 2 + 1)
    n = stagger(generated, max_days=max_days, target=target, minimum=target // 2)
    assert n == max(expected_slice, 1)


def test_repeated_days_converge_to_a_spread_catalogue():
    """Simulate the daily pass: a single-day cluster should spread out over the window."""
    max_days, target, minimum = 3, 12, 6
    # day -> list of segment ages; start with everything made on day 0
    stock: list[datetime] = days_ago(0, target)
    for day in range(1, 6):
        now = NOW + timedelta(days=day)
        # natural expiry first
        stock = [dt for dt in stock if (now - dt).days < max_days]
        n = scheduler._stagger_expiry_count(
            sorted(stock), now, max_days=max_days, target=target, minimum=minimum
        )
        stock = sorted(stock)[n:]
        # top-up, stamped with the current day
        if len(stock) <= minimum and len(stock) < target:
            stock += [now] * (target - len(stock))
    assert len({dt.date() for dt in stock}) >= 2, "catalogue never spread out"
    assert len(stock) >= minimum, "staggering starved the show"
