"""Tests for strategy_c.probability.digital_call."""
import math
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "strategies"))

from strategy_c.probability.digital_call import (
    binary_call_probability,
    flat_vol_to_integrated_variance,
)


class TestFlatVolToIntegratedVariance:
    def test_basic(self):
        assert flat_vol_to_integrated_variance(0.02, 900.0) == pytest.approx(0.02 ** 2 * 900.0)

    def test_zero_sigma(self):
        assert flat_vol_to_integrated_variance(0.0, 900.0) == 0.0

    def test_zero_dt(self):
        assert flat_vol_to_integrated_variance(0.5, 0.0) == 0.0


class TestBinaryCallProbability:
    def test_atm_high_vol_near_half(self):
        # ATM (S == K) with non-trivial variance; result should be near 0.5
        iv = flat_vol_to_integrated_variance(0.001, 900.0)
        p = binary_call_probability(100.0, 100.0, iv, 900.0)
        assert 0.45 < p < 0.55

    def test_atm_very_low_vol_close_to_half(self):
        # Very low vol -> d2 ~ 0 -> N(d2) ~ 0.5
        iv = flat_vol_to_integrated_variance(1e-9, 60.0)
        p = binary_call_probability(100.0, 100.0, iv, 60.0)
        assert 0.49 < p < 0.51

    def test_deep_itm_near_one(self):
        # Strike far below spot with very low vol -> very high probability
        # sigma=0.001/s, dt=3600s -> IV=0.0036; log(200/50)=1.386; d2~23 -> N(d2)~1.0
        iv = flat_vol_to_integrated_variance(0.001, 3600.0)
        p = binary_call_probability(200.0, 50.0, iv, 3600.0)
        assert p > 0.99

    def test_deep_otm_near_zero(self):
        # Strike far above spot with very low vol -> very low probability
        iv = flat_vol_to_integrated_variance(0.001, 3600.0)
        p = binary_call_probability(50.0, 200.0, iv, 3600.0)
        assert p < 0.01

    def test_expired_above_strike(self):
        # time_to_expiry_seconds <= 0 with S > K -> deterministic 1.0
        p = binary_call_probability(100.0, 99.0, 0.1, 0.0)
        assert p == 1.0

    def test_expired_below_strike(self):
        # time_to_expiry_seconds <= 0 with S <= K -> deterministic 0.0
        p = binary_call_probability(99.0, 100.0, 0.1, 0.0)
        assert p == 0.0

    def test_expired_at_strike(self):
        # S == K at expiry: convention is 0.0 (not strictly above)
        p = binary_call_probability(100.0, 100.0, 0.1, 0.0)
        assert p == 0.0

    def test_degenerate_zero_price_returns_sentinel(self):
        p = binary_call_probability(0.0, 100.0, 0.1, 900.0)
        assert p == 0.5

    def test_degenerate_zero_variance_returns_sentinel(self):
        p = binary_call_probability(100.0, 100.0, 0.0, 900.0)
        assert p == 0.5

    def test_degenerate_negative_variance_returns_sentinel(self):
        p = binary_call_probability(100.0, 100.0, -0.1, 900.0)
        assert p == 0.5

    def test_probability_bounds(self):
        iv = flat_vol_to_integrated_variance(0.02, 3600.0)
        for spot in [50.0, 100.0, 200.0]:
            for strike in [50.0, 100.0, 200.0]:
                p = binary_call_probability(spot, strike, iv, 3600.0)
                assert 0.0 <= p <= 1.0

    def test_higher_spot_higher_prob(self):
        # Holding K fixed, higher S -> higher P(S_T > K)
        iv = flat_vol_to_integrated_variance(0.02, 3600.0)
        p_low = binary_call_probability(90.0, 100.0, iv, 3600.0)
        p_high = binary_call_probability(110.0, 100.0, iv, 3600.0)
        assert p_high > p_low

    def test_lower_strike_higher_prob(self):
        # Holding S fixed, lower K -> higher P(S_T > K)
        iv = flat_vol_to_integrated_variance(0.02, 3600.0)
        p_low_k = binary_call_probability(100.0, 80.0, iv, 3600.0)
        p_high_k = binary_call_probability(100.0, 120.0, iv, 3600.0)
        assert p_low_k > p_high_k

    def test_rfr_zero_same_as_default(self):
        iv = flat_vol_to_integrated_variance(0.02, 3600.0)
        p1 = binary_call_probability(100.0, 100.0, iv, 3600.0)
        p2 = binary_call_probability(100.0, 100.0, iv, 3600.0, risk_free_rate=0.0)
        assert p1 == p2
