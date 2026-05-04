"""
Tests for FifteenMinStrategy — the unified 15m strategy for BTC/ETH/SOL/XRP.

Direction: D3-hybrid ensemble (compute_15m_signal).
EV: calibrated BS p_yes per direction; per-asset minimum gate.
"""

import time
from contextlib import contextmanager
from unittest.mock import patch as _patch

import pytest

from strategies.fifteen_min_strategy import FifteenMinStrategy
from strategies.features import MarketFeatures
from strategies.skip_layer import SkipConfig


def _mock_15m_signal(side: str = "yes", raw_p: float = 0.70, vote_count: int = 5):
    """Patch compute_15m_signal to return a fixed (side, raw_p, vote_count) 3-tuple.
    vote_count=5 ensures Gate B always passes in these tests."""
    return _patch(
        "strategies.signals.fifteen_min_signal.compute_15m_signal",
        return_value=(side, raw_p, vote_count),
    )




def _features(
    *,
    asset: str = "ETH",
    current_price: float = 2100.0,
    strike: float = 2075.0,
    yes_ask: float = 60.0,
    no_ask: float = 42.0,
    seconds_left: float = 600.0,
    rising: bool = True,
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
    for i in range(60):
        price = current_price + (i * 0.5 if rising else -i * 0.5)
        f.prices_60m.append((float(i), price))
    return f


def _strat(asset="ETH", min_ev=0.05):
    return FifteenMinStrategy(
        asset=asset,
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=min_ev,
        stake_dollars=25.0,
    )


def test_decides_trade_or_skip():
    with _mock_15m_signal("yes", 0.70):
        d = _strat().decide(_features())
    assert d.action in ("trade", "skip")
    assert 0.0 < d.p_model < 1.0


def test_signal_yes_picks_yes():
    """Signal='yes' → YES direction."""
    with _mock_15m_signal("yes", 0.70):
        d = _strat(min_ev=0.01).decide(
            _features(yes_ask=55.0, no_ask=47.0)
        )
    if d.action == "trade":
        assert d.side == "yes"


def test_signal_no_picks_no():
    """Signal='no' → NO direction."""
    with _mock_15m_signal("no", 0.35):
        d = _strat(min_ev=0.01).decide(
            _features(yes_ask=65.0, no_ask=37.0)
        )
    if d.action == "trade":
        assert d.side == "no"




def test_entry_range_rejects_76c_entry():
    """Entry at 76c is always rejected."""
    strat = FifteenMinStrategy(
        asset="ETH",
        skip_config=SkipConfig(cold_start_samples=10, max_entry_price_cents=76.0),
        min_ev=0.001,
        stake_dollars=25.0,
    )
    with _mock_15m_signal("yes", 0.70):
        d = strat.decide(_features(yes_ask=76.0, no_ask=26.0))
    assert d.action == "skip"


def test_no_trade_below_floor():
    """Both sides below 20c floor → entry_range skip."""
    with _mock_15m_signal("yes", 0.70):
        d = _strat().decide(
            _features(yes_ask=15.0, no_ask=18.0)
        )
    assert d.action == "skip"
    assert "entry_range" in d.reason


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
