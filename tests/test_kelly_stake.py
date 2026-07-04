"""_kelly_stake: never above the clip, floors at min stake, scales with edge."""
import sys, os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot_strategy import _kelly_stake

CFG = {"trade_amount_dollars": 25, "kelly_sizing_enabled": True,
       "kelly_cap": 0.05, "min_stake_dollars": 5.0}


def test_never_exceeds_the_clip():
    for p in (0.55, 0.7, 0.9, 0.99):
        for c in (25, 40, 60, 80):
            assert _kelly_stake(p, c, CFG) <= 25.0 + 1e-9


def test_rich_edge_earns_the_full_clip():
    assert _kelly_stake(0.90, 60.0, CFG) == pytest.approx(25.0)


def test_thin_edge_floors_at_min_stake():
    # Barely past breakeven: quarter-Kelly tiny -> stake floored, not zeroed.
    assert _kelly_stake(0.61, 60.0, CFG) == pytest.approx(5.0)


def test_monotone_in_win_prob():
    stakes = [_kelly_stake(p, 50.0, CFG) for p in (0.55, 0.60, 0.65, 0.70, 0.75)]
    assert stakes == sorted(stakes)


def test_disabled_flag_returns_flat_clip():
    assert _kelly_stake(0.55, 50.0, {**CFG, "kelly_sizing_enabled": False}) == 25.0


def test_respects_smaller_configured_clip():
    cfg = {**CFG, "trade_amount_dollars": 10}
    assert _kelly_stake(0.95, 50.0, cfg) <= 10.0
    # Floor can never exceed the clip either.
    assert _kelly_stake(0.62, 60.0, {**cfg, "min_stake_dollars": 50.0}) <= 10.0


def test_garbage_inputs_fall_back_to_clip():
    assert _kelly_stake(None, 50.0, CFG) == 25.0
    assert _kelly_stake(0.7, None, CFG) == 25.0
    assert _kelly_stake(0.7, 0.0, CFG) == 25.0


def test_zero_clip_means_no_stake():
    # trade_amount_dollars=0 is a valid "do not trade" config; it must never
    # inflate to the $25 cap.
    assert _kelly_stake(0.9, 50.0, {**CFG, "trade_amount_dollars": 0}) == 0.0
    assert _kelly_stake(0.9, 50.0, {**CFG, "trade_amount_dollars": -3}) == 0.0


def test_malformed_config_never_raises():
    # The dashboard can persist arbitrary JSON; the hot loop must not blow up on it.
    assert _kelly_stake(0.7, 55.0, {**CFG, "kelly_cap": "0.05x"}) == 25.0
    assert _kelly_stake(0.7, 55.0, {**CFG, "min_stake_dollars": "five"}) == 25.0
    assert _kelly_stake(0.7, 55.0, {**CFG, "trade_amount_dollars": "lots"}) <= 25.0
