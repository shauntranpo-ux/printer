"""Tests for backtesting/training/strategy_c_fitter.py."""
import sys
import os
import pickle
import tempfile

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "strategies"))

from backtesting.training.strategy_c_fitter import fit_strategy_c, load_fitted_strategy_c


def _make_bars(n: int = 500, base_price: float = 50000.0, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    start = pd.Timestamp("2024-01-01", tz="UTC")
    ts = pd.date_range(start, periods=n, freq="1min", tz="UTC")
    prices = base_price + np.cumsum(rng.normal(0, 100, n))
    prices = np.abs(prices) + 1000.0
    return pd.DataFrame({
        "timestamp": ts,
        "open": prices,
        "high": prices * (1 + np.abs(rng.normal(0, 0.001, n))),
        "low": prices * (1 - np.abs(rng.normal(0, 0.001, n))),
        "close": prices * (1 + rng.normal(0, 0.0005, n)),
        "volume": rng.exponential(1.0, n),
    })


def _make_ladder_df(
    n_events: int = 10,
    base_price: float = 50000.0,
    strike_spacing: float = 100.0,
    n_strikes: int = 8,
    seed: int = 1,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    start = pd.Timestamp("2024-01-01 01:00:00", tz="UTC")
    for i in range(n_events):
        close_time = start + pd.Timedelta(hours=i + 1)
        event_id = f"event_{i:04d}"
        entry_snap = close_time - pd.Timedelta(minutes=30)
        for j in range(n_strikes):
            strike = base_price - (n_strikes // 2) * strike_spacing + j * strike_spacing
            bid = float(np.clip(rng.uniform(0.05, 0.95), 0.01, 0.99))
            ask = float(np.clip(bid + rng.uniform(0.01, 0.05), 0.01, 0.99))
            rows.append({
                "event_id": event_id,
                "event_close_time": close_time,
                "timestamp": entry_snap,
                "strike": strike,
                "yes_bid": bid,
                "yes_ask": ask,
                "no_bid": 1.0 - ask,
                "no_ask": 1.0 - bid,
                "mid_price": (bid + ask) / 2.0,
                "volume": float(rng.integers(100, 1000)),
                "market_id": f"mkt_{event_id}_{j}",
            })
    return pd.DataFrame(rows)


def _make_labels_df(
    ladder_df: pd.DataFrame,
    bars: pd.DataFrame,
) -> pd.DataFrame:
    from backtesting.data.label_builder import build_strike_ladder_labels
    return build_strike_ladder_labels(bars, ladder_df)


def _minimal_config() -> dict:
    return {
        "asset": "btc",
        "volatility_reference": {"annualized": 0.43},
        "probability": {"risk_free_rate": 0.0},
        "fees": {"kalshi": {"taker_fee_rate": 0.03}, "safety_margin": 0.005},
        "calibration": {"per_bucket": {}},
        "moneyness": {
            "deep_itm_threshold": 0.30,
            "itm_threshold": 0.10,
            "otm_threshold": -0.10,
            "deep_otm_threshold": -0.30,
        },
    }


class TestFitStrategyC:
    def test_returns_dict_with_status_ok(self, tmp_path):
        bars = _make_bars(600)
        ladder = _make_ladder_df(12)
        labels = _make_labels_df(ladder, bars)
        config = _minimal_config()
        result = fit_strategy_c(
            asset="btc",
            underlying_bars=bars,
            ladder_df=ladder,
            labels_df=labels,
            config=config,
            output_dir=str(tmp_path),
        )
        assert isinstance(result, dict)
        assert result["status"] == "ok"

    def test_fitted_config_path_written(self, tmp_path):
        bars = _make_bars(600)
        ladder = _make_ladder_df(12)
        labels = _make_labels_df(ladder, bars)
        config = _minimal_config()
        result = fit_strategy_c("btc", bars, ladder, labels, config, output_dir=str(tmp_path))
        assert os.path.exists(result["fitted_config_path"])

    def test_drift_weight_is_float(self, tmp_path):
        bars = _make_bars(600)
        ladder = _make_ladder_df(12)
        labels = _make_labels_df(ladder, bars)
        config = _minimal_config()
        result = fit_strategy_c("btc", bars, ladder, labels, config, output_dir=str(tmp_path))
        assert isinstance(result["drift_adjustment_weight"], float)

    def test_calibration_rows_positive(self, tmp_path):
        bars = _make_bars(600)
        ladder = _make_ladder_df(12)
        labels = _make_labels_df(ladder, bars)
        config = _minimal_config()
        result = fit_strategy_c("btc", bars, ladder, labels, config, output_dir=str(tmp_path))
        assert result["n_calibration_rows"] > 0

    def test_rejects_sol(self, tmp_path):
        bars = _make_bars(200)
        ladder = _make_ladder_df(5)
        labels = pd.DataFrame(columns=["event_id", "strike", "event_close_time", "label", "reference_price_close"])
        config = _minimal_config()
        with pytest.raises(ValueError, match="BTC and ETH"):
            fit_strategy_c("sol", bars, ladder, labels, config, output_dir=str(tmp_path))

    def test_empty_ladder_raises(self, tmp_path):
        bars = _make_bars(200)
        ladder = pd.DataFrame(columns=[
            "event_id", "event_close_time", "timestamp", "strike",
            "yes_bid", "yes_ask", "no_bid", "no_ask", "mid_price", "volume", "market_id"
        ])
        labels = pd.DataFrame(columns=["event_id", "strike", "event_close_time", "label", "reference_price_close"])
        config = _minimal_config()
        with pytest.raises(ValueError):
            fit_strategy_c("btc", bars, ladder, labels, config, output_dir=str(tmp_path))

    def test_calibrator_pickles_loadable(self, tmp_path):
        bars = _make_bars(1000)
        ladder = _make_ladder_df(50, n_strikes=10)
        labels = _make_labels_df(ladder, bars)
        config = _minimal_config()
        result = fit_strategy_c("btc", bars, ladder, labels, config, output_dir=str(tmp_path))
        for bucket, path in result["calibrator_paths"].items():
            if path is not None:
                assert os.path.exists(path)
                with open(path, "rb") as f:
                    obj = pickle.load(f)
                assert obj is not None

    def test_eth_asset_accepted(self, tmp_path):
        bars = _make_bars(600, base_price=3000.0)
        config = _minimal_config()
        config["asset"] = "eth"
        config["volatility_reference"] = {"annualized": 0.77}
        ladder = _make_ladder_df(12, base_price=3000.0, strike_spacing=20.0)
        labels = _make_labels_df(ladder, bars)
        result = fit_strategy_c("eth", bars, ladder, labels, config, output_dir=str(tmp_path))
        assert result["status"] == "ok"
