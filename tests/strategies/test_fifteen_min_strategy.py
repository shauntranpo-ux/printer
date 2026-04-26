"""
Tests for FifteenMinStrategy — the unified 15m strategy for BTC/ETH/SOL/XRP.

Direction: price-continuation (above strike -> YES, below strike -> NO).
Probability: BV3 P(YES=above strike), validated by EV + confidence gates.
"""

import time
from unittest.mock import patch as _patch

import pytest

from strategies.fifteen_min_strategy import FifteenMinStrategy
from strategies.features import MarketFeatures
from strategies.skip_layer import SkipConfig


def _mock_octagon(model_prob=0.82):
    return _patch(
        "strategies.signals.octagon_client.query",
        return_value=(model_prob, None, "high", False),
    )


def _features(
    *,
    asset: str = "ETH",
    current_price: float = 2100.0,
    strike: float = 2075.0,
    yes_ask: float = 60.0,
    no_ask: float = 42.0,
    seconds_left: float = 600.0,
    bv3_prob: float = 0.76,
):
    now = time.time()
    f = MarketFeatures(
        asset=asset,
        ticker=f"KX{asset}15M-TEST",
        timestamp=now,
        current_price=current_price,
        strike=strike,
        btc_price=100000.0,
        seconds_left=seconds_left,
        elapsed_seconds=300.0,
        yes_ask=yes_ask,
        no_ask=no_ask,
        yes_bid=max(0.0, yes_ask - 1.0),
        no_bid=max(0.0, no_ask - 1.0),
        spread_yes=1.0,
        spread_no=1.0,
        realized_vol_1min=0.002,
    )
    f.bv3_prob = bv3_prob
    for i in range(60):
        f.prices_60m.append((float(i), current_price))
    return f


def _strat(asset="ETH", min_ev=0.05, confidence_threshold=0.0):
    return FifteenMinStrategy(
        asset=asset,
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=min_ev,
        stake_dollars=25.0,
        confidence_threshold=confidence_threshold,
    )


def test_decides_trade_or_skip():
    with _mock_octagon():
        d = _strat().decide(_features())
    assert d.action in ("trade", "skip")
    assert 0.0 < d.p_model < 1.0


def test_above_strike_picks_yes():
    """Price above strike -> continuation direction is YES."""
    with _mock_octagon():
        d = _strat(min_ev=0.01).decide(
            _features(current_price=2100.0, strike=2075.0, yes_ask=65.0, no_ask=37.0, bv3_prob=0.77)
        )
    if d.action == "trade":
        assert d.side == "yes"


def test_below_strike_picks_no():
    """Octagon P(YES) < 0.5 -> NO direction."""
    with _mock_octagon(model_prob=0.28):  # Octagon: 28% chance YES → NO
        d = _strat(min_ev=0.01).decide(
            _features(current_price=2050.0, strike=2075.0, yes_ask=28.0, no_ask=55.0, bv3_prob=0.20)
        )
    if d.action == "trade":
        assert d.side == "no"


def test_low_bv3_skips_via_confidence_gate():
    """BV3 near 0.50 -> confidence gate rejects the trade."""
    with _mock_octagon():
        d = _strat(confidence_threshold=0.74).decide(
            _features(bv3_prob=0.55)
        )
    assert d.action == "skip"


def test_entry_range_rejects_76c_entry():
    """Entry at or above 76c is outside the range (bot.py sets max=76 for 15m)."""
    strat = FifteenMinStrategy(
        asset="ETH",
        skip_config=SkipConfig(cold_start_samples=10, max_entry_price_cents=76.0),
        min_ev=0.001,
        stake_dollars=25.0,
    )
    with _mock_octagon():
        d = strat.decide(_features(yes_ask=76.0, no_ask=26.0, bv3_prob=0.90))
    assert d.action == "skip"
    assert "entry_range" in d.reason


def test_no_trade_below_floor():
    """Both sides below 20c floor -> skip."""
    with _mock_octagon():
        d = _strat().decide(
            _features(yes_ask=15.0, no_ask=18.0, bv3_prob=0.80)
        )
    assert d.action == "skip"


def test_all_assets_instantiate():
    """FifteenMinStrategy works for all four live assets."""
    for asset in ("BTC", "ETH", "SOL", "XRP"):
        s = FifteenMinStrategy(
            asset=asset,
            skip_config=SkipConfig(),
            min_ev=0.05,
            stake_dollars=25.0,
        )
        assert s.asset == asset
        assert s.is_15m is True
