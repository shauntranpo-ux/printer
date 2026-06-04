"""Tests for dual-side arbitrage detector."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from bot_strategy import check_dual_side_arb


def test_dual_arb_fires_when_spread_below_threshold():
    """YES=42 + NO=47 = 89c < 93c threshold → arb fires."""
    result = check_dual_side_arb(yes_ask=42.0, no_ask=47.0, fee_per_contract_cents=7)
    assert result["arb"] is True, f"Expected arb=True, got {result}"
    assert result["net_edge_cents"] > 0


def test_dual_arb_skips_when_spread_above_threshold():
    """YES=49 + NO=51 = 100c > 93c → no arb."""
    result = check_dual_side_arb(yes_ask=49.0, no_ask=51.0, fee_per_contract_cents=7)
    assert result["arb"] is False


def test_dual_arb_net_edge_calculation():
    """net_edge = 100 - yes_ask - no_ask - fees, approximately 100 - 40 - 45 = 15."""
    result = check_dual_side_arb(yes_ask=40.0, no_ask=45.0, fee_per_contract_cents=7)
    assert result["arb"] is True
    # Net edge should be approximately 100 - 40 - 45 = 15 minus small fees
    assert 10.0 <= result["net_edge_cents"] <= 16.0, \
        f"Expected ~15c net edge, got {result['net_edge_cents']}"


def test_dual_arb_threshold_configurable():
    """Threshold can be passed as argument."""
    # 42 + 52 = 94 > 90 → no arb at threshold=90
    result_no = check_dual_side_arb(42.0, 52.0, threshold=90.0)
    assert result_no["arb"] is False
    # 42 + 47 = 89 < 93 → arb at default threshold
    result_yes = check_dual_side_arb(42.0, 47.0, threshold=93.0)
    assert result_yes["arb"] is True
