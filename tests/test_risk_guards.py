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


def test_s2_obi_gate_passes_on_none_amm():
    """OBI gate must pass when no OBI data — AMM markets always return None OBI."""
    original = dict(bot_state._ticker_obi)
    bot_state._ticker_obi.clear()
    try:
        confirmed, val = bot_strategy._s2_obi_gate("TEST-TICK", "yes", 0.20)
        assert confirmed, "OBI gate should pass (fail-open) for AMM markets with no OBI data"
        assert val is None
    finally:
        bot_state._ticker_obi.update(original)


def test_s2_obi_gate_passes_with_data():
    """OBI gate must pass when obi_val exceeds min_obi threshold."""
    bot_state._ticker_obi["TEST-TICK"] = 0.40
    try:
        confirmed, val = bot_strategy._s2_obi_gate("TEST-TICK", "yes", 0.20)
        assert confirmed, f"OBI gate should pass when obi=0.40 > min=0.20; got confirmed={confirmed}"
        assert abs(val - 0.40) < 0.001
    finally:
        bot_state._ticker_obi.pop("TEST-TICK", None)


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
    import bot_state
    from bot_strategy import strategy_brain_s1

    # Seed 2 recent trade times for ETH (within last 60 min)
    now = time.time()
    bot_state._s1_asset_trade_times["ETH"] = [now - 100, now - 200]

    result = strategy_brain_s1(
        btc_price=2850.0, strike=2800.0, yes_ask=45.0, no_ask=55.0,
        elapsed_seconds=760.0, secs_left=240.0,
        ticker="KXETH-RATELIMIT-TEST", asset="ETH",
    )
    # Clean up
    bot_state._s1_asset_trade_times["ETH"] = []

    # May be blocked by rate limit OR quiet hours (depending on time of day)
    assert result["action"] == "skip"
    assert "s1_rate_limit" in result["reasoning"] or "s1_quiet_hours" in result["reasoning"],         f"Expected rate_limit or quiet_hours skip, got: {result['reasoning']}"
