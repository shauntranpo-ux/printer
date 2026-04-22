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
    return {"timestamp": pd.Timestamp.now("UTC"), "bids": bids, "asks": asks}

def _trade(side="buy"):
    return {"timestamp": pd.Timestamp.now("UTC"), "price": 50005.0,
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
    now = pd.Timestamp.now("UTC")
    trades = [{"timestamp": now, "price": 50000.0, "size": 100.0, "aggressor_side": "buy"}
              for _ in range(20)]
    result = of.compute({"book": _book(), "trades": trades})
    assert 0.0 <= result["vpin"] <= 1.0


def test_empty_book_gives_nan_spread():
    of = OrderFlowFeatures(_CFG)
    result = of.compute({"book": {"timestamp": pd.Timestamp.now("UTC"), "bids": [], "asks": []}, "trades": []})
    assert np.isnan(result["spread_abs"])


def test_vpin_large_trade_stays_bounded():
    """A single trade larger than the bucket size must not produce VPIN > 1.0."""
    of = OrderFlowFeatures(_CFG)  # bucket_size=500
    now = pd.Timestamp.now("UTC")
    # 600-unit all-buy trade exceeds the 500-unit bucket
    big_trade = {"timestamp": now, "price": 50000.0, "size": 600.0, "aggressor_side": "buy"}
    result = of.compute({"book": _book(), "trades": [big_trade]})
    assert 0.0 <= result["vpin"] <= 1.0, f"VPIN={result['vpin']} out of [0,1]"


def test_tz_naive_timestamp_does_not_crash():
    """Trades with tz-naive timestamps must not raise TypeError in _purge."""
    of = OrderFlowFeatures(_CFG)
    naive_ts = "2026-04-22T10:00:00"  # no timezone info
    trade = {"timestamp": naive_ts, "price": 50000.0, "size": 1.0, "aggressor_side": "buy"}
    book = {"timestamp": pd.Timestamp.now("UTC"), "bids": _book()["bids"], "asks": _book()["asks"]}
    result = of.compute({"book": book, "trades": [trade]})
    assert isinstance(result, dict)
