"""Unit tests for compute_15m_signal (D3-hybrid)."""
import collections
import time
import pytest

from strategies.features import MarketFeatures
from strategies.signals.fifteen_min_signal import compute_15m_signal


def _make_features(prices, current_price, strike, rv=0.001, seconds_left=600.0):
    dq = collections.deque(maxlen=3600)
    ts = time.time()
    for i, p in enumerate(prices):
        dq.append((ts - (len(prices) - i) * 60.0, p))
    return MarketFeatures(
        asset="BTC", ticker="BTC-2026-01-01", timestamp=ts,
        current_price=current_price, strike=strike, btc_price=current_price,
        seconds_left=seconds_left, elapsed_seconds=300.0,
        yes_ask=55, no_ask=48, yes_bid=50, no_bid=44,
        spread_yes=5, spread_no=4, prices_60m=dq, realized_vol_1min=rv,
    )

def _uptrend_prices(n=60, start=100.0, step=0.5):
    return [start + i * step for i in range(n)]

def _downtrend_prices(n=60, start=100.0, step=0.5):
    return [start - i * step for i in range(n)]

def test_insufficient_prices_returns_none():
    features = _make_features([100.0] * 20, 100.0, 98.0)
    assert compute_15m_signal(features) is None

def test_uptrend_gives_no():
    # Mean-reversion signal: uptrend → extended → expect reversion down → NO
    prices = _uptrend_prices(60, 90.0, 0.5)
    features = _make_features(prices, prices[-1], 100.0, rv=0.0005)
    result = compute_15m_signal(features)
    if result is not None:
        side, raw_p = result
        assert side == "no"
        assert 0.0 <= raw_p <= 1.0

def test_downtrend_gives_yes():
    # Mean-reversion signal: downtrend → depressed → expect bounce up → YES
    prices = _downtrend_prices(60, 110.0, 0.5)
    features = _make_features(prices, prices[-1], 100.0, rv=0.0005)
    result = compute_15m_signal(features)
    if result is not None:
        side, raw_p = result
        assert side == "yes"
        assert 0.0 <= raw_p <= 1.0

def test_flat_prices_may_return_none():
    prices = [100.0] * 60
    features = _make_features(prices, 100.0, 100.0, rv=0.0001)
    assert compute_15m_signal(features) is None

def test_zero_vol_returns_none():
    prices = _uptrend_prices(60)
    features = _make_features(prices, prices[-1], 90.0, rv=0.0, seconds_left=600.0)
    assert compute_15m_signal(features) is None

def test_raw_p_in_unit_interval():
    prices = _uptrend_prices(60, 80.0, 0.8)
    features = _make_features(prices, prices[-1], 100.0, rv=0.001)
    result = compute_15m_signal(features)
    if result is not None:
        _, raw_p = result
        assert 0.0 <= raw_p <= 1.0
