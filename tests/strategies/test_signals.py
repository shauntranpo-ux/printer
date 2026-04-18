import math
import pytest

from strategies.signals.rolling_beta import (
    compute_beta_from_returns, log_returns_from_prices
)
from strategies.signals.variance_ratio import (
    variance_ratio, variance_ratio_to_regime
)
from strategies.signals.ratio_divergence import ratio_z_score
from strategies.signals.kalshi_velocity import (
    contract_velocity, velocity_adjustment_for_side
)


# ── Rolling beta ──────────────────────────────────────────────────────────

def test_beta_perfect_correlation_equals_one():
    rets = [0.01, -0.02, 0.005, 0.003, -0.015] * 30
    beta = compute_beta_from_returns(rets, rets)
    assert abs(beta - 1.0) < 1e-9


def test_beta_doubled_returns_equals_two():
    btc_rets = [0.01, -0.02, 0.005, 0.003, -0.015] * 30
    eth_rets = [r * 2 for r in btc_rets]
    beta = compute_beta_from_returns(eth_rets, btc_rets)
    assert abs(beta - 2.0) < 1e-9


def test_beta_insufficient_data_returns_none():
    assert compute_beta_from_returns([0.01, 0.02], [0.01, 0.02]) is None


def test_beta_zero_variance_returns_none():
    rets = [0.01] * 200
    btc = [0.0] * 200
    assert compute_beta_from_returns(rets, btc) is None


def test_log_returns_from_prices():
    prices = [(1, 100.0), (2, 101.0), (3, 99.0)]
    rets = log_returns_from_prices(prices)
    assert len(rets) == 2
    assert abs(rets[0] - math.log(1.01)) < 1e-9


# ── Variance ratio ────────────────────────────────────────────────────────

def test_vr_iid_random_walk_near_one():
    import random
    random.seed(42)
    rets = [random.gauss(0, 0.01) for _ in range(500)]
    vr = variance_ratio(rets, q=5)
    assert 0.7 <= vr <= 1.3


def test_vr_trending_greater_than_one():
    # AR(1) with strong positive autocorrelation (rho=0.7) produces VR > 1
    import random
    random.seed(42)
    rets = []
    r = 0.0
    for _ in range(500):
        r = 0.7 * r + random.gauss(0, 0.01)
        rets.append(r)
    vr = variance_ratio(rets, q=5)
    assert vr > 1.0


def test_vr_insufficient_data_returns_none():
    assert variance_ratio([0.01, 0.02, 0.03], q=5) is None


def test_vr_regime_classification():
    assert variance_ratio_to_regime(1.5) == "momentum"
    assert variance_ratio_to_regime(0.5) == "reversion"
    assert variance_ratio_to_regime(1.0) == "neutral"
    assert variance_ratio_to_regime(None) == "neutral"


# ── Kalshi velocity ───────────────────────────────────────────────────────

def test_velocity_rising_detected():
    hist = [(i, 50 + i) for i in range(40)]
    assert contract_velocity(hist) == "rising"


def test_velocity_falling_detected():
    hist = [(i, 90 - i) for i in range(40)]
    assert contract_velocity(hist) == "falling"


def test_velocity_flat_detected():
    hist = [(i, 60.0) for i in range(40)]
    assert contract_velocity(hist) == "flat"


def test_velocity_insufficient_history_is_flat():
    hist = [(0, 50.0)]
    assert contract_velocity(hist) == "flat"


def test_velocity_adjustment_signs():
    assert velocity_adjustment_for_side("rising", "yes") > 0
    assert velocity_adjustment_for_side("rising", "no") < 0
    assert velocity_adjustment_for_side("falling", "yes") < 0
    assert velocity_adjustment_for_side("falling", "no") > 0
    assert velocity_adjustment_for_side("flat", "yes") == 0.0


# ── Ratio divergence ──────────────────────────────────────────────────────

def test_ratio_z_score_stable_ratio_is_none():
    eth = [(i * 60, 2000.0) for i in range(60)]
    btc = [(i * 60, 50000.0) for i in range(60)]
    z = ratio_z_score(eth, btc, lookback_minutes=60)
    # Constant ratio -> std=0 -> returns None
    assert z is None


def test_ratio_z_score_rising_eth_positive_z():
    eth = [(i * 60, 2000.0 + i * 0.5) for i in range(60)]
    btc = [(i * 60, 50000.0) for i in range(60)]
    z = ratio_z_score(eth, btc, lookback_minutes=60)
    assert z is not None
    assert z > 1.0


def test_ratio_z_score_insufficient_data_returns_none():
    eth = [(0, 2000.0)]
    btc = [(0, 50000.0)]
    assert ratio_z_score(eth, btc) is None
