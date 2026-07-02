"""Tests for risk guard fixes."""
import os
import sys
import inspect
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot_state
import bot_strategy
import bot_risk
import bot_loops


def test_s1_cap_global_source_check():
    """strategy_brain_s1 must have s1_cap_global skip reason."""
    src = inspect.getsource(bot_strategy.strategy_brain_s1)
    assert "s1_cap_global" in src, "strategy_brain_s1 missing s1_cap_global skip reason"


def test_s1_cap_asset_source_check():
    """strategy_brain_s1 must have s1_cap_asset skip reason."""
    src = inspect.getsource(bot_strategy.strategy_brain_s1)
    assert "s1_cap_asset" in src, "strategy_brain_s1 missing s1_cap_asset skip reason"


def test_s1_consecutive_loss_persisted():
    """write_state_file must include s1_consecutive_losses in its state dict."""
    src = inspect.getsource(bot_risk.write_state_file)
    assert "s1_consecutive_losses" in src, \
        "write_state_file not persisting s1_consecutive_losses"


def test_s1_consecutive_loss_restored():
    """bot_loops startup recovery must read s1_consecutive_losses from saved state."""
    src = inspect.getsource(bot_loops)
    assert "s1_consecutive_losses" in src, \
        "bot_loops startup not restoring s1_consecutive_losses"


def test_s1_cl_alert_source_check():
    """_settle_s1_trade must check max_consecutive_losses and send [S1] alert."""
    src = inspect.getsource(bot_risk._settle_s1_trade)
    assert "max_consecutive_losses" in src or "max_cl" in src, \
        "_settle_s1_trade not checking max_consecutive_losses for S1 alert"
    assert "[S1]" in src and "consecutive losses" in src, \
        "_settle_s1_trade missing [S1] consecutive losses Telegram text"


def test_s1_momentum_signal_source_check():
    """bot_loops.py must call _s1_multitf_momentum as the S1 direction pointer."""
    from pathlib import Path
    bot_loops_path = Path(__file__).parent.parent / "bot_loops.py"
    with open(bot_loops_path, encoding="utf-8") as f:
        src = f.read()
    assert "_s1_multitf_momentum" in src, \
        "bot_loops.py does not call _s1_multitf_momentum — S1 direction is not multi-timeframe"


def test_s1_rate_limit_skips_after_max_per_hour():
    """S1 must skip when >= max_s1_per_asset_per_hour recent fills for asset."""
    from unittest.mock import patch
    from bot_strategy import strategy_brain_s1

    # SOL is an enabled S1 asset (ETH short-circuits on the disabled gate). Seed 2 recent
    # SOL fills within the last hour to trip the per-asset rate limit.
    now = time.time()
    with patch("bot_strategy.read_config",
               return_value={"mode": "paper", "quiet_hours_enabled": False}), \
         patch.object(bot_state, "_s1_pending_trades", {}), \
         patch.object(bot_state, "_s1_cooldown_until", {}), \
         patch.object(bot_state, "_s1_asset_trade_times", {"SOL": [now - 100, now - 200]}):
        result = strategy_brain_s1(
            btc_price=150.0, strike=149.0, yes_ask=45.0, no_ask=55.0,
            elapsed_seconds=660.0, secs_left=240.0,
            ticker="KXSOL-RATELIMIT-TEST", asset="SOL",
        )

    assert result["action"] == "skip"
    assert "s1_rate_limit" in result["reasoning"], \
        f"Expected rate_limit skip, got: {result['reasoning']}"
