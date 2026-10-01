"""Profit-lock tiers: arm at progress %, ratchet stop to BE+lock %."""

from core.models import Direction
from core.strategy.mes_5orb.exits import (
    active_profit_lock_fraction,
    apply_profit_lock_stop,
    normalize_profit_lock_tiers,
    profit_lock_stop_price,
    target_progress,
    tighten_stop_to_be,
)

TIERS = (
    (0.75, 0.35),
    (0.80, 0.50),
    (0.90, 0.75),
    (0.95, 0.85),
    (0.975, 0.90),
)


def test_target_progress_short():
    assert target_progress(Direction.SHORT, 100.0, 80.0, 85.0) == 0.75
    assert target_progress(Direction.SHORT, 100.0, 80.0, 90.0) == 0.5


def test_target_progress_long():
    assert target_progress(Direction.LONG, 100.0, 120.0, 115.0) == 0.75


def test_profit_lock_stop_short_35pct():
    # lock 35% of 100→80 span = 7pts → stop 93 (BE+35%)
    assert profit_lock_stop_price(Direction.SHORT, 100.0, 80.0, lock_fraction=0.35) == 93.0


def test_profit_lock_stop_long_35pct():
    assert profit_lock_stop_price(Direction.LONG, 100.0, 120.0, lock_fraction=0.35) == 107.0


def test_active_tier_picks_highest_reached():
    assert active_profit_lock_fraction(0.74, TIERS) is None
    assert active_profit_lock_fraction(0.75, TIERS) == 0.35
    assert active_profit_lock_fraction(0.80, TIERS) == 0.50
    assert active_profit_lock_fraction(0.90, TIERS) == 0.75
    assert active_profit_lock_fraction(0.95, TIERS) == 0.85
    assert active_profit_lock_fraction(0.975, TIERS) == 0.90
    assert active_profit_lock_fraction(1.0, TIERS) == 0.90


def test_apply_profit_lock_arms_at_75pct_short():
    armed = apply_profit_lock_stop(
        Direction.SHORT,
        100.0,
        80.0,
        85.0,
        110.0,
        tiers=TIERS,
    )
    assert armed == 93.0  # 35% lock


def test_apply_profit_lock_steps_to_50pct_at_80():
    # 80% progress on 100→80 = mark 84
    armed = apply_profit_lock_stop(
        Direction.SHORT,
        100.0,
        80.0,
        84.0,
        110.0,
        tiers=TIERS,
    )
    assert armed == 90.0  # 50% of 20pt span


def test_apply_profit_lock_steps_to_75pct_at_90():
    armed = apply_profit_lock_stop(
        Direction.SHORT,
        100.0,
        80.0,
        82.0,  # 90%
        110.0,
        tiers=TIERS,
    )
    assert armed == 85.0  # 75% lock


def test_apply_profit_lock_not_before_arm():
    same = apply_profit_lock_stop(
        Direction.SHORT,
        100.0,
        80.0,
        90.0,
        110.0,
        tiers=TIERS,
    )
    assert same == 110.0


def test_apply_profit_lock_never_loosens():
    keep = apply_profit_lock_stop(
        Direction.SHORT,
        100.0,
        80.0,
        85.0,
        92.0,
        tiers=TIERS,
    )
    assert keep == 92.0


def test_tighten_be_does_not_undo_profit_lock_short():
    assert tighten_stop_to_be(Direction.SHORT, 100.0, 93.0) == 93.0
    assert tighten_stop_to_be(Direction.SHORT, 100.0, 110.0) == 100.0


def test_normalize_merges_legacy_pair():
    assert normalize_profit_lock_tiers(None, arm_fraction=0.75, lock_fraction=0.35) == [
        (0.75, 0.35)
    ]


def test_asia_example_levels():
    entry, target = 7737.25, 7705.5
    span = entry - target  # 31.75
    arm_px = entry - 0.75 * span
    lock_px = entry - 0.35 * span
    assert abs(arm_px - 7713.4375) < 1e-6
    assert abs(lock_px - (7737.25 - 0.35 * 31.75)) < 1e-6
    stop = apply_profit_lock_stop(
        Direction.SHORT,
        entry,
        target,
        7713.25,
        7782.75,
        tiers=TIERS,
    )
    assert abs(stop - lock_px) < 1e-6
    # At 80% path → 50% lock
    mark_80 = entry - 0.80 * span
    stop80 = apply_profit_lock_stop(
        Direction.SHORT, entry, target, mark_80, 7782.75, tiers=TIERS
    )
    assert abs(stop80 - (entry - 0.50 * span)) < 1e-6
