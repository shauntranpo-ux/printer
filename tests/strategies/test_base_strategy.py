from collections import deque
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


def test_skip_propagates_from_skip_layer():
    strat = DummyStrategy(
        fixed_p=0.9,
        asset="BTC",
        skip_config=SkipConfig(),
        min_ev=0.08,
        stake_dollars=5.0,
    )
    features = _make_features(seconds_left=10)  # will skip
    decision = strat.decide(features)
    assert decision.action == "skip"
    assert "seconds_left" in decision.reason


def test_trade_when_ev_positive():
    strat = DummyStrategy(
        fixed_p=0.85,  # high p_yes
        asset="BTC",
        skip_config=SkipConfig(),
        min_ev=0.05,
        stake_dollars=5.0,
    )
    features = _make_features(yes_ask=65.0, no_ask=37.0)
    decision = strat.decide(features)
    assert decision.action == "trade"
    assert decision.side == "yes"
    assert decision.expected_value > 0.05


def test_trade_flips_to_no_when_model_disagrees_with_market():
    strat = DummyStrategy(
        fixed_p=0.20,  # model says yes unlikely
        asset="BTC",
        skip_config=SkipConfig(),
        min_ev=0.05,
        stake_dollars=5.0,
    )
    # Market priced at 65c for yes (market says 65% yes), but model says 20%
    # no_ev = (1 - 0.20) - 0.37 - fee = 0.80 - 0.37 - small = ~0.40 positive
    features = _make_features(yes_ask=65.0, no_ask=37.0)
    decision = strat.decide(features)
    assert decision.action == "trade"
    assert decision.side == "no"


def test_skip_when_ev_below_threshold():
    strat = DummyStrategy(
        fixed_p=0.66,  # barely above market implied
        asset="BTC",
        skip_config=SkipConfig(),
        min_ev=0.20,  # very high threshold
        stake_dollars=5.0,
    )
    features = _make_features(yes_ask=65.0, no_ask=37.0)
    decision = strat.decide(features)
    assert decision.action == "skip"
    assert "EV" in decision.reason
