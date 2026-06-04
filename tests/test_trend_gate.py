"""Tests for 10-minute trend direction filter."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot_strategy import _trend_direction


def _make_prices(slope_per_sec: float, window_sec: float = 620.0, base: float = 2000.0) -> list:
    now = time.time()
    n = int(window_sec / 10)
    return [(now - (n - i) * 10, base + slope_per_sec * (i * 10)) for i in range(n)]


def test_trend_direction_detects_uptrend():
    prices = _make_prices(slope_per_sec=0.05)
    assert _trend_direction(prices) == 1, "Expected uptrend +1"


def test_trend_direction_detects_downtrend():
    prices = _make_prices(slope_per_sec=-0.05)
    assert _trend_direction(prices) == -1, "Expected downtrend -1"


def test_trend_direction_returns_zero_on_insufficient_data():
    assert _trend_direction([]) == 0
    assert _trend_direction([(time.time(), 100.0)]) == 0


def test_trend_direction_uses_only_last_600s():
    now = time.time()
    # ancient series ends at now-700 (well outside the 600s window) with upward slope
    ancient = [(now - 1000 + i * 10, 1000 + i * 2) for i in range(30)]
    # recent series covers now-290 to now with a clear downward slope
    recent  = [(now - 290 + i * 10, 2000 - i * 5) for i in range(30)]
    prices = ancient + recent
    result = _trend_direction(prices, window_seconds=600.0)
    assert result == -1, f"Should detect recent downtrend, got {result}"
