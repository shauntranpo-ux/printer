"""Tests for build_strike_ladder_labels() in backtesting/data/label_builder.py."""
import sys
import os

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtesting.data.label_builder import build_strike_ladder_labels


def _bars(n: int = 100, base: float = 50000.0) -> pd.DataFrame:
    start = pd.Timestamp("2024-01-01", tz="UTC")
    ts = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    prices = np.linspace(base, base + n * 10, n)
    return pd.DataFrame({
        "timestamp": ts,
        "open": prices,
        "high": prices * 1.001,
        "low": prices * 0.999,
        "close": prices * 1.0005,
        "volume": np.ones(n),
    })


def _ladder(
    n_events: int = 3,
    strikes: list[float] | None = None,
    base_close_price: float = 50000.0,
) -> pd.DataFrame:
    if strikes is None:
        strikes = [49500.0, 49700.0, 50000.0, 50300.0, 50500.0]
    rows = []
    start = pd.Timestamp("2024-01-01 00:30:00", tz="UTC")
    for i in range(n_events):
        close_time = start + pd.Timedelta(hours=i + 1)
        eid = f"ev_{i}"
        snap_ts = close_time - pd.Timedelta(minutes=15)
        for k in strikes:
            rows.append({
                "event_id": eid,
                "event_close_time": close_time,
                "timestamp": snap_ts,
                "strike": k,
                "yes_bid": 0.45, "yes_ask": 0.55,
                "no_bid": 0.45, "no_ask": 0.55,
                "mid_price": 0.50, "volume": 100.0,
                "market_id": f"{eid}_{k}",
            })
    return pd.DataFrame(rows)


class TestBuildStrikeLadderLabels:
    def test_returns_dataframe(self):
        bars = _bars()
        ladder = _ladder()
        result = build_strike_ladder_labels(bars, ladder)
        assert isinstance(result, pd.DataFrame)

    def test_required_columns_present(self):
        bars = _bars()
        ladder = _ladder()
        result = build_strike_ladder_labels(bars, ladder)
        for col in ["event_id", "strike", "event_close_time", "label", "reference_price_close"]:
            assert col in result.columns

    def test_label_is_binary(self):
        bars = _bars()
        ladder = _ladder()
        result = build_strike_ladder_labels(bars, ladder)
        assert set(result["label"].unique()).issubset({0, 1})

    def test_one_row_per_event_strike(self):
        bars = _bars()
        ladder = _ladder(n_events=3)
        result = build_strike_ladder_labels(bars, ladder)
        assert result.duplicated(subset=["event_id", "strike"]).sum() == 0

    def test_label_1_when_close_above_strike(self):
        # close price at bar 60 is base + 60*10 = 50600; strike 50000 should be label=1
        bars = _bars(100, base=50000.0)
        close_time = pd.Timestamp("2024-01-01 01:00:00", tz="UTC")
        ladder = pd.DataFrame([{
            "event_id": "ev0",
            "event_close_time": close_time,
            "timestamp": pd.Timestamp("2024-01-01 00:45:00", tz="UTC"),
            "strike": 50000.0,
            "yes_bid": 0.4, "yes_ask": 0.6,
            "no_bid": 0.4, "no_ask": 0.6,
            "mid_price": 0.5, "volume": 100.0,
            "market_id": "m0",
        }])
        result = build_strike_ladder_labels(bars, ladder)
        assert len(result) == 1
        assert result["label"].iloc[0] == 1

    def test_label_0_when_close_below_strike(self):
        bars = _bars(100, base=50000.0)
        close_time = pd.Timestamp("2024-01-01 01:00:00", tz="UTC")
        ladder = pd.DataFrame([{
            "event_id": "ev0",
            "event_close_time": close_time,
            "timestamp": pd.Timestamp("2024-01-01 00:45:00", tz="UTC"),
            "strike": 999999.0,  # far above any close price
            "yes_bid": 0.01, "yes_ask": 0.05,
            "no_bid": 0.95, "no_ask": 0.99,
            "mid_price": 0.03, "volume": 10.0,
            "market_id": "m1",
        }])
        result = build_strike_ladder_labels(bars, ladder)
        assert len(result) == 1
        assert result["label"].iloc[0] == 0

    def test_empty_bars_returns_empty(self):
        bars = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        ladder = _ladder()
        result = build_strike_ladder_labels(bars, ladder)
        assert result.empty

    def test_empty_ladder_returns_empty(self):
        bars = _bars()
        ladder = pd.DataFrame(columns=[
            "event_id", "event_close_time", "timestamp", "strike",
            "yes_bid", "yes_ask", "no_bid", "no_ask", "mid_price", "volume", "market_id"
        ])
        result = build_strike_ladder_labels(bars, ladder)
        assert result.empty

    def test_drops_event_with_no_underlying_data(self):
        bars = _bars(30)  # only 30 minutes of data
        close_time = pd.Timestamp("2030-01-01 01:00:00", tz="UTC")  # far in the future
        ladder = pd.DataFrame([{
            "event_id": "future",
            "event_close_time": close_time,
            "timestamp": close_time - pd.Timedelta(minutes=30),
            "strike": 100000.0,
            "yes_bid": 0.5, "yes_ask": 0.5,
            "no_bid": 0.5, "no_ask": 0.5,
            "mid_price": 0.5, "volume": 1.0,
            "market_id": "m",
        }])
        result = build_strike_ladder_labels(bars, ladder)
        assert result.empty
