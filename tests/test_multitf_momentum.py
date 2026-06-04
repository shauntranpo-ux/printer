"""Tests for multi-timeframe momentum composite signal."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot_strategy import _s1_multitf_momentum


def _rising_prices(base=2000.0, rate=0.005, seconds=300.0) -> list:
    now = time.time()
    n = int(seconds / 5)
    return [(now - seconds + i * (seconds / n), base * (1 + rate * i / n)) for i in range(n + 1)]


def _falling_prices(base=2000.0, rate=0.005, seconds=300.0) -> list:
    now = time.time()
    n = int(seconds / 5)
    return [(now - seconds + i * (seconds / n), base * (1 - rate * i / n)) for i in range(n + 1)]


def test_multitf_returns_yes_on_sustained_uptrend():
    prices = _rising_prices(rate=0.005, seconds=300.0)
    side, score = _s1_multitf_momentum(prices, min_momentum=0.001)
    assert side == "yes", f"Expected yes on uptrend, got {side} score={score}"
    assert score > 0.30, f"Score {score} too low"


def test_multitf_returns_no_on_sustained_downtrend():
    prices = _falling_prices(rate=0.005, seconds=300.0)
    side, score = _s1_multitf_momentum(prices, min_momentum=0.001)
    assert side == "no", f"Expected no on downtrend, got {side} score={score}"


def test_multitf_returns_none_on_short_data():
    prices = [(time.time() - i, 2000.0) for i in range(5)]
    side, score = _s1_multitf_momentum(prices, min_momentum=0.001)
    assert side is None, f"Expected None on short data, got {side}"


def test_multitf_returns_none_on_flat():
    prices = [(time.time() - i * 5, 2000.0) for i in range(60, 0, -1)]
    side, score = _s1_multitf_momentum(prices, min_momentum=0.001)
    assert side is None, f"Expected None on flat prices, got {side}"
