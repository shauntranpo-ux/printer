import numpy as np
import pytest
from strategy_a.model import StrategyAModel

_FEES = {"kalshi": {"taker_fee_rate": 0.03, "maker_fee_rate": 0.00}, "safety_margin": 0.005}
_CFG = {
    "model": {"type": "logistic_regression", "calibration": "isotonic"},
    "thresholds": {
        "edge_above_fee": {"eu_open": 0.02, "weekend": 0.03},
        "btc_degraded_penalty": 0.01,
    },
}


def _fitted_model():
    m = StrategyAModel(_CFG, _FEES)
    rng = np.random.default_rng(42)
    X = rng.normal(0, 1, (200, 5))
    y = (rng.random(200) > 0.5).astype(int)
    m.fit(X, y, [f"f{i}" for i in range(5)])
    return m


def test_unfitted_returns_half():
    m = StrategyAModel(_CFG, _FEES)
    assert m.predict_proba({"x": 1.0}) == 0.5


def test_predict_proba_in_range():
    m = _fitted_model()
    rng = np.random.default_rng(7)
    for _ in range(30):
        feats = {f"f{i}": float(rng.normal()) for i in range(5)}
        p = m.predict_proba(feats)
        assert 0.0 <= p <= 1.0, f"p={p} out of [0,1]"


def test_get_edge_positive():
    m = StrategyAModel(_CFG, _FEES)
    assert m.get_edge(0.7, 0.55) == pytest.approx(0.15)


def test_get_edge_negative():
    m = StrategyAModel(_CFG, _FEES)
    assert m.get_edge(0.3, 0.45) == pytest.approx(-0.15)


def test_should_trade_above_threshold():
    m = StrategyAModel(_CFG, _FEES)
    # min_edge = 0.03 + 0.005 + 0.02 = 0.055; edge = 0.7 - 0.55 = 0.15 > 0.055
    assert m.should_trade(0.70, 0.55, "eu_open", _CFG)


def test_should_trade_below_threshold():
    m = StrategyAModel(_CFG, _FEES)
    # edge = 0.01 < 0.055
    assert not m.should_trade(0.56, 0.55, "eu_open", _CFG)


def test_btc_degraded_widens_threshold():
    m = StrategyAModel(_CFG, _FEES)
    # edge = 0.06: passes without degraded (0.055), fails with degraded (0.065)
    assert     m.should_trade(0.61, 0.55, "eu_open", _CFG, btc_degraded=False)
    assert not m.should_trade(0.61, 0.55, "eu_open", _CFG, btc_degraded=True)


def test_weekend_threshold_used():
    m = StrategyAModel(_CFG, _FEES)
    # weekend min_edge = 0.03 + 0.005 + 0.03 = 0.065; edge = 0.06 < 0.065
    assert not m.should_trade(0.61, 0.55, "weekend", _CFG)


def test_unknown_regime_defaults_to_002():
    m = StrategyAModel(_CFG, _FEES)
    # unknown regime → 0.02 default; min_edge = 0.055
    assert m.should_trade(0.70, 0.55, "unknown_regime", _CFG)


def test_no_side_with_zero_edge():
    m = StrategyAModel(_CFG, _FEES)
    assert not m.should_trade(0.55, 0.55, "eu_open", _CFG)


def test_edge_exactly_at_threshold_does_not_trade():
    m = StrategyAModel(_CFG, _FEES)
    # min_edge = 0.03 + 0.005 + 0.02 = 0.055; edge = exactly 0.055 → must NOT trade (> not >=)
    assert not m.should_trade(0.605, 0.55, "eu_open", _CFG)
