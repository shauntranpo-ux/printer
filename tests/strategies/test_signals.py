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


# ── Solana health check ───────────────────────────────────────────────────

import time as _time
from unittest.mock import patch, MagicMock
from strategies.signals.solana_health import check_solana_health
import strategies.signals.solana_health as sh_module


def _reset_solana_cache():
    sh_module._cache.update({"ts": 0.0, "healthy": False, "reason": "reset"})


def test_solana_health_happy_path():
    _reset_solana_cache()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"result": "ok"}
    with patch("strategies.signals.solana_health.httpx.post",
               return_value=mock_resp):
        is_healthy, reason = check_solana_health(force=True)
        assert is_healthy is True
        assert reason == "ok"


def test_solana_health_degraded():
    _reset_solana_cache()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"result": "behind"}
    with patch("strategies.signals.solana_health.httpx.post",
               return_value=mock_resp):
        is_healthy, reason = check_solana_health(force=True)
        assert is_healthy is False
        assert "unhealthy" in reason


def test_solana_health_timeout_fails_safe():
    _reset_solana_cache()
    import httpx as rq
    with patch("strategies.signals.solana_health.httpx.post",
               side_effect=rq.TimeoutException("timeout")):
        is_healthy, reason = check_solana_health(force=True)
        assert is_healthy is False
        assert reason == "rpc_timeout"


def test_solana_health_http_error_fails_safe():
    _reset_solana_cache()
    mock_resp = MagicMock()
    mock_resp.status_code = 503
    with patch("strategies.signals.solana_health.httpx.post",
               return_value=mock_resp):
        is_healthy, reason = check_solana_health(force=True)
        assert is_healthy is False


def test_solana_health_caches_within_ttl():
    _reset_solana_cache()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"result": "ok"}
    with patch("strategies.signals.solana_health.httpx.post",
               return_value=mock_resp) as mock_post:
        check_solana_health(force=True)
        assert mock_post.call_count == 1
        check_solana_health(force=False)
        assert mock_post.call_count == 1


# ── Exhaustion fade ───────────────────────────────────────────────────────

from strategies.signals.exhaustion_fade import exhaustion_fade_adjustment


def test_exhaustion_inactive_outside_final_window():
    now = _time.time()
    prices = [(now - 59, 100.0), (now, 102.0)]
    adj, sig = exhaustion_fade_adjustment(
        prices, realized_vol_1min=0.01, seconds_left=500
    )
    assert adj == 0.0
    assert sig["exhaustion_active"] is False


def test_exhaustion_active_on_extreme_up_move():
    now = _time.time()
    prices = [(now - 59, 100.0), (now, 103.0)]  # +3% vs 1% vol = 3 sigma
    adj, sig = exhaustion_fade_adjustment(
        prices, realized_vol_1min=0.01, seconds_left=60
    )
    assert adj < 0  # fade the up move
    assert sig["exhaustion_active"] is True


def test_exhaustion_active_on_extreme_down_move():
    now = _time.time()
    prices = [(now - 59, 100.0), (now, 97.0)]  # -3% vs 1% vol = 3 sigma
    adj, sig = exhaustion_fade_adjustment(
        prices, realized_vol_1min=0.01, seconds_left=60
    )
    assert adj > 0  # rebound expectation


def test_exhaustion_inactive_on_small_move():
    now = _time.time()
    prices = [(now - 59, 100.0), (now, 100.5)]  # +0.5% vs 1% vol = 0.5 sigma
    adj, sig = exhaustion_fade_adjustment(
        prices, realized_vol_1min=0.01, seconds_left=60
    )
    assert adj == 0.0
    assert sig["exhaustion_active"] is False
