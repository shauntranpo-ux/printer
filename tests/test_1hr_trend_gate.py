"""Tests: 1-hour macro trend gate blocks contra-trend S1 entries."""
import sys, os, time
from collections import deque
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
import asset_manager
from bot_strategy import strategy_brain_s1


def _make_prices(slope_per_sec: float, window_sec: float = 3700.0, base: float = 1600.0) -> list:
    """Build a price series covering >1hr so the 1hr trend gate has data."""
    now = time.time()
    n = int(window_sec / 10)
    return [(now - (n - i) * 10, base + slope_per_sec * (i * 10)) for i in range(n)]


def _run_s1(asset: str, prices: list, current: float, strike: float, yes_ask: float, no_ask: float):
    config = {"mode": "paper", "bot_enabled": True}
    patches = [
        patch("bot_strategy.read_config", return_value=config),
        patch("bot_strategy._is_quiet_hours", return_value=False),
        patch.object(bot_state, "_s1_pending_trades", {}),
        patch.object(bot_state, "_s1_asset_trade_times", {}),
        patch.dict(asset_manager._prices, {asset: deque(prices)}),
    ]
    # Optional state vars — patch only if they exist
    for attr, default in [
        ("_s1_window_fired", 0.0),
        ("_s1_cooldown_until", {}),
        ("_s1_consec_losses_by_asset", {}),
    ]:
        if hasattr(bot_state, attr):
            patches.append(patch.object(bot_state, attr, default))

    # Use nested with via contextlib
    import contextlib
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return strategy_brain_s1(
            current, strike, yes_ask, no_ask,
            elapsed_seconds=200, secs_left=400,
            ticker=f"KX{asset}15M-TEST", asset=asset,
        )


def test_1hr_uptrend_blocks_no_bet():
    """1-hour uptrend must block NO bets — even when 10-min is flat."""
    # 1hr upward trend overall, but flat for last 10min (so 10-min gate alone wouldn't block)
    now = time.time()
    n_long = 360    # 3600s of data at 10s intervals
    n_flat = 63     # last 630s are flat (>600s so 10-min window is entirely flat)
    rising_count = n_long - n_flat
    prices = (
        [(now - (n_long - i) * 10, 1600.0 + i * 0.05) for i in range(rising_count)]
        + [(now - (n_flat - i) * 10, 1600.0 + rising_count * 0.05) for i in range(n_flat)]
    )
    current = prices[-1][1]
    # 0.08% above strike → dislocation fires NO with sufficient edge (no_ask=30 → edge≈0.16)
    strike  = current * 1.0008

    result = _run_s1("ETH", prices, current, strike, yes_ask=55.0, no_ask=30.0)

    assert result["action"] == "skip", (
        f"Expected skip for NO bet during 1hr uptrend (10-min flat), "
        f"got action={result['action']} reasoning={result.get('reasoning')}"
    )
    assert "1hr_trend_gate" in result.get("reasoning", ""), (
        f"Expected 1hr_trend_gate in reasoning, got: {result.get('reasoning')}"
    )


def test_1hr_downtrend_blocks_yes_bet():
    """1-hour downtrend must block YES bets — even when 10-min is flat."""
    now = time.time()
    n_long = 360    # 3600s of data at 10s intervals
    n_flat = 63     # last 630s are flat (>600s so 10-min window is entirely flat)
    falling_count = n_long - n_flat
    prices = (
        [(now - (n_long - i) * 10, 1700.0 - i * 0.05) for i in range(falling_count)]
        + [(now - (n_flat - i) * 10, 1700.0 - falling_count * 0.05) for i in range(n_flat)]
    )
    current = prices[-1][1]
    # 0.08% above strike → dislocation fires YES (yes_ask=30 → edge≈0.16)
    strike  = current * 0.9992

    result = _run_s1("ETH", prices, current, strike, yes_ask=30.0, no_ask=55.0)

    assert result["action"] == "skip", (
        f"Expected skip for YES bet during 1hr downtrend, got action={result['action']}"
    )
    assert "1hr_trend_gate" in result.get("reasoning", ""), (
        f"Expected 1hr_trend_gate in reasoning, got: {result.get('reasoning')}"
    )


def test_1hr_flat_does_not_block():
    """Flat 1-hour price must not trigger the trend gate."""
    prices = _make_prices(slope_per_sec=0.0, base=1600.0)
    current = prices[-1][1]
    result = _run_s1("ETH", prices, current, current * 1.003, yes_ask=55.0, no_ask=40.0)
    assert "1hr_trend_gate" not in result.get("reasoning", ""), (
        f"Flat trend should not trigger 1hr_trend_gate: {result.get('reasoning')}"
    )


def test_1hr_gate_insufficient_data_does_not_block():
    """With <5 price points in the 1hr window, gate returns 0 (no block)."""
    now = time.time()
    sparse = [(now - 300, 1600.0), (now - 150, 1605.0), (now, 1610.0)]
    result = _run_s1("ETH", sparse, 1610.0, 1610.0 * 1.003, yes_ask=55.0, no_ask=40.0)
    assert "1hr_trend_gate" not in result.get("reasoning", ""), (
        f"Insufficient data should not trigger 1hr gate: {result.get('reasoning')}"
    )
