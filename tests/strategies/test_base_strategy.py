from unittest.mock import patch as _patch

from strategies.base import BaseStrategy
from strategies.features import MarketFeatures
from strategies.skip_layer import SkipConfig


class DummyStrategy(BaseStrategy):
    """Concrete subclass for testing the decision pipeline."""


def _make_features(trend: float = 0.0, **overrides):
    """
    trend > 0 → prices rise over 60 ticks (aligns momentum with YES signal).
    trend < 0 → prices fall (aligns with NO signal).
    """
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
        f.prices_60m.append((float(i), 100000.0 + trend * i))
    for k, v in overrides.items():
        setattr(f, k, v)
    return f


def _mock_signal(side: str):
    """
    Patch compute_15m_signal to return a fixed (side, raw_p_yes, vote_count).
    raw_p_yes is always P(YES wins): 0.70 for YES signals, 0.30 for NO signals.
    ev.py uses (1 - p_model) for the NO leg, so NO at p_yes=0.30 → no_ev = 0.70 - no_ask.
    vote_count=5 ensures Gate B always passes in these tests.
    """
    p_yes = 0.70 if side == "yes" else 0.30
    return _patch(
        "strategies.signals.fifteen_min_signal.compute_15m_signal",
        return_value=(side, p_yes, 5),
    )


def test_trade_when_ev_positive():
    strat = DummyStrategy(asset="BTC", skip_config=SkipConfig(), min_ev=0.05, stake_dollars=5.0)
    # yes_ask=55c, trend up → signal=yes, momentum aligned, EV positive
    features = _make_features(trend=10.0, yes_ask=55.0, no_ask=47.0, yes_bid=54.0, no_bid=46.0)
    with _mock_signal("yes"):
        decision = strat.decide(features)
    assert decision.action == "trade"
    assert decision.side == "yes"


def test_trade_flips_to_no_when_signal_is_down():
    strat = DummyStrategy(asset="BTC", skip_config=SkipConfig(), min_ev=0.05, stake_dollars=5.0)
    # no_ask=37c, trend down → signal=no (raw_p_yes=0.30), momentum aligned, no_ev=0.70-0.37-fee>0
    features = _make_features(trend=-10.0, yes_ask=65.0, no_ask=37.0, yes_bid=64.0, no_bid=36.0)
    with _mock_signal("no"):
        decision = strat.decide(features)
    assert decision.action == "trade"
    assert decision.side == "no"


def test_skip_when_ev_below_threshold():
    strat = DummyStrategy(asset="BTC", skip_config=SkipConfig(), min_ev=0.20, stake_dollars=5.0)
    # yes_ask=65c: yes_ev = 0.70 - 0.65 - fee ≈ 0.05 < 0.20
    features = _make_features(trend=10.0, yes_ask=65.0, no_ask=37.0, yes_bid=64.0, no_bid=36.0)
    with _mock_signal("yes"):
        decision = strat.decide(features)
    assert decision.action == "skip"
    assert "EV" in decision.reason


def test_skip_when_momentum_misaligned():
    strat = DummyStrategy(asset="BTC", skip_config=SkipConfig(), min_ev=0.05, stake_dollars=5.0)
    # Signal says YES but prices are trending down → momentum misalign skip
    features = _make_features(trend=-10.0, yes_ask=55.0, no_ask=47.0, yes_bid=54.0, no_bid=46.0)
    with _mock_signal("yes"):
        decision = strat.decide(features)
    assert decision.action == "skip"
    assert "momentum" in decision.reason
