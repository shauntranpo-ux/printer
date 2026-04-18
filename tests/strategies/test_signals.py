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


# ── Correlation monitor ───────────────────────────────────────────────────

from strategies.signals.correlation_monitor import (
    rolling_correlation, btc_signal_weight_from_correlation
)


def test_correlation_perfect_positive():
    asset_prices = [(i * 60, 100.0 + i * 0.1) for i in range(60)]
    btc_prices = [(i * 60, 50000.0 + i * 10) for i in range(60)]
    corr = rolling_correlation(asset_prices, btc_prices, lookback_minutes=60)
    assert corr is not None
    assert corr > 0.95


def test_correlation_zero_when_one_constant():
    asset_prices = [(i * 60, 100.0) for i in range(60)]
    btc_prices = [(i * 60, 50000.0 + i * 10) for i in range(60)]
    corr = rolling_correlation(asset_prices, btc_prices)
    assert corr is None


def test_correlation_insufficient_data():
    asset_prices = [(0, 100.0), (60, 101.0)]
    btc_prices = [(0, 50000.0), (60, 50100.0)]
    assert rolling_correlation(asset_prices, btc_prices) is None


def test_btc_weight_ramps_from_zero_to_max():
    assert btc_signal_weight_from_correlation(0.0, max_weight=0.30) == 0.0
    assert btc_signal_weight_from_correlation(0.3, max_weight=0.30) == 0.0
    w_05 = btc_signal_weight_from_correlation(0.5, max_weight=0.30)
    w_07 = btc_signal_weight_from_correlation(0.7, max_weight=0.30)
    assert 0 < w_05 < w_07
    assert w_07 == 0.30


def test_btc_weight_none_returns_middle():
    w = btc_signal_weight_from_correlation(None, max_weight=0.30)
    assert w == 0.15


# ── Volume spike + extreme velocity ───────────────────────────────────────

from strategies.signals.volume_spike import detect_volume_spike
from strategies.signals.kalshi_velocity import extreme_velocity_event


def test_volume_spike_inactive_on_calm_data():
    hist = [(i * 60, 100.0, 1000.0) for i in range(90)]
    is_spike, direction, ret = detect_volume_spike(hist, lookback_minutes=60)
    assert is_spike is False


def test_volume_spike_insufficient_data():
    hist = [(0, 100.0, 1000.0)]
    is_spike, direction, ret = detect_volume_spike(hist, lookback_minutes=60)
    assert is_spike is False


def test_extreme_velocity_detects_up():
    hist = [(i, 50 + i) for i in range(40)]
    is_extreme, direction = extreme_velocity_event(hist, lookback_samples=30)
    assert is_extreme is True
    assert direction == "up"


def test_extreme_velocity_detects_down():
    hist = [(i, 90 - i) for i in range(40)]
    is_extreme, direction = extreme_velocity_event(hist, lookback_samples=30)
    assert is_extreme is True
    assert direction == "down"


def test_extreme_velocity_inactive_on_small_move():
    hist = [(i, 60.0 + (i % 2) * 0.1) for i in range(40)]
    is_extreme, direction = extreme_velocity_event(hist, lookback_samples=30)
    assert is_extreme is False


# ── Event calendar ────────────────────────────────────────────────────────

from strategies.signals.event_calendar import EventCalendar
import json
from pathlib import Path


def test_event_calendar_empty_returns_inactive(tmp_path):
    p = tmp_path / "events.json"
    p.write_text(json.dumps({"events": []}))
    cal = EventCalendar(path=p)
    active, reason = cal.is_event_active()
    assert active is False


def test_event_calendar_active_within_window(tmp_path):
    import time
    now = time.time()
    p = tmp_path / "events.json"
    p.write_text(json.dumps({
        "events": [
            {
                "date": __import__("datetime").datetime.fromtimestamp(
                    now - 300, tz=__import__("datetime").timezone.utc
                ).isoformat(),
                "reason": "test_event",
                "severity": "high",
            }
        ]
    }))
    cal = EventCalendar(path=p)
    active, reason = cal.is_event_active(now=now)
    assert active is True
    assert "test_event" in reason


def test_event_calendar_outside_window(tmp_path):
    import time
    now = time.time()
    p = tmp_path / "events.json"
    p.write_text(json.dumps({
        "events": [
            {
                "date": __import__("datetime").datetime.fromtimestamp(
                    now - 7200, tz=__import__("datetime").timezone.utc
                ).isoformat(),
                "reason": "old_event",
                "severity": "low",
            }
        ]
    }))
    cal = EventCalendar(path=p)
    active, reason = cal.is_event_active(now=now)
    assert active is False


def test_event_calendar_missing_file_ok(tmp_path):
    p = tmp_path / "does_not_exist.json"
    cal = EventCalendar(path=p)
    active, reason = cal.is_event_active()
    assert active is False


def test_event_calendar_corrupt_file_ok(tmp_path):
    p = tmp_path / "events.json"
    p.write_text("this is not json at all {{{")
    cal = EventCalendar(path=p)
    active, reason = cal.is_event_active()
    assert active is False


# ── Session awareness ─────────────────────────────────────────────────────

from strategies.signals.session_awareness import (
    current_session, session_min_ev_multiplier
)
from datetime import datetime, timezone as _tz


def test_session_weekend_detected():
    sat = datetime(2026, 4, 11, 12, 0, 0, tzinfo=_tz.utc).timestamp()
    assert current_session(sat) == "weekend"
    sun = datetime(2026, 4, 12, 20, 0, 0, tzinfo=_tz.utc).timestamp()
    assert current_session(sun) == "weekend"


def test_session_us_afternoon_detected():
    ts = datetime(2026, 4, 14, 19, 0, 0, tzinfo=_tz.utc).timestamp()
    assert current_session(ts) == "us_afternoon"


def test_session_normal_detected():
    ts = datetime(2026, 4, 15, 9, 0, 0, tzinfo=_tz.utc).timestamp()
    assert current_session(ts) == "normal"


def test_session_multipliers():
    assert session_min_ev_multiplier("normal") == 1.0
    assert session_min_ev_multiplier("weekend") == 1.25
    assert session_min_ev_multiplier("us_afternoon") == 1.25


# ── Idiosyncratic detector ────────────────────────────────────────────────

from strategies.signals.idiosyncratic_detector import detect_idiosyncratic_mode


def test_idiosyncratic_insufficient_data():
    doge = [(0, 0.1)]
    btc = [(0, 50000.0)]
    is_idio, sig = detect_idiosyncratic_mode(doge, btc, beta=1.3)
    assert is_idio is False
    assert "insufficient" in sig["reason"]


def test_idiosyncratic_normal_move_not_flagged():
    now = _time.time()
    doge_prices = []
    btc_prices = []
    doge_price = 0.10
    btc_price = 50000.0
    for i in range(90):
        ts = now - (90 - i) * 60
        btc_ret = 0.0005 * (1 if i % 2 == 0 else -1)
        btc_price = btc_price * (1 + btc_ret)
        doge_ret = 1.3 * btc_ret
        doge_price = doge_price * (1 + doge_ret)
        btc_prices.append((ts, btc_price))
        doge_prices.append((ts, doge_price))
    is_idio, sig = detect_idiosyncratic_mode(doge_prices, btc_prices, beta=1.3)
    assert is_idio is False


def test_idiosyncratic_divergent_move_flagged():
    import random
    random.seed(42)
    now = _time.time()
    doge_prices = []
    btc_prices = []
    doge_price = 0.10
    btc_price = 50000.0
    for i in range(90):
        ts = now - (90 - i) * 60
        btc_ret = random.gauss(0, 0.0001)
        btc_price = btc_price * (1 + btc_ret)
        if i < 75:
            doge_ret = 1.3 * btc_ret + random.gauss(0, 0.0001)
        else:
            doge_ret = 0.003  # +0.3%/min with BTC flat
        doge_price = doge_price * (1 + doge_ret)
        btc_prices.append((ts, btc_price))
        doge_prices.append((ts, doge_price))
    is_idio, sig = detect_idiosyncratic_mode(doge_prices, btc_prices, beta=1.3)
    assert is_idio is True
    assert sig["divergence_sigma"] > 2.5


# ── BV3 lookup ────────────────────────────────────────────────────────────

from unittest.mock import patch as _patch
from strategies.signals.bv3_lookup import bv3_p_yes


@_patch("bot._win_prob_for_asset")
def test_bv3_p_yes_above_strike_uses_same_side(mock_bv3):
    mock_bv3.return_value = 0.80
    p = bv3_p_yes("BTC", 100500.0, 100000.0, 600.0)
    assert p == 0.80


@_patch("bot._win_prob_for_asset")
def test_bv3_p_yes_below_strike_flips(mock_bv3):
    mock_bv3.return_value = 0.80
    p = bv3_p_yes("BTC", 99500.0, 100000.0, 600.0)
    assert abs(p - 0.20) < 1e-9


@_patch("bot._win_prob_for_asset", return_value=None)
def test_bv3_p_yes_returns_none_when_lookup_fails(mock_bv3):
    p = bv3_p_yes("BTC", 100500.0, 100000.0, 600.0)
    assert p is None


def test_bv3_p_yes_invalid_strike_returns_none():
    p = bv3_p_yes("BTC", 100500.0, 0.0, 600.0)
    assert p is None
