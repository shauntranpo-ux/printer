import numpy as np
import pytest
from strategy_a.features.funding import FundingFeatures

_RNG = np.random.default_rng(42)


def _varied_baseline(f: FundingFeatures, n: int = 25) -> None:
    """Feed n slightly-varied observations so the buffer has non-zero std."""
    rng = np.random.default_rng(0)
    for _ in range(n):
        f.compute({
            "funding_rate": float(0.0001 + rng.normal(0, 1e-5)),
            "open_interest": float(1e9 + rng.normal(0, 1e7)),
        })

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
    """crowded_long fires when both funding z-score AND OI z-score exceed thresholds."""
    f = FundingFeatures(_CFG)
    # Build a varied baseline so std > 0
    _varied_baseline(f, 25)
    # Inject extreme positive funding + high OI - both z-scores should exceed 2.0 and 1.5
    result = f.compute({"funding_rate": 10.0, "open_interest": 1e13})
    assert result["crowded_long"] == 1.0


def test_crowded_short_fires_on_low_fr_and_high_oi():
    """crowded_short fires when funding z-score is very negative AND OI z-score is high."""
    f = FundingFeatures(_CFG)
    _varied_baseline(f, 25)
    result = f.compute({"funding_rate": -10.0, "open_interest": 1e13})
    assert result["crowded_short"] == 1.0


def test_crowded_long_requires_both_conditions():
    """High funding alone (low OI) must NOT trigger crowded_long."""
    f = FundingFeatures(_CFG)
    _varied_baseline(f, 25)
    # Only funding is extreme; OI stays at baseline (low OI z-score)
    result = f.compute({"funding_rate": 10.0, "open_interest": 1e9})
    assert result["crowded_long"] == 0.0


def test_single_observation_returns_zero_zscore():
    """First observation has no variance - z-score must be 0.0 (not NaN or crash)."""
    f = FundingFeatures(_CFG)
    result = f.compute({"funding_rate": 0.0001, "open_interest": 1e9})
    assert result["funding_rate_zscore"] == 0.0
    assert result["oi_zscore"] == 0.0


def test_constant_series_returns_zero_zscore():
    """All-same values -> std=0 -> z-score must be 0.0 (not inf/nan)."""
    f = FundingFeatures(_CFG)
    for _ in range(10):
        result = f.compute({"funding_rate": 0.0001, "open_interest": 1e9})
    assert result["funding_rate_zscore"] == 0.0
    assert result["oi_zscore"] == 0.0
