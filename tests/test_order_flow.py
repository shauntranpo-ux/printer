import numpy as np
import pandas as pd
import pytest
from strategy_a.features.order_flow import OrderFlowFeatures

_CFG = {
    "order_flow": {
        "ofi_depths": [1, 3, 5, 10],
        "vpin_bucket_size": 500.0,
        "vpin_rolling_buckets": 50,
    }
}

_EXPECTED_KEYS = {
    "ofi_l1", "ofi_l3", "ofi_l5", "ofi_l10",
    "vamp",
    "signed_flow_1m", "signed_flow_5m", "signed_flow_15m",
    "vpin",
    "spread_abs", "spread_bps", "depth_bid", "depth_ask",
}

def _book():
    bids = [(50000.0 - i * 10, 1.0 + i * 0.1) for i in range(10)]
    asks = [(50010.0 + i * 10, 1.0 + i * 0.1) for i in range(10)]
    return {"timestamp": pd.Timestamp.utcnow(), "bids": bids, "asks": asks}

def _trade(side="buy"):
    return {"timestamp": pd.Timestamp.utcnow(), "price": 50005.0,
            "size": 0.5, "aggressor_side": side}


def test_smoke():
    of = OrderFlowFeatures(_CFG)
    assert isinstance(of.compute({"book": _book(), "trades": [_trade()]}), dict)


def test_shape():
    of = OrderFlowFeatures(_CFG)
    result = of.compute({"book": _book(), "trades": []})
    assert _EXPECTED_KEYS.issubset(result.keys())


def test_ofi_range():
    of = OrderFlowFeatures(_CFG)
    result = of.compute({"book": _book(), "trades": []})
    for d in _CFG["order_flow"]["ofi_depths"]:
        v = result[f"ofi_l{d}"]
        assert isinstance(v, float)
        assert -1.0 <= v <= 1.0, f"ofi_l{d}={v} outside [-1,1]"


def test_spread_positive():
    of = OrderFlowFeatures(_CFG)
    result = of.compute({"book": _book(), "trades": []})
    assert result["spread_abs"] > 0.0
    assert result["spread_bps"] > 0.0


def test_signed_flow_direction():
    of = OrderFlowFeatures(_CFG)
    trade = {**_trade("buy"), "size": 10.0}
    result = of.compute({"book": _book(), "trades": [trade]})
    assert result["signed_flow_1m"] > 0.0
    assert result["signed_flow_5m"] > 0.0


def test_vpin_range():
    of = OrderFlowFeatures(_CFG)
    now = pd.Timestamp.utcnow()
    trades = [{"timestamp": now, "price": 50000.0, "size": 100.0, "aggressor_side": "buy"}
              for _ in range(20)]
    result = of.compute({"book": _book(), "trades": trades})
    assert 0.0 <= result["vpin"] <= 1.0


def test_empty_book_gives_nan_spread():
    of = OrderFlowFeatures(_CFG)
    result = of.compute({"book": {"timestamp": pd.Timestamp.utcnow(), "bids": [], "asks": []}, "trades": []})
    assert np.isnan(result["spread_abs"])
