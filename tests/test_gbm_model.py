"""Tests for _s1_certainty_win_prob GBM model floor and ceiling."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot_strategy import _s1_certainty_win_prob


def test_gbm_floor_is_50_pct_not_52():
    """At zero distance from strike, GBM cert is 0.50 — floor must not artificially boost it."""
    result = _s1_certainty_win_prob(dist_pct=0.0001, secs_left=450.0, asset="ETH")
    assert result <= 0.51, (
        f"GBM at near-zero distance must return ~0.50 (no edge). "
        f"Got {result:.4f} — floor must be 0.50, not 0.52."
    )


def test_gbm_ceiling_above_75_pct_for_deep_otm():
    """Deep OTM + little time should return > 0.75 — current 0.75 ceiling is too conservative."""
    # 2% distance, 60 seconds left — GBM cert should be ~0.95
    result = _s1_certainty_win_prob(dist_pct=0.020, secs_left=60.0, asset="BTC")
    assert result >= 0.80, (
        f"Deep OTM (2%) with 60s left must have win_prob >= 0.80. "
        f"Got {result:.4f} — ceiling must be raised from 0.75 to 0.85."
    )


def test_gbm_floor_is_050():
    """GBM floor must be exactly 0.50 after fix."""
    # At extremely low distance, tanh → 0, cert → 0.50
    result = _s1_certainty_win_prob(dist_pct=0.00001, secs_left=450.0, asset="ETH")
    assert abs(result - 0.50) < 0.02, (
        f"GBM floor must be 0.50. Got {result:.4f}"
    )


def test_gbm_ceiling_is_085():
    """GBM ceiling must be 0.85 after fix (not 0.75)."""
    # 5% distance, 30s left → cert ≈ 1.0, clipped to 0.85
    result = _s1_certainty_win_prob(dist_pct=0.050, secs_left=30.0, asset="BTC")
    assert result >= 0.85 - 0.001, (
        f"GBM ceiling must be 0.85. Got {result:.4f}"
    )
    assert result <= 0.851, (
        f"GBM ceiling must not exceed 0.85. Got {result:.4f}"
    )
