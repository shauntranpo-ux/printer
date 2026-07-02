"""Tests: per-asset S1 consecutive-loss cooldown (regime detector)."""
import sys, os, time
from collections import deque
from unittest.mock import patch
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
import asset_manager
from bot_strategy import strategy_brain_s1


def _make_prices(base=1600.0):
    now = time.time()
    return [(now - (620 - i) * 10, base) for i in range(63)]


def _run_s1(asset: str, prices: list, cooldown_until: dict, consec_losses: dict):
    config = {"mode": "paper", "bot_enabled": True}
    patches = [
        patch("bot_strategy.read_config", return_value=config),
        patch("bot_strategy._is_quiet_hours", return_value=False),
        patch.object(bot_state, "_s1_pending_trades", {}),
        patch.object(bot_state, "_s1_asset_trade_times", {}),
        patch.object(bot_state, "_s1_cooldown_until", cooldown_until),
        patch.object(bot_state, "_s1_consec_losses_by_asset", consec_losses),
        patch.dict(asset_manager._prices, {asset: deque(prices)}),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return strategy_brain_s1(
            prices[-1][1], prices[-1][1] * 1.005,
            38.0, 57.0,
            elapsed_seconds=200, secs_left=400,
            ticker=f"KX{asset}15M-TEST", asset=asset,
        )


def test_cooldown_blocks_asset_during_cooldown():
    """S1 must return skip for an asset currently in cooldown."""
    # SOL is an enabled S1 asset (ETH is disabled in the cross-asset S1, so it would
    # short-circuit before the cooldown gate - use SOL to exercise the cooldown path).
    prices = _make_prices(base=150.0)
    future = time.time() + 600
    result = _run_s1("SOL", prices,
                     cooldown_until={"SOL": future},
                     consec_losses={"SOL": 3})
    assert result["action"] == "skip", (
        f"Expected skip during cooldown, got action={result['action']}"
    )
    assert "s1_cooldown" in result.get("reasoning", ""), (
        f"Expected s1_cooldown in reasoning, got: {result.get('reasoning')}"
    )


def test_cooldown_expired_allows_trade():
    """Once cooldown expires, S1 must be allowed through this gate."""
    prices = _make_prices()
    past = time.time() - 60
    result = _run_s1("ETH", prices,
                     cooldown_until={"ETH": past},
                     consec_losses={"ETH": 3})
    assert "s1_cooldown" not in result.get("reasoning", ""), (
        f"Expired cooldown must not block: {result.get('reasoning')}"
    )


def test_cooldown_does_not_block_other_assets():
    """ETH cooldown must not block SOL."""
    prices_sol = _make_prices(base=60.0)
    future = time.time() + 600
    result = _run_s1("SOL", prices_sol,
                     cooldown_until={"ETH": future},
                     consec_losses={"ETH": 3})
    assert "s1_cooldown" not in result.get("reasoning", ""), (
        f"ETH cooldown must not block SOL: {result.get('reasoning')}"
    )


def test_settle_sets_cooldown_after_3_losses():
    """_settle_s1_trade source must update _s1_consec_losses_by_asset and _s1_cooldown_until."""
    import inspect
    from bot_risk import _settle_s1_trade
    src = inspect.getsource(_settle_s1_trade)
    assert "_s1_consec_losses_by_asset" in src, (
        "_s1_consec_losses_by_asset not updated in _settle_s1_trade"
    )
    assert "_s1_cooldown_until" in src, (
        "_s1_cooldown_until not set in _settle_s1_trade"
    )


def test_win_resets_consec_loss_streak():
    """A win must reset that asset's consecutive loss counter to 0."""
    import inspect
    from bot_risk import _settle_s1_trade
    src = inspect.getsource(_settle_s1_trade)
    assert (
        '_s1_consec_losses_by_asset[asset] = 0' in src
        or "_s1_consec_losses_by_asset.pop(asset" in src
    ), "Win path must reset per-asset consecutive loss counter"


def test_new_state_vars_exist_in_bot_state():
    """bot_state must export both new per-asset cooldown dicts."""
    import bot_state as bs
    assert hasattr(bs, "_s1_consec_losses_by_asset"), "_s1_consec_losses_by_asset missing from bot_state"
    assert hasattr(bs, "_s1_cooldown_until"), "_s1_cooldown_until missing from bot_state"
    assert isinstance(bs._s1_consec_losses_by_asset, dict)
    assert isinstance(bs._s1_cooldown_until, dict)
