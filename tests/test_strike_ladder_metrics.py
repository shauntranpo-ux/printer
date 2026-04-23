"""Tests for backtesting/metrics/strike_ladder_metrics.py."""
import sys
import os

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtesting.metrics.strike_ladder_metrics import (
    per_moneyness_calibration,
    per_event_pnl,
    c2_arbitrage_summary,
    strategy_c_full_summary,
)

_BUCKETS = ["deep_itm", "itm", "atm", "otm", "deep_otm"]


def _trade_log(n: int = 40, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    buckets = rng.choice(_BUCKETS, n)
    labels = rng.integers(0, 2, n)
    p_model = rng.uniform(0.3, 0.7, n)
    pnl = labels.astype(float) - p_model
    fee = np.full(n, 0.03)
    strategies = rng.choice(["strategy_c1", "strategy_c2"], n)
    event_ids = [f"ev_{i // 4}" for i in range(n)]
    return pd.DataFrame({
        "event_id": event_ids,
        "label": labels,
        "p_model": p_model,
        "moneyness_bucket": buckets,
        "pnl": pnl,
        "fee": fee,
        "strategy": strategies,
        "violation_type": [
            "monotonicity" if s == "strategy_c2" and rng.random() > 0.5 else
            ("convexity" if s == "strategy_c2" else None)
            for s in strategies
        ],
    })


class TestPerMoneynesscalibration:
    def test_returns_dataframe(self):
        df = per_moneyness_calibration(_trade_log())
        assert isinstance(df, pd.DataFrame)

    def test_has_all_buckets(self):
        df = per_moneyness_calibration(_trade_log(200))
        assert set(df["moneyness_bucket"]) == set(_BUCKETS)

    def test_brier_in_valid_range(self):
        df = per_moneyness_calibration(_trade_log(200))
        for _, row in df.iterrows():
            if row["n"] > 0 and row["brier"] == row["brier"]:  # not nan
                assert 0.0 <= row["brier"] <= 1.0

    def test_empty_df_returns_empty(self):
        result = per_moneyness_calibration(pd.DataFrame())
        assert result.empty

    def test_n_column_matches_counts(self):
        tl = _trade_log(100)
        df = per_moneyness_calibration(tl)
        for _, row in df.iterrows():
            expected = int((tl["moneyness_bucket"] == row["moneyness_bucket"]).sum())
            assert row["n"] == expected


class TestPerEventPnl:
    def test_returns_dataframe(self):
        result = per_event_pnl(_trade_log())
        assert isinstance(result, pd.DataFrame)

    def test_one_row_per_event(self):
        tl = _trade_log(40)
        result = per_event_pnl(tl)
        assert len(result) == tl["event_id"].nunique()

    def test_net_pnl_equals_pnl_minus_fee(self):
        tl = _trade_log(20)
        result = per_event_pnl(tl)
        for _, row in result.iterrows():
            sub = tl[tl["event_id"] == row["event_id"]]
            expected_net = float((sub["pnl"] - sub["fee"]).sum())
            assert abs(row["net_pnl"] - expected_net) < 1e-9

    def test_c1_c2_trade_counts_sum_to_total(self):
        tl = _trade_log(40)
        result = per_event_pnl(tl)
        for _, row in result.iterrows():
            assert row["c1_trades"] + row["c2_trades"] == row["n_trades"]

    def test_empty_df_returns_empty(self):
        result = per_event_pnl(pd.DataFrame())
        assert result.empty


class TestC2ArbitrageSummary:
    def test_returns_dict(self):
        result = c2_arbitrage_summary(_trade_log())
        assert isinstance(result, dict)

    def test_required_keys_present(self):
        result = c2_arbitrage_summary(_trade_log())
        for key in ["n_c2_trades", "n_monotonicity", "n_convexity", "gross_pnl", "net_pnl"]:
            assert key in result

    def test_empty_returns_zero_trades(self):
        result = c2_arbitrage_summary(pd.DataFrame())
        assert result["n_c2_trades"] == 0

    def test_counts_match_strategy_column(self):
        tl = _trade_log(40)
        c2_rows = tl[tl["strategy"] == "strategy_c2"]
        result = c2_arbitrage_summary(tl)
        assert result["n_c2_trades"] == len(c2_rows)


class TestStrategyCFullSummary:
    def test_returns_dict(self):
        result = strategy_c_full_summary(_trade_log())
        assert isinstance(result, dict)

    def test_combined_net_pnl_is_sum_of_net(self):
        tl = _trade_log(40)
        result = strategy_c_full_summary(tl)
        expected = float((tl["pnl"] - tl["fee"]).sum())
        assert abs(result["combined_net_pnl"] - expected) < 1e-9

    def test_empty_returns_zero_events(self):
        result = strategy_c_full_summary(pd.DataFrame())
        assert result.get("n_events_traded", 0) == 0

    def test_fee_drag_sign(self):
        tl = _trade_log(40)
        result = strategy_c_full_summary(tl)
        if result.get("fee_drag_pct") == result.get("fee_drag_pct"):  # not nan
            # Fee drag can be any sign depending on P&L sign, but total_fee should be positive
            assert result["total_fee"] >= 0.0
