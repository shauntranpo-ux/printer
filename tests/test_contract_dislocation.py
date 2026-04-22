import numpy as np
import pandas as pd
import pytest
from strategy_b.contract_dislocation import ContractDislocationDetector
from shared.types import DislocationSignal

_CFG = {
    "asset": {"symbol": "BTC", "kalshi_market_prefix": "KXBTC15M"},
    "dislocation": {
        "lookback_seconds": 30,
        "residual_threshold": 2.0,
        "signal_staleness_seconds": 60,
    },
    "implied_move": {
        "volatility_source": "har_rs_j",
        "fallback_rolling_std_minutes": 30,
    },
}


def _tick(ts, yes_bid, yes_ask, seconds_to_expiry=450):
    return {"timestamp": ts, "yes_bid": yes_bid, "yes_ask": yes_ask,
            "no_bid": 100 - yes_ask, "no_ask": 100 - yes_bid,
            "seconds_to_expiry": seconds_to_expiry}

def _price(ts, px):
    return {"timestamp": ts, "price": px}


def test_smoke_empty():
    d = ContractDislocationDetector(_CFG)
    assert d.detect_dislocation([], []) is None


def test_smoke_single_tick_no_history():
    d = ContractDislocationDetector(_CFG)
    now = pd.Timestamp.now("UTC")
    result = d.detect_dislocation([_tick(now, 60, 62)], [_price(now, 50000)])
    assert result is None  # no old tick before the lookback window


def test_signal_on_large_residual():
    d = ContractDislocationDetector(_CFG)
    d.update_vol(0.80)
    now = pd.Timestamp.now("UTC")
    old = now - pd.Timedelta(seconds=40)
    # Contract jumped 5c but underlying barely moved
    ticks  = [_tick(old, 60, 62), _tick(now, 65, 67)]
    prices = [_price(old, 50000), _price(now, 50010)]
    result = d.detect_dislocation(ticks, prices)
    # actual_move=5, implied_move≈small → residual > threshold(2)
    assert result is not None


def test_signal_shape():
    d = ContractDislocationDetector(_CFG)
    d.update_vol(0.80)
    now = pd.Timestamp.now("UTC")
    old = now - pd.Timedelta(seconds=40)
    ticks  = [_tick(old, 60, 62), _tick(now, 65, 67)]
    prices = [_price(old, 50000), _price(now, 50010)]
    sig = d.detect_dislocation(ticks, prices)
    assert isinstance(sig, DislocationSignal)
    assert sig.direction in ("fade_up", "fade_down")
    assert sig.side in ("yes", "no")
    assert 0.0 <= sig.confidence <= 1.0
    assert sig.residual_magnitude >= 0.0
    assert isinstance(sig.staleness_timestamp, pd.Timestamp)


def test_no_signal_small_residual():
    d = ContractDislocationDetector(_CFG)
    now = pd.Timestamp.now("UTC")
    old = now - pd.Timedelta(seconds=40)
    # Contract mid barely moves (0.5c), underlying unchanged
    ticks  = [_tick(old, 60, 62), _tick(now, 60.5, 62.5)]
    prices = [_price(old, 50000), _price(now, 50000)]
    assert d.detect_dislocation(ticks, prices) is None


def test_fade_up_yields_no_side():
    d = ContractDislocationDetector(_CFG)
    d.update_vol(0.80)
    now = pd.Timestamp.now("UTC")
    old = now - pd.Timedelta(seconds=40)
    # Contract moved UP more than implied → fade by buying NO
    ticks  = [_tick(old, 60, 62), _tick(now, 65, 67)]
    prices = [_price(old, 50000), _price(now, 50010)]
    sig = d.detect_dislocation(ticks, prices)
    assert sig is not None
    assert sig.direction == "fade_up"
    assert sig.side == "no"


def test_fade_down_yields_yes_side():
    d = ContractDislocationDetector(_CFG)
    d.update_vol(0.80)
    now = pd.Timestamp.now("UTC")
    old = now - pd.Timedelta(seconds=40)
    # Contract dropped more than implied → buy YES (it will revert up)
    ticks  = [_tick(old, 60, 62), _tick(now, 55, 57)]
    prices = [_price(old, 50000), _price(now, 49990)]
    sig = d.detect_dislocation(ticks, prices)
    assert sig is not None
    assert sig.direction == "fade_down"
    assert sig.side == "yes"
