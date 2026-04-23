"""Tests for strategy_c.features.moneyness."""
import math
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "strategies"))

from strategy_c.features.moneyness import compute_moneyness_features


_CONFIG = {
    "moneyness": {
        "deep_itm_log_moneyness_cutoff": -0.02,
        "itm_log_moneyness_cutoff": -0.005,
        "atm_log_moneyness_cutoff": 0.005,
        "otm_log_moneyness_cutoff": 0.02,
    }
}


class TestComputeMoneynessFeatures:
    def test_at_the_money(self):
        feat = compute_moneyness_features(100.0, 100.0, 0.5, 3600.0, _CONFIG)
        assert feat["log_moneyness"] == pytest.approx(0.0)
        assert feat["moneyness_bucket"] == "atm"
        assert feat["distance_to_spot_bps"] == pytest.approx(0.0)

    def test_deep_otm_far_above_spot(self):
        # Strike much higher than spot → deep_otm
        feat = compute_moneyness_features(100.0, 150.0, 0.5, 3600.0, _CONFIG)
        assert feat["log_moneyness"] > 0
        assert feat["moneyness_bucket"] == "deep_otm"

    def test_deep_itm_far_below_spot(self):
        # Strike much lower than spot → deep_itm (log(K/S) << 0)
        feat = compute_moneyness_features(100.0, 50.0, 0.5, 3600.0, _CONFIG)
        assert feat["log_moneyness"] < 0
        assert feat["moneyness_bucket"] == "deep_itm"

    def test_slight_itm(self):
        # Strike slightly below spot: log(99/100) ≈ -0.01 → itm (between -0.02 and -0.005)
        feat = compute_moneyness_features(100.0, 99.0, 0.5, 3600.0, _CONFIG)
        assert feat["moneyness_bucket"] == "itm"

    def test_slight_otm(self):
        # Strike slightly above spot: log(101/100) ≈ +0.01 → otm (between 0.005 and 0.02)
        feat = compute_moneyness_features(100.0, 101.0, 0.5, 3600.0, _CONFIG)
        assert feat["moneyness_bucket"] == "otm"

    def test_log_moneyness_formula(self):
        spot, strike = 100.0, 110.0
        feat = compute_moneyness_features(spot, strike, 0.5, 3600.0, _CONFIG)
        assert feat["log_moneyness"] == pytest.approx(math.log(strike / spot))

    def test_distance_bps_formula(self):
        spot, strike = 100.0, 103.0
        feat = compute_moneyness_features(spot, strike, 0.5, 3600.0, _CONFIG)
        expected_bps = (strike / spot - 1.0) * 10_000.0
        assert feat["distance_to_spot_bps"] == pytest.approx(expected_bps)

    def test_distance_sigma_positive(self):
        feat = compute_moneyness_features(100.0, 105.0, 0.5, 3600.0, _CONFIG)
        assert feat["distance_to_spot_sigma"] > 0

    def test_distance_sigma_zero_tte(self):
        # time_to_expiry_seconds = 0 → sigma_expiry = 0 → distance_sigma = 0 (no division)
        feat = compute_moneyness_features(100.0, 110.0, 0.5, 0.0, _CONFIG)
        assert feat["distance_to_spot_sigma"] == 0.0

    def test_all_buckets_covered(self):
        buckets_seen = set()
        for strike in [50.0, 99.0, 99.6, 100.0, 100.5, 101.5, 120.0]:
            feat = compute_moneyness_features(100.0, strike, 0.5, 3600.0, _CONFIG)
            buckets_seen.add(feat["moneyness_bucket"])
        assert buckets_seen == {"deep_itm", "itm", "atm", "otm", "deep_otm"}

    def test_returns_expected_keys(self):
        feat = compute_moneyness_features(100.0, 100.0, 0.5, 3600.0, _CONFIG)
        assert set(feat.keys()) == {
            "log_moneyness", "moneyness_bucket",
            "distance_to_spot_bps", "distance_to_spot_sigma",
        }
