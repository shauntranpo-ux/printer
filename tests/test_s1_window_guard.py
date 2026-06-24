# tests/test_s1_window_guard.py
"""Cross-asset S1 window guard: after ETH fires, SOL must be blocked for 300s."""
import sys, os, time
from collections import deque
from unittest.mock import patch
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
import asset_manager
from bot_strategy import strategy_brain_s1


def _make_prices(base=60.0, n=63):
    now = time.time()
    return [(now - (n - i) * 10, base) for i in range(n)]


def _run_s1_for_asset(asset, base_price, asset_trade_times):
    config = {"mode": "paper", "bot_enabled": True}
    strike = base_price * 1.005
    prices = _make_prices(base=base_price)
    patches = [
        patch("bot_strategy.read_config", return_value=config),
        patch("bot_strategy._is_quiet_hours", return_value=False),
        patch.object(bot_state, "_s1_pending_trades", {}),
        patch.object(bot_state, "_s1_asset_trade_times", dict(asset_trade_times)),
        patch.object(bot_state, "_s1_cooldown_until", {}),
        patch.object(bot_state, "_s1_consec_losses_by_asset", {}),
        patch.dict(asset_manager._prices, {asset: deque(prices)}),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches: stack.enter_context(p)
        return strategy_brain_s1(
            prices[-1][1], strike,
            38.0, 57.0,
            elapsed_seconds=200, secs_left=400,
            ticker=f"KX{asset}15M-TEST", asset=asset,
        )


def test_window_guard_blocks_sol_when_eth_just_fired():
    """SOL S1 must return skip when ETH fired S1 within 300s."""
    eth_fired_30s_ago = time.time() - 30
    result = _run_s1_for_asset("SOL", 60.0, {"ETH": [eth_fired_30s_ago]})
    assert result["action"] == "skip", (
        f"SOL S1 must be blocked when ETH fired 30s ago, got action={result['action']}"
    )
    assert "s1_window_guard" in result.get("reasoning", ""), (
        f"Expected s1_window_guard in reasoning, got: {result.get('reasoning')}"
    )


def test_window_guard_allows_sol_when_eth_fired_long_ago():
    """SOL S1 must pass window guard when ETH fired >300s ago."""
    eth_fired_400s_ago = time.time() - 400
    result = _run_s1_for_asset("SOL", 60.0, {"ETH": [eth_fired_400s_ago]})
    assert "s1_window_guard" not in result.get("reasoning", ""), (
        f"ETH fired 400s ago — SOL must not be blocked: {result.get('reasoning')}"
    )


def test_window_guard_does_not_block_btc():
    """BTC S1 must never be blocked by another asset's window guard."""
    eth_fired_30s_ago = time.time() - 30
    btc_prices = _make_prices(base=105000.0)
    config = {"mode": "paper", "bot_enabled": True}
    patches = [
        patch("bot_strategy.read_config", return_value=config),
        patch("bot_strategy._is_quiet_hours", return_value=False),
        patch.object(bot_state, "_s1_pending_trades", {}),
        patch.object(bot_state, "_s1_asset_trade_times", {"ETH": [eth_fired_30s_ago]}),
        patch.object(bot_state, "_s1_cooldown_until", {}),
        patch.object(bot_state, "_s1_consec_losses_by_asset", {}),
        patch.object(bot_state, "btc_prices", deque(btc_prices)),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches: stack.enter_context(p)
        result = strategy_brain_s1(
            btc_prices[-1][1], btc_prices[-1][1] * 1.005,
            38.0, 57.0,
            elapsed_seconds=200, secs_left=400,
            ticker="KXBTC15M-TEST", asset="BTC",
        )
    assert "s1_window_guard" not in result.get("reasoning", ""), (
        f"BTC must never be blocked by cross-asset window guard: {result.get('reasoning')}"
    )


def test_window_guard_allows_when_no_recent_trades():
    """SOL S1 must pass when _s1_asset_trade_times is empty."""
    result = _run_s1_for_asset("SOL", 60.0, {})
    assert "s1_window_guard" not in result.get("reasoning", ""), (
        f"Empty trade times must not trigger window guard: {result.get('reasoning')}"
    )
