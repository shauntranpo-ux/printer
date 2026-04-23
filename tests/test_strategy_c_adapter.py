"""Tests for backtesting/simulation/strategy_c_adapter.py."""
import sys
import os

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "strategies"))

from backtesting.simulation.strategy_c_adapter import (
    run_strategy_c_backtest,
    _build_snapshot_dict,
    _RESULT_COLUMNS,
)


def _bars(n: int = 200, base: float = 50000.0, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    start = pd.Timestamp("2024-01-01", tz="UTC")
    ts = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    prices = base + np.cumsum(rng.normal(0, 50, n))
    prices = np.abs(prices) + 1000.0
    return pd.DataFrame({
        "timestamp": ts,
        "open": prices,
        "high": prices * 1.001,
        "low": prices * 0.999,
        "close": prices * (1 + rng.normal(0, 0.0002, n)),
        "volume": rng.exponential(1.0, n),
    })


def _ladder_and_labels(
    n_events: int = 5,
    n_strikes: int = 6,
    base_price: float = 50000.0,
    strike_spacing: float = 100.0,
    seed: int = 2,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    bars = _bars(200, base_price)
    rows = []
    start = pd.Timestamp("2024-01-01 00:30:00", tz="UTC")
    for i in range(n_events):
        close_time = start + pd.Timedelta(hours=i + 1)
        eid = f"ev_{i:04d}"
        snap_ts = close_time - pd.Timedelta(minutes=20)
        for j in range(n_strikes):
            k = base_price - (n_strikes // 2) * strike_spacing + j * strike_spacing
            bid = float(np.clip(rng.uniform(0.10, 0.90), 0.01, 0.99))
            ask = float(np.clip(bid + 0.02, 0.01, 0.99))
            rows.append({
                "event_id": eid,
                "event_close_time": close_time,
                "timestamp": snap_ts,
                "strike": k,
                "yes_bid": bid, "yes_ask": ask,
                "no_bid": 1 - ask, "no_ask": 1 - bid,
                "mid_price": (bid + ask) / 2.0,
                "volume": float(rng.integers(10, 500)),
                "market_id": f"{eid}_{j}",
            })
    ladder = pd.DataFrame(rows)
    from backtesting.data.label_builder import build_strike_ladder_labels
    labels = build_strike_ladder_labels(bars, ladder)
    return bars, ladder, labels


def _minimal_config() -> dict:
    return {
        "asset": "btc",
        "volatility_reference": {"annualized": 0.43},
        "probability": {"risk_free_rate": 0.0},
        "fees": {"kalshi": {"taker_fee_rate": 0.03}, "safety_margin": 0.005},
        "moneyness": {
            "deep_itm_threshold": 0.30,
            "itm_threshold": 0.10,
            "otm_threshold": -0.10,
            "deep_otm_threshold": -0.30,
        },
        "c2_scanner": {
            "monotonicity_tolerance": 0.002,
            "convexity_tolerance": 0.002,
            "bounds_epsilon": 0.005,
        },
        "thresholds": {
            "base_min_edge": 0.055,
        },
    }


class TestBuildSnapshotDict:
    def test_returns_dict_with_strikes(self):
        rng = np.random.default_rng(0)
        rows = [{"strike": float(k), "yes_bid": 0.4, "yes_ask": 0.6,
                 "no_bid": 0.4, "no_ask": 0.6, "volume": 100.0, "market_id": f"m{k}"}
                for k in [100, 200, 300]]
        df = pd.DataFrame(rows)
        snap = _build_snapshot_dict("ev0", pd.Timestamp("2024-01-01", tz="UTC"), df)
        assert snap["event_id"] == "ev0"
        assert len(snap["strikes"]) == 3

    def test_normalizes_cents_to_decimal(self):
        rows = [{"strike": 100.0, "yes_bid": 40.0, "yes_ask": 60.0,
                 "no_bid": 40.0, "no_ask": 60.0, "volume": 100.0, "market_id": "m"}]
        df = pd.DataFrame(rows)
        snap = _build_snapshot_dict("ev0", pd.Timestamp("2024-01-01", tz="UTC"), df)
        strike = snap["strikes"][0]
        assert strike["yes_bid"] <= 1.0
        assert strike["yes_ask"] <= 1.0


class TestRunStrategyCBacktest:
    def test_returns_dataframe(self):
        bars, ladder, labels = _ladder_and_labels()
        config = _minimal_config()
        result = run_strategy_c_backtest("btc", ladder, labels, bars, config)
        assert isinstance(result, pd.DataFrame)

    def test_has_required_columns(self):
        bars, ladder, labels = _ladder_and_labels()
        config = _minimal_config()
        result = run_strategy_c_backtest("btc", ladder, labels, bars, config)
        for col in _RESULT_COLUMNS:
            assert col in result.columns

    def test_empty_ladder_returns_empty(self):
        bars = _bars()
        ladder = pd.DataFrame()
        labels = pd.DataFrame()
        config = _minimal_config()
        result = run_strategy_c_backtest("btc", ladder, labels, bars, config)
        assert result.empty

    def test_run_c2_only(self):
        bars, ladder, labels = _ladder_and_labels()
        config = _minimal_config()
        result = run_strategy_c_backtest("btc", ladder, labels, bars, config,
                                          run_c1=False, run_c2=True)
        assert isinstance(result, pd.DataFrame)
        if not result.empty:
            assert (result["strategy"] == "strategy_c2").all()

    def test_run_c1_only(self):
        bars, ladder, labels = _ladder_and_labels()
        config = _minimal_config()
        result = run_strategy_c_backtest("btc", ladder, labels, bars, config,
                                          run_c1=True, run_c2=False)
        assert isinstance(result, pd.DataFrame)
        if not result.empty:
            assert (result["strategy"] == "strategy_c1").all()

    def test_max_positions_respected(self):
        bars, ladder, labels = _ladder_and_labels(n_events=5, n_strikes=10)
        config = _minimal_config()
        config["thresholds"] = {"base_min_edge": 0.0}  # always trade
        result = run_strategy_c_backtest("btc", ladder, labels, bars, config,
                                          max_positions_per_event=1)
        if not result.empty:
            per_event = result.groupby("event_id").size()
            assert (per_event <= 1).all()

    def test_labels_are_binary(self):
        bars, ladder, labels = _ladder_and_labels()
        config = _minimal_config()
        result = run_strategy_c_backtest("btc", ladder, labels, bars, config)
        if not result.empty:
            assert set(result["label"].dropna().unique()).issubset({0, 1})

    def test_fill_price_bounded(self):
        bars, ladder, labels = _ladder_and_labels()
        config = _minimal_config()
        config["thresholds"] = {"base_min_edge": 0.0}
        result = run_strategy_c_backtest("btc", ladder, labels, bars, config)
        if not result.empty:
            assert (result["fill_price"].dropna() >= 0.0).all()
            assert (result["fill_price"].dropna() <= 1.5).all()
