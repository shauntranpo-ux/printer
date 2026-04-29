import math
import pytest
from strategies.baseline import brownian_bridge_prob_above


def test_at_the_money_is_fifty_fifty():
    assert brownian_bridge_prob_above(100.0, 100.0, 300, 0.002) == 0.5


def test_far_above_strike_with_little_time_near_certain():
    # 1% above strike, 60 seconds left, 0.1% per-min vol
    # Very unlikely to reverse
    p = brownian_bridge_prob_above(101.0, 100.0, 60, 0.001)
    assert p > 0.95


def test_far_below_strike_with_little_time_near_certain():
    p = brownian_bridge_prob_above(99.0, 100.0, 60, 0.001)
    assert p < 0.05


def test_high_volatility_pushes_probability_toward_half():
    # Same distance, but high vol = more uncertainty
    p_low_vol = brownian_bridge_prob_above(101.0, 100.0, 300, 0.001)
    p_high_vol = brownian_bridge_prob_above(101.0, 100.0, 300, 0.01)
    assert p_low_vol > p_high_vol  # high vol more uncertain -> closer to 0.5


def test_more_time_remaining_more_uncertainty():
    # Currently above, vol is what it is, more time = more chance of reversal
    p_short = brownian_bridge_prob_above(100.5, 100.0, 60, 0.002)
    p_long = brownian_bridge_prob_above(100.5, 100.0, 900, 0.002)
    assert p_short > p_long


def test_zero_volatility_is_deterministic():
    assert brownian_bridge_prob_above(101.0, 100.0, 300, 0.0) == 0.999
    assert brownian_bridge_prob_above(99.0, 100.0, 300, 0.0) == 0.001


def test_time_expired_settles_deterministically():
    assert brownian_bridge_prob_above(101.0, 100.0, 0, 0.002) == 0.999
    assert brownian_bridge_prob_above(99.0, 100.0, 0, 0.002) == 0.001


def test_output_bounds():
    """Output must always be in [0.001, 0.999]."""
    for S, K, t, v in [
        (1000.0, 100.0, 300, 0.002),  # extreme above
        (100.0, 1000.0, 300, 0.002),  # extreme below
        (100.0, 100.0, 1, 0.00001),   # tiny vol
    ]:
        p = brownian_bridge_prob_above(S, K, t, v)
        assert 0.001 <= p <= 0.999
