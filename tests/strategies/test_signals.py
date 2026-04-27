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


# â”€â”€ Rolling beta â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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


# â”€â”€ Variance ratio â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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


# â”€â”€ Kalshi velocity â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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


# â”€â”€ Ratio divergence â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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


# â”€â”€ Solana health check â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

import time as _time
from unittest.mock import patch, MagicMock
from strategies.signals.solana_health import check_solana_health
import strategies.signals.solana_health as sh_module


def _reset_solana_cache():
    sh_module._cache.update({"ts": 0.0, "healthy": False, "reason": "reset"})


def _make_urlopen_mock(status: int = 200, body: dict | None = None):
    """Return a MagicMock configured as a urllib.request.urlopen context manager."""
    import json as _json
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = _json.dumps(body or {}).encode("utf-8")
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return cm


def test_solana_health_happy_path():
    _reset_solana_cache()
    cm = _make_urlopen_mock(200, {"result": "ok"})
    with patch("strategies.signals.solana_health.urllib.request.urlopen",
               return_value=cm):
        is_healthy, reason = check_solana_health(force=True)
        assert is_healthy is True
        assert reason == "ok"


def test_solana_health_degraded():
    _reset_solana_cache()
    cm = _make_urlopen_mock(200, {"result": "behind"})
    with patch("strategies.signals.solana_health.urllib.request.urlopen",
               return_value=cm):
        is_healthy, reason = check_solana_health(force=True)
        assert is_healthy is False
        assert "unhealthy" in reason


def test_solana_health_timeout_fails_safe():
    _reset_solana_cache()
    with patch("strategies.signals.solana_health.urllib.request.urlopen",
               side_effect=TimeoutError("timeout")):
        is_healthy, reason = check_solana_health(force=True)
        assert is_healthy is False
        assert "TimeoutError" in reason


def test_solana_health_http_error_fails_safe():
    _reset_solana_cache()
    import urllib.error
    err = urllib.error.HTTPError(
        url="http://x", code=503, msg="Service Unavailable",
        hdrs=None, fp=None,
    )
    with patch("strategies.signals.solana_health.urllib.request.urlopen",
               side_effect=err):
        is_healthy, reason = check_solana_health(force=True)
        assert is_healthy is False


def test_solana_health_caches_within_ttl():
    _reset_solana_cache()
    cm = _make_urlopen_mock(200, {"result": "ok"})
    with patch("strategies.signals.solana_health.urllib.request.urlopen",
               return_value=cm) as mock_urlopen:
        check_solana_health(force=True)
        assert mock_urlopen.call_count == 1
        check_solana_health(force=False)
        assert mock_urlopen.call_count == 1


# â”€â”€ Exhaustion fade â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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


# â”€â”€ Correlation monitor â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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


# â”€â”€ Volume spike + extreme velocity â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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


# â”€â”€ Event calendar â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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


# â”€â”€ Session awareness â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
