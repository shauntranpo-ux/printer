from collections import deque
from unittest.mock import patch as _patch
import pytest

from strategies.base import BaseStrategy
from strategies.features import MarketFeatures, Decision
from strategies.skip_layer import SkipConfig


class DummyStrategy(BaseStrategy):
    """Concrete subclass for testing the decision pipeline."""


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


def _mock_supertrend(direction: int):
    """Patch supertrend to return a fixed direction (1=YES, -1=NO)."""
    return _patch(
        "strategies.signals.supertrend.supertrend_direction",
        return_value=direction,
    )


def test_skip_propagates_from_skip_layer():
    strat = DummyStrategy(
        asset="BTC",
        skip_config=SkipConfig(),
        min_ev=0.08,
        stake_dollars=5.0,
    )
    features = _make_features(seconds_left=10)  # triggers skip_layer before Supertrend
    decision = strat.decide(features)
    assert decision.action == "skip"
    assert "seconds_left" in decision.reason


def test_trade_when_ev_positive():
    strat = DummyStrategy(
        asset="BTC",
        skip_config=SkipConfig(),
        min_ev=0.05,
        stake_dollars=5.0,
    )
    # yes_ask=55c: yes_ev = 0.70 - 0.55 - fee ≈ 0.15 > 0.05
    features = _make_features(yes_ask=55.0, no_ask=47.0)
    with _mock_supertrend(1):
        decision = strat.decide(features)
    assert decision.action == "trade"
    assert decision.side == "yes"


def test_trade_flips_to_no_when_supertrend_is_down():
    strat = DummyStrategy(
        asset="BTC",
        skip_config=SkipConfig(),
        min_ev=0.05,
        stake_dollars=5.0,
    )
    # Supertrend=-1 (down/NO): p_model_for_ev=0.30
    # no_ev = (1-0.30) - 0.37 - fee = 0.70 - 0.37 - fee ≈ 0.33 > 0.05
    features = _make_features(yes_ask=65.0, no_ask=37.0)
    with _mock_supertrend(-1):
        decision = strat.decide(features)
    assert decision.action == "trade"
    assert decision.side == "no"


def test_skip_when_ev_below_threshold():
    strat = DummyStrategy(
        asset="BTC",
        skip_config=SkipConfig(),
        min_ev=0.20,  # very high threshold
        stake_dollars=5.0,
    )
    # yes_ask=65c: yes_ev = 0.70 - 0.65 - fee ≈ 0.05 < 0.20
    features = _make_features(yes_ask=65.0, no_ask=37.0)
    with _mock_supertrend(1):
        decision = strat.decide(features)
    assert decision.action == "skip"
    assert "EV" in decision.reason
