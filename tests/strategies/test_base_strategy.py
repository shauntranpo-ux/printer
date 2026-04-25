from collections import deque
from unittest.mock import patch as _patch
import pytest

from strategies.base import BaseStrategy
from strategies.features import MarketFeatures, Decision
from strategies.skip_layer import SkipConfig


class DummyStrategy(BaseStrategy):
    """Returns a fixed raw p_model for testing pipeline plumbing."""

    def __init__(self, fixed_p: float, **kwargs):
        super().__init__(**kwargs)
        self.fixed_p = fixed_p

    def compute_raw_p_model(self, features, baseline_p_above):
        return self.fixed_p, {"dummy_signal": self.fixed_p}


def _make_features(**overrides):
    f = MarketFeatures(
        asset="BTC",
        ticker="TEST",
        timestamp=0.0,
        current_price=100000.0,
        strike=99500.0,
        btc_price=100000.0,
        seconds_left=600.0,
        elapsed_seconds=300.0,
        yes_ask=65.0,
        no_ask=37.0,
        yes_bid=64.0,
        no_bid=36.0,
        spread_yes=1.0,
        spread_no=1.0,
        realized_vol_1min=0.002,
    )
    for i in range(60):
        f.prices_60m.append((float(i), 100000.0))
    for k, v in overrides.items():
        setattr(f, k, v)
    return f


def _mock_octagon(model_prob):
    """Patch octagon_client.query to return a fixed model probability."""
    return _patch(
        "strategies.signals.octagon_client.query",
        return_value=(model_prob, None, "high", False),
    )


def test_skip_propagates_from_skip_layer():
    strat = DummyStrategy(
        fixed_p=0.9,
        asset="BTC",
        skip_config=SkipConfig(),
        min_ev=0.08,
        stake_dollars=5.0,
    )
    features = _make_features(seconds_left=10)  # will skip before reaching Octagon
    decision = strat.decide(features)
    assert decision.action == "skip"
    assert "seconds_left" in decision.reason


def test_trade_when_ev_positive():
    strat = DummyStrategy(
        fixed_p=0.85,
        asset="BTC",
        skip_config=SkipConfig(),
        min_ev=0.05,
        stake_dollars=5.0,
    )
    features = _make_features(yes_ask=65.0, no_ask=37.0)
    with _mock_octagon(0.85):
        decision = strat.decide(features)
    assert decision.action == "trade"
    assert decision.side == "yes"
    assert decision.expected_value > 0.05


def test_trade_flips_to_no_when_model_disagrees_with_market():
    strat = DummyStrategy(
        fixed_p=0.20,
        asset="BTC",
        skip_config=SkipConfig(),
        min_ev=0.05,
        stake_dollars=5.0,
    )
    # Market priced at 65c for yes (~64% implied), model says 20% -> NO direction
    # no_ev = (1 - 0.20) - 0.37 - fee = 0.80 - 0.37 - small = ~0.40 positive
    features = _make_features(yes_ask=65.0, no_ask=37.0)
    with _mock_octagon(0.20):
        decision = strat.decide(features)
    assert decision.action == "trade"
    assert decision.side == "no"


def test_skip_when_ev_below_threshold():
    strat = DummyStrategy(
        fixed_p=0.66,  # barely above market implied (~64%)
        asset="BTC",
        skip_config=SkipConfig(),
        min_ev=0.20,  # very high threshold
        stake_dollars=5.0,
    )
    features = _make_features(yes_ask=65.0, no_ask=37.0)
    with _mock_octagon(0.66):
        decision = strat.decide(features)
    assert decision.action == "skip"
    assert "EV" in decision.reason