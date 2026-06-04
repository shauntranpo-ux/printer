"""Tests for DISLOCATION signal — contract underpricing vs asset move."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot_strategy import _s1_dislocation_check


def test_dislocation_fires_when_contract_underpriced():
    """BTC 0.4% above strike, contract at 40c, 4min left → dislocation fires."""
    edge, fair_p = _s1_dislocation_check(
        dist_pct=0.004, yes_ask=40.0, secs_left=240.0, asset="ETH", min_edge=0.04,
    )
    assert edge > 0.04, f"Expected edge > 0.04, got {edge:.3f}"
    assert fair_p > 0.50, f"Expected fair_p > 0.50, got {fair_p:.3f}"


def test_dislocation_no_signal_when_contract_fairly_priced():
    """Contract at 65c for 0.2% move → already priced in, no dislocation."""
    edge, fair_p = _s1_dislocation_check(
        dist_pct=0.002, yes_ask=65.0, secs_left=300.0, asset="ETH", min_edge=0.04,
    )
    assert edge < 0.04, f"Expected edge < 0.04 (fair price), got {edge:.3f}"


def test_dislocation_no_signal_on_tiny_btc_move():
    """BTC move < 0.05% → below threshold, returns zero edge."""
    edge, fair_p = _s1_dislocation_check(
        dist_pct=0.0003, yes_ask=48.0, secs_left=300.0, asset="ETH", min_edge=0.04,
    )
    assert edge <= 0, f"Expected no edge on tiny move, got {edge:.3f}"


def test_dislocation_caps_fair_p_at_0_80():
    """Fair probability never exceeds 0.80 cap."""
    _, fair_p = _s1_dislocation_check(
        dist_pct=0.05, yes_ask=30.0, secs_left=60.0, asset="BTC", min_edge=0.04,
    )
    assert fair_p <= 0.80, f"Fair probability capped at 0.80, got {fair_p:.3f}"


def test_dislocation_edge_increases_with_dist():
    """Larger BTC move → larger dislocation edge at same contract price."""
    edge_small, _ = _s1_dislocation_check(0.002, 45.0, 300.0, "ETH", 0.0)
    edge_large, _ = _s1_dislocation_check(0.010, 45.0, 300.0, "ETH", 0.0)
    assert edge_large > edge_small, \
        f"Larger move should give larger edge: {edge_small:.3f} vs {edge_large:.3f}"
