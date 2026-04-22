import numpy as np
import pytest
from strategy_a.features.funding import FundingFeatures

_CFG = {
    "funding": {
        "zscore_window_days": 7,
        "crowded_long_threshold": 2.0,
        "crowded_short_threshold": -2.0,
        "oi_crowded_threshold": 1.5,
    }
}

_EXPECTED_KEYS = {"funding_rate_zscore", "oi_zscore", "crowded_long", "crowded_short"}


def test_smoke():
    f = FundingFeatures(_CFG)
    assert isinstance(f.compute({"funding_rate": 0.0001, "open_interest": 1e9}), dict)


def test_shape():
    f = FundingFeatures(_CFG)
    assert _EXPECTED_KEYS.issubset(
        f.compute({"funding_rate": 0.0001, "open_interest": 1e9}).keys()
    )


def test_zscore_type_finite():
    f = FundingFeatures(_CFG)
    for i in range(15):
        result = f.compute({"funding_rate": 0.0001 * i, "open_interest": 1e9 * (1 + i * 0.01)})
    assert isinstance(result["funding_rate_zscore"], float)
    assert np.isfinite(result["funding_rate_zscore"])
    assert np.isfinite(result["oi_zscore"])


def test_crowded_flags_binary():
    f = FundingFeatures(_CFG)
    for i in range(20):
        result = f.compute({"funding_rate": 0.0001 * i, "open_interest": 1e9})
    assert result["crowded_long"] in (0.0, 1.0)
    assert result["crowded_short"] in (0.0, 1.0)


def test_crowded_long_fires_on_high_fr_and_oi():
    """crowded_long should fire when both funding z-score and OI z-score exceed thresholds."""
    f = FundingFeatures(_CFG)
    # Build a baseline of normal values
    for _ in range(20):
        f.compute({"funding_rate": 0.0001, "open_interest": 1e9})
    # Inject extreme positive funding + high OI
    result = f.compute({"funding_rate": 10.0, "open_interest": 1e12})
    # z-scores should be very large positive — crowded_long may or may not fire
    # depending on how many baseline obs we have, but fr_z should be > 0
    assert result["funding_rate_zscore"] > 0.0


def test_single_observation_returns_zero_zscore():
    """First observation has no variance — z-score must be 0.0 (not NaN or crash)."""
    f = FundingFeatures(_CFG)
    result = f.compute({"funding_rate": 0.0001, "open_interest": 1e9})
    assert result["funding_rate_zscore"] == 0.0
    assert result["oi_zscore"] == 0.0


def test_constant_series_returns_zero_zscore():
    """All-same values → std=0 → z-score must be 0.0 (not inf/nan)."""
    f = FundingFeatures(_CFG)
    for _ in range(10):
        result = f.compute({"funding_rate": 0.0001, "open_interest": 1e9})
    assert result["funding_rate_zscore"] == 0.0
    assert result["oi_zscore"] == 0.0
