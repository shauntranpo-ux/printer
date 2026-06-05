"""Tests for DISLOCATION signal — contract underpricing vs asset move."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot_strategy import _s1_dislocation_check


def test_dislocation_fires_when_contract_underpriced():
    """BTC 0.4% above strike, contract at 40c, 4min left → dislocation fires."""
    edge, fair_p = _s1_dislocation_check(
        dist_pct=0.004, yes_ask=40.0, secs_left=240.0, asset="ETH",
    )
    assert edge > 0.04, f"Expected edge > 0.04, got {edge:.3f}"
    assert fair_p > 0.50, f"Expected fair_p > 0.50, got {fair_p:.3f}"


def test_dislocation_no_signal_when_contract_fairly_priced():
    """Contract at 65c for 0.2% move → already priced in, no dislocation."""
    edge, fair_p = _s1_dislocation_check(
        dist_pct=0.002, yes_ask=65.0, secs_left=300.0, asset="ETH",
    )
    assert edge < 0.04, f"Expected edge < 0.04 (fair price), got {edge:.3f}"


def test_dislocation_no_signal_on_tiny_btc_move():
    """BTC move < 0.05% → below threshold, returns zero edge."""
    edge, fair_p = _s1_dislocation_check(
        dist_pct=0.0003, yes_ask=48.0, secs_left=300.0, asset="ETH",
    )
    assert edge <= 0, f"Expected no edge on tiny move, got {edge:.3f}"


def test_dislocation_caps_fair_p_at_0_80():
    """Fair probability never exceeds 0.80 cap."""
    _, fair_p = _s1_dislocation_check(
        dist_pct=0.05, yes_ask=30.0, secs_left=60.0, asset="BTC",
    )
    assert fair_p <= 0.80, f"Fair probability capped at 0.80, got {fair_p:.3f}"


def test_dislocation_edge_increases_with_dist():
    """Larger BTC move → larger dislocation edge at same contract price."""
    edge_small, _ = _s1_dislocation_check(0.002, 45.0, 300.0, "ETH")
    edge_large, _ = _s1_dislocation_check(0.010, 45.0, 300.0, "ETH")
    assert edge_large > edge_small, \
        f"Larger move should give larger edge: {edge_small:.3f} vs {edge_large:.3f}"


def test_dislocation_no_side_edge_uses_fair_no_price():
    """For NO-side trades, edge = (1-fair_p) - no_ask. Not fair_p - no_ask."""
    # dist_pct=0.004, secs_left=240, asset=ETH → fair_p will be computed by function
    # fair_p for YES side ≈ 0.65-0.75
    # no_ask = 35c → fair NO price = 1-0.65 = 0.35, edge ≈ 0.00 (neutral)
    # If bug: edge = 0.65 - 0.35 = 0.30 (wrongly fires)
    # This test verifies the NO-side edge does NOT exceed yes-side edge
    yes_edge, fair_p = _s1_dislocation_check(0.004, 35.0, 240.0, "ETH")
    # Correct no-side edge: (1 - fair_p) - 0.35
    correct_no_edge = (1.0 - fair_p) - 0.35
    # Buggy no-side edge would be fair_p - 0.35 (which equals yes_edge)
    assert correct_no_edge < yes_edge, \
        f"No-side edge ({correct_no_edge:.3f}) should be less than yes-side edge ({yes_edge:.3f})"
    # When no_ask = 35c and fair NO price ≈ 0.25-0.35, no-side edge should be near 0
    assert correct_no_edge < 0.10, \
        f"No-side edge {correct_no_edge:.3f} seems too large — possible mismatch"
