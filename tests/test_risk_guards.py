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
    bot_state._ticker_obi.clear()
    confirmed, val = bot_strategy._s2_obi_gate("TEST-TICK", "yes", 0.20)
    assert not confirmed, "OBI gate should fail-closed when obi_val is None"
    assert val is None


def test_s2_obi_gate_passes_with_data():
    """OBI gate must pass when obi_val exceeds min_obi threshold."""
    bot_state._ticker_obi["TEST-TICK"] = 0.40
    try:
        confirmed, val = bot_strategy._s2_obi_gate("TEST-TICK", "yes", 0.20)
        assert confirmed, f"OBI gate should pass when obi=0.40 > min=0.20; got confirmed={confirmed}"
        assert abs(val - 0.40) < 0.001
    finally:
        bot_state._ticker_obi.pop("TEST-TICK", None)
