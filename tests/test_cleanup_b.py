"""Tests for Approach B cleanup: config wiring + ghost param removal."""
import inspect
import time
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _prices_eth(n: int = 40, base: float = 95_000.0, step: float = 100.0):
    """(ts, price) pairs — ETH price trending up, old enough for EMA window."""
    now = time.time()
    return [(now - (n - i) * 20, base + i * step) for i in range(n)]


# ---------------------------------------------------------------------------
# Test 1: S1 config.json override is applied
# ---------------------------------------------------------------------------

def test_s1_config_override_skips_on_overridden_min_dist():
    """
    When s1_config.ETH.min_dist is set to 0.9999 in config,
    a trade with abs_pct=5.5% should get a s1_dist_gate skip even though
    the hardcoded default (0.003) would pass.

    This fails BEFORE the config-merge patch in strategy_brain_s1.
    After the patch it passes because cfg["min_dist"] == 0.9999.
    """
    from bot_strategy import strategy_brain_s1
    import asset_manager

    cfg = {
        "min_entry_price_cents": 20,
        "max_entry_price_cents": 76,
        "s1_config": {
            "ETH": {"min_dist": 0.9999},  # impossibly high → forces dist_gate
        },
    }

    prices = _prices_eth(40)

    with patch("bot_strategy.read_config", return_value=cfg), \
         patch.dict(asset_manager._prices, {"ETH": prices}):
        result = strategy_brain_s1(
            btc_price=95_000.0,
            strike=90_000.0,    # abs_pct ≈ 5.6% — normally passes 0.003 default
            yes_ask=60.0, no_ask=40.0,
            elapsed_seconds=30.0, secs_left=300.0,   # 5 min — within [3,12] gate
            ticker="KXETH-25MAY15-T90000", asset="ETH",
        )

    assert result["action"] == "skip", f"Expected skip, got: {result}"
    assert "s1_dist_gate" in result["reasoning"], (
        f"Expected s1_dist_gate, got: {result['reasoning']}"
    )


# ---------------------------------------------------------------------------
# Test 2: S2 config.json override is applied
# ---------------------------------------------------------------------------

def test_s2_config_override_skips_on_overridden_min_dist():
    """
    When s2_config.ETH.min_dist is set to 0.9999 in config,
    a trade with abs_pct=5.5% should get a s2_dist_gate skip even though
    the hardcoded default (0.003) would pass.

    This fails BEFORE the config-merge patch in strategy_brain_s2.
    """
    from bot_strategy import strategy_brain_s2
    import asset_manager

    cfg = {
        "min_entry_price_cents": 20,
        "max_entry_price_cents": 76,
        "s2_config": {
            "ETH": {"min_dist": 0.9999},  # impossibly high → forces dist_gate
        },
    }

    prices = _prices_eth(40)

    with patch("bot_strategy.read_config", return_value=cfg), \
         patch.dict(asset_manager._prices, {"ETH": prices}):
        result = strategy_brain_s2(
            btc_price=95_000.0,
            strike=90_000.0,    # abs_pct ≈ 5.6%
            yes_ask=60.0, no_ask=40.0,
            elapsed_seconds=30.0, secs_left=300.0,
            ticker="KXETH-25MAY15-T90000", asset="ETH",
        )

    assert result["action"] == "skip", f"Expected skip, got: {result}"
    assert "s2_dist_gate" in result["reasoning"], (
        f"Expected s2_dist_gate, got: {result['reasoning']}"
    )


# ---------------------------------------------------------------------------
# Test 3: S2 ghost params removed from signature
# ---------------------------------------------------------------------------

def test_s2_signature_has_no_ghost_params():
    """
    strategy_brain_s2 must NOT accept the ghost params that were always
    ignored: min_ev_base, vol_gate_thresh, kalshi_fee, min_reward_cents,
    max_risk_reward_ratio, max_entry_price_cents.

    This fails BEFORE Task 6 removes them from the signature.
    """
    from bot_strategy import strategy_brain_s2

    sig = inspect.signature(strategy_brain_s2)
    ghost = {
        "min_ev_base", "vol_gate_thresh", "kalshi_fee",
        "min_reward_cents", "max_risk_reward_ratio",
        "max_entry_price_cents",  # reads from config internally, not this param
    }
    present = ghost & set(sig.parameters)
    assert not present, f"Ghost params still in strategy_brain_s2 signature: {present}"
