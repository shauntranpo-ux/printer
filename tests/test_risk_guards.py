"""Tests for risk guard fixes."""
import os
import sys
import inspect

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot_state
import bot_strategy
import bot_risk
import bot_loops


def test_s2_obi_gate_fails_closed_on_none():
    """OBI gate must block (not pass) when no OBI data exists for ticker."""
    original = dict(bot_state._ticker_obi)
    bot_state._ticker_obi.clear()
    try:
        confirmed, val = bot_strategy._s2_obi_gate("TEST-TICK", "yes", 0.20)
        assert not confirmed, "OBI gate should fail-closed when obi_val is None"
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
