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
