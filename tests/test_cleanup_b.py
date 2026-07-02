"""Tests for Approach B cleanup: config wiring + ghost param removal."""
import inspect
import time
from unittest.mock import patch


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

def test_s1_config_override_skips_on_overridden_time_max():
    """
    s1_config.SOL override must merge into cfg. Setting time_max to an impossibly
    small value forces an early s1_time_gate skip even though the default (13.0)
    would pass at 5 minutes left — proving the per-asset config merge is applied.
    """
    from bot_strategy import strategy_brain_s1
    import asset_manager
    import bot_state as bs
    from collections import deque

    cfg = {
        "mode": "paper",
        "quiet_hours_enabled": False,
        "s1_config": {"SOL": {"time_max": 0.001}},  # impossibly small → forces time_gate
    }
    now = time.time()
    prices = deque([(now - (40 - i) * 2, 150.0) for i in range(40)])

    with patch("bot_strategy.read_config", return_value=cfg), \
         patch.object(bs, "_s1_pending_trades", {}), \
         patch.object(bs, "_s1_asset_trade_times", {}), \
         patch.object(bs, "_s1_cooldown_until", {}), \
         patch.dict(asset_manager._prices, {"SOL": prices}):
        result = strategy_brain_s1(
            btc_price=150.0, strike=149.0,
            yes_ask=45.0, no_ask=55.0,
            elapsed_seconds=600.0, secs_left=300.0,   # 5 min left
            ticker="KXSOL-CFG", asset="SOL",
        )

    assert result["action"] == "skip", f"Expected skip, got: {result}"
    assert "s1_time_gate" in result["reasoning"], (
        f"Expected s1_time_gate, got: {result['reasoning']}"
    )


# ---------------------------------------------------------------------------
# Test 2: S2 config.json override is applied
# ---------------------------------------------------------------------------

def test_s2_config_override_skips_on_overridden_time_max():
    """
    s2_config.SOL override must merge into cfg. Setting time_max to an impossibly
    small value forces an early s2_time_gate skip — proving the per-asset config
    merge is applied in strategy_brain_s2.
    """
    from bot_strategy import strategy_brain_s2
    import asset_manager
    from collections import deque

    cfg = {
        "mode": "paper",
        "quiet_hours_enabled": False,
        "s2_config": {"SOL": {"time_max": 0.001}},  # impossibly small → forces time_gate
    }
    now = time.time()
    prices = deque([(now - (40 - i) * 2, 150.5) for i in range(40)])

    with patch("bot_strategy.read_config", return_value=cfg), \
         patch.dict(asset_manager._prices, {"SOL": prices}):
        result = strategy_brain_s2(
            btc_price=150.5, strike=150.0,
            yes_ask=45.0, no_ask=55.0,
            elapsed_seconds=600.0, secs_left=300.0,
            ticker="KXSOL-CFG", asset="SOL",
        )

    assert result["action"] == "skip", f"Expected skip, got: {result}"
    assert "s2_time_gate" in result["reasoning"], (
        f"Expected s2_time_gate, got: {result['reasoning']}"
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
