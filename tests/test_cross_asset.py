import numpy as np
import pytest
from strategy_a.features.cross_asset import compute, _SENTINEL

_EXPECTED_KEYS = set(_SENTINEL.keys())


def _btc_feats():
    return {
        "har_rv": {
            "15m_rv": 1e-4, "15m_rv_pos": 6e-5, "15m_rv_neg": 4e-5,
            "15m_bv": 9e-5,  "15m_jump": 1e-5,  "15m_signed_jump": 2e-5,
            "sigma_forecast": 0.012,
        },
        "order_flow": {"ofi_l1": 0.3, "ofi_l5": 0.15},
    }


def test_smoke_with_data():
    d = {"btc_features": _btc_feats(), "btc_returns": {"1m": 0.001, "5m": 0.002, "15m": 0.003}, "config": {}}
    assert isinstance(compute(d), dict)


def test_smoke_no_data():
    assert isinstance(compute({}), dict)


def test_shape():
    d = {"btc_features": _btc_feats(), "btc_returns": {"1m": 0.001, "5m": 0.002, "15m": 0.003}, "config": {}}
    assert _EXPECTED_KEYS.issubset(compute(d).keys())


def test_sentinel_shape():
    assert _EXPECTED_KEYS.issubset(compute({}).keys())


def test_degraded_on_missing():
    assert compute({})["btc_degraded"] == 1.0


def test_not_degraded_when_present():
    d = {"btc_features": _btc_feats(), "btc_returns": {"1m": 0.001, "5m": 0.002, "15m": 0.003}, "config": {}}
    assert compute(d)["btc_degraded"] == 0.0


def test_jump_flag_binary():
    d = {"btc_features": _btc_feats(), "btc_returns": {"1m": 0.001, "5m": 0.002, "15m": 0.003}, "config": {}}
    v = compute(d)["btc_jump_flag"]
    assert v in (0.0, 1.0)


def test_returns_are_float():
    d = {"btc_features": _btc_feats(), "btc_returns": {"1m": 0.001, "5m": 0.002, "15m": 0.003}, "config": {}}
    result = compute(d)
    for k in ("btc_ret_1m", "btc_ret_5m", "btc_ret_15m", "btc_sigma_forecast"):
        assert isinstance(result[k], float), f"{k} is not float"


def test_jump_detected_when_high_ratio():
    """Jump flag should fire when J/RV > 0.10."""
    d = {
        "btc_features": {
            "har_rv": {
                "15m_rv": 1e-4, "15m_jump": 2e-5,  # ratio = 0.2 > 0.10
                "sigma_forecast": 0.01,
            },
            "order_flow": {"ofi_l1": 0.0, "ofi_l5": 0.0},
        },
        "btc_returns": {"1m": 0.0, "5m": 0.0, "15m": 0.0},
        "config": {},
    }
    assert compute(d)["btc_jump_flag"] == 1.0


def test_exception_returns_sentinel():
    """If BTC features are malformed, should return sentinel not raise."""
    bad = {"btc_features": {"har_rv": None}, "btc_returns": {"1m": 0.0}, "config": {}}
    result = compute(bad)
    assert result["btc_degraded"] == 1.0
