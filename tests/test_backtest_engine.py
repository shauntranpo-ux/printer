"""
Tests for backtesting/simulation/backtest_engine.py

TDD: these tests are written before the implementation exists.
"""
import sys
import os

import numpy as np
import pandas as pd
import pytest

from backtesting.simulation.backtest_engine import run_backtest
from backtesting.data.aligner import Event, build_event_stream

# ── helpers ───────────────────────────────────────────────────────────────────

_FEES = {
    "kalshi": {"taker_fee_rate": 0.03, "maker_fee_rate": 0.00},
    "safety_margin": 0.005,
}

_CFG = {
    "model": {"type": "logistic_regression", "calibration": "isotonic"},
    "thresholds": {
        "edge_above_fee": {
            "asia_deep_night": 0.02,
            "asia_active": 0.02,
            "eu_open": 0.02,
            "eu_us_overlap": 0.02,
            "us_afternoon": 0.02,
            "us_late": 0.02,
            "weekend": 0.03,
        },
        "btc_degraded_penalty": 0.01,
    },
}

_REQUIRED_COLUMNS = [
    "entry_time", "exit_time", "asset", "strategy", "side",
    "p_model", "p_market", "edge", "regime",
    "fill_price", "pnl", "fee", "label",
]


def _make_labels(n=3, freq="15min", start="2024-01-01 10:00:00"):
    ts = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    labels = [1, 0, 1][:n] + [1] * max(0, n - 3)
    ref_open = [100.0 + i for i in range(n)]
    ref_close = [101.0 + i for i in range(n)]
    log_ret = [0.01] * n
    return pd.DataFrame({
        "timestamp": ts,
        "label": labels[:n],
        "reference_price_open": ref_open,
        "reference_price_close": ref_close,
        "log_return": log_ret,
    })


def _make_kalshi_ticks(n=3, freq="15min", start="2024-01-01 10:00:00",
                       yes_bid=45, yes_ask=55, no_bid=45, no_ask=55):
    ts = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({
        "timestamp": ts,
        "yes_bid": [yes_bid] * n,
        "yes_ask": [yes_ask] * n,
        "no_bid": [no_bid] * n,
        "no_ask": [no_ask] * n,
    })


def _make_bars(n=3, freq="15min", start="2024-01-01 10:00:00", close=100.0):
    ts = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({
        "timestamp": ts,
        "open":   [close] * n,
        "high":   [close + 1] * n,
        "low":    [close - 1] * n,
        "close":  [close] * n,
        "volume": [1000.0] * n,
    })


class _AlwaysTradeModel:
    """Stub StrategyAModel that always triggers a trade with a large edge."""
    def __init__(self, p_model=0.80):
        self._p_model = p_model
        self.config = _CFG
        self.fees = _FEES

    def predict_proba(self, features: dict) -> float:
        return self._p_model

    def get_edge(self, p_model: float, p_market: float) -> float:
        return p_model - p_market

    def should_trade(self, p_model, p_market, regime, config, btc_degraded=False) -> bool:
        return True  # always trade


class _NeverTradeModel:
    """Stub StrategyAModel that never triggers a trade."""
    def predict_proba(self, features: dict) -> float:
        return 0.5

    def get_edge(self, p_model: float, p_market: float) -> float:
        return 0.0

    def should_trade(self, p_model, p_market, regime, config, btc_degraded=False) -> bool:
        return False


# ── Test 1: Empty inputs → empty DataFrame with correct columns ───────────────

class TestEmptyInputs:
    def test_empty_events_empty_labels_returns_empty_df(self):
        labels = pd.DataFrame(columns=["timestamp", "label",
                                        "reference_price_open",
                                        "reference_price_close",
                                        "log_return"])
        result = run_backtest(
            events=[],
            labels=labels,
            asset="btc",
            strategy="strategy_a",
        )
        assert isinstance(result, pd.DataFrame)
        assert result.empty

    def test_empty_result_has_correct_columns(self):
        labels = pd.DataFrame(columns=["timestamp", "label",
                                        "reference_price_open",
                                        "reference_price_close",
                                        "log_return"])
        result = run_backtest(
            events=[],
            labels=labels,
            asset="btc",
            strategy="strategy_a",
        )
        for col in _REQUIRED_COLUMNS:
            assert col in result.columns, f"Missing column: {col}"


# ── Test 2: Trade record emitted when should_trade fires ─────────────────────

class TestTradeRecordEmission:
    def test_trade_emitted_per_window_when_always_trade(self):
        labels = _make_labels(n=2)
        ticks = _make_kalshi_ticks(
            n=2,
            # Place ticks BEFORE the label timestamps so iter_windows sees them
            start="2024-01-01 09:45:00",
        )
        bars = _make_bars(n=2, start="2024-01-01 09:30:00")
        events = build_event_stream(bars=bars, labels=labels, kalshi_ticks=ticks)
        model = _AlwaysTradeModel(p_model=0.80)

        result = run_backtest(
            events=events,
            labels=labels,
            asset="btc",
            strategy="strategy_a",
            model_a=model,
            model_config=_CFG,
            fees_config=_FEES,
        )
        # 2 label windows → 2 trades
        assert len(result) == 2

    def test_no_trade_when_never_trade(self):
        labels = _make_labels(n=2)
        ticks = _make_kalshi_ticks(n=2, start="2024-01-01 09:45:00")
        bars = _make_bars(n=2, start="2024-01-01 09:30:00")
        events = build_event_stream(bars=bars, labels=labels, kalshi_ticks=ticks)
        model = _NeverTradeModel()

        result = run_backtest(
            events=events,
            labels=labels,
            asset="btc",
            strategy="strategy_a",
            model_a=model,
            model_config=_CFG,
            fees_config=_FEES,
        )
        assert len(result) == 0

    def test_no_model_a_produces_no_trades_for_strategy_a(self):
        labels = _make_labels(n=2)
        events = []
        result = run_backtest(
            events=events,
            labels=labels,
            asset="btc",
            strategy="strategy_a",
            model_a=None,
        )
        assert len(result) == 0


# ── Test 3: P&L correctness ───────────────────────────────────────────────────

class TestPnLCorrectness:
    def test_yes_win_pnl(self):
        """side=yes, label=1, fill_price=0.60 → pnl = 1 - 0.60 = 0.40"""
        # Build a single-window scenario with yes_ask=60 (fill_price=0.60)
        labels = _make_labels(n=1, start="2024-01-01 10:00:00")
        labels["label"] = 1
        # tick placed at 09:59 so it's strictly before window at 10:00
        ticks = _make_kalshi_ticks(
            n=1,
            start="2024-01-01 09:59:00",
            yes_bid=50, yes_ask=60,
        )
        bars = _make_bars(n=1, start="2024-01-01 09:30:00")
        events = build_event_stream(bars=bars, labels=labels, kalshi_ticks=ticks)
        # p_model=0.90 >> p_market ≈ 0.55 (mid) → side=yes
        model = _AlwaysTradeModel(p_model=0.90)

        result = run_backtest(
            events=events,
            labels=labels,
            asset="btc",
            strategy="strategy_a",
            model_a=model,
            model_config=_CFG,
            fees_config=_FEES,
            latency_ms=0.0,
        )
        assert len(result) == 1
        row = result.iloc[0]
        assert row["side"] == "yes"
        assert row["fill_price"] == pytest.approx(0.60)
        assert row["pnl"] == pytest.approx(1.0 - 0.60)

    def test_no_win_pnl(self):
        """side=no, label=0, fill_price=0.40 → pnl = (1-0) - 0.40 = 0.60"""
        labels = _make_labels(n=1, start="2024-01-01 10:00:00")
        labels["label"] = 0
        # p_model=0.10 < p_market ≈ 0.50 → side=no → fills at no_ask=40
        ticks = _make_kalshi_ticks(
            n=1,
            start="2024-01-01 09:59:00",
            yes_bid=50, yes_ask=60,
            no_bid=30, no_ask=40,
        )
        bars = _make_bars(n=1, start="2024-01-01 09:30:00")
        events = build_event_stream(bars=bars, labels=labels, kalshi_ticks=ticks)
        model = _AlwaysTradeModel(p_model=0.10)

        result = run_backtest(
            events=events,
            labels=labels,
            asset="btc",
            strategy="strategy_a",
            model_a=model,
            model_config=_CFG,
            fees_config=_FEES,
            latency_ms=0.0,
        )
        assert len(result) == 1
        row = result.iloc[0]
        assert row["side"] == "no"
        assert row["fill_price"] == pytest.approx(0.40)
        assert row["pnl"] == pytest.approx((1 - 0) - 0.40)

    def test_yes_loss_pnl(self):
        """side=yes, label=0, fill_price=0.60 → pnl = 0 - 0.60 = -0.60"""
        labels = _make_labels(n=1, start="2024-01-01 10:00:00")
        labels["label"] = 0
        ticks = _make_kalshi_ticks(
            n=1,
            start="2024-01-01 09:59:00",
            yes_bid=50, yes_ask=60,
        )
        bars = _make_bars(n=1, start="2024-01-01 09:30:00")
        events = build_event_stream(bars=bars, labels=labels, kalshi_ticks=ticks)
        model = _AlwaysTradeModel(p_model=0.90)

        result = run_backtest(
            events=events,
            labels=labels,
            asset="btc",
            strategy="strategy_a",
            model_a=model,
            model_config=_CFG,
            fees_config=_FEES,
            latency_ms=0.0,
        )
        assert len(result) == 1
        row = result.iloc[0]
        assert row["side"] == "yes"
        assert row["pnl"] == pytest.approx(0 - 0.60)

    def test_no_loss_pnl(self):
        """side=no, label=1 (YES wins, so NO loses), fill_price=0.40 → pnl = (1-1) - 0.40 = -0.40"""
        labels = _make_labels(n=1, start="2024-01-01 10:00:00")
        labels["label"] = 1
        # p_model=0.10 < p_market ≈ 0.50 → side=no → fills at no_ask=40
        ticks = _make_kalshi_ticks(
            n=1,
            start="2024-01-01 09:59:00",
            yes_bid=50, yes_ask=60,
            no_bid=30, no_ask=40,
        )
        bars = _make_bars(n=1, start="2024-01-01 09:30:00")
        events = build_event_stream(bars=bars, labels=labels, kalshi_ticks=ticks)
        model = _AlwaysTradeModel(p_model=0.10)

        result = run_backtest(
            events=events,
            labels=labels,
            asset="btc",
            strategy="strategy_a",
            model_a=model,
            model_config=_CFG,
            fees_config=_FEES,
            latency_ms=0.0,
        )
        assert len(result) == 1
        row = result.iloc[0]
        assert row["side"] == "no"
        assert row["fill_price"] == pytest.approx(0.40)
        assert row["pnl"] == pytest.approx((1 - 1) - 0.40)  # = -0.40


# ── Test 4: No look-ahead ─────────────────────────────────────────────────────

class TestNoLookAhead:
    def test_events_at_window_ts_are_excluded(self):
        """Events timestamped exactly at window_ts must NOT be visible."""
        window_ts = pd.Timestamp("2024-01-01 10:00:00", tz="UTC")
        labels = _make_labels(n=1, start="2024-01-01 10:00:00")
        labels["label"] = 1

        # ONE tick exactly at window_ts (should be invisible)
        # ONE tick 1 second before (should be visible)
        ticks_df = pd.DataFrame({
            "timestamp": [
                window_ts - pd.Timedelta(seconds=1),  # visible
                window_ts,                              # NOT visible
            ],
            "yes_bid": [40, 80],
            "yes_ask": [60, 99],  # if look-ahead, fill_price=0.99; else 0.60
            "no_bid":  [40, 10],
            "no_ask":  [60, 20],
        })
        bars = _make_bars(n=1, start="2024-01-01 09:30:00")
        events = build_event_stream(bars=bars, labels=labels, kalshi_ticks=ticks_df)
        model = _AlwaysTradeModel(p_model=0.90)

        result = run_backtest(
            events=events,
            labels=labels,
            asset="btc",
            strategy="strategy_a",
            model_a=model,
            model_config=_CFG,
            fees_config=_FEES,
            latency_ms=0.0,
        )
        assert len(result) == 1
        # Should have used the tick at 09:59:59 (yes_ask=60 → 0.60), not the one at 10:00
        assert result.iloc[0]["fill_price"] == pytest.approx(0.60), (
            f"Expected 0.60 (no look-ahead), got {result.iloc[0]['fill_price']}"
        )


# ── Test 5: Required columns ──────────────────────────────────────────────────

class TestRequiredColumns:
    def test_trade_log_has_all_required_columns(self):
        labels = _make_labels(n=1)
        ticks = _make_kalshi_ticks(n=1, start="2024-01-01 09:59:00")
        bars = _make_bars(n=1, start="2024-01-01 09:30:00")
        events = build_event_stream(bars=bars, labels=labels, kalshi_ticks=ticks)
        model = _AlwaysTradeModel(p_model=0.90)

        result = run_backtest(
            events=events,
            labels=labels,
            asset="eth",
            strategy="strategy_a",
            model_a=model,
            model_config=_CFG,
            fees_config=_FEES,
        )
        for col in _REQUIRED_COLUMNS:
            assert col in result.columns, f"Missing column: {col}"

    def test_asset_and_strategy_columns_populated(self):
        labels = _make_labels(n=1)
        ticks = _make_kalshi_ticks(n=1, start="2024-01-01 09:59:00")
        bars = _make_bars(n=1, start="2024-01-01 09:30:00")
        events = build_event_stream(bars=bars, labels=labels, kalshi_ticks=ticks)
        model = _AlwaysTradeModel()

        result = run_backtest(
            events=events,
            labels=labels,
            asset="sol",
            strategy="strategy_a",
            model_a=model,
            model_config=_CFG,
            fees_config=_FEES,
        )
        assert len(result) >= 1
        assert result.iloc[0]["asset"] == "sol"
        assert result.iloc[0]["strategy"] == "strategy_a"


# ── Test 6: No Kalshi ticks → p_market defaults to 0.5 ───────────────────────

class TestNoKalshiTicks:
    def test_p_market_defaults_to_half_when_no_ticks(self):
        labels = _make_labels(n=1, start="2024-01-01 10:00:00")
        labels["label"] = 1
        bars = _make_bars(n=1, start="2024-01-01 09:30:00")
        # No kalshi_ticks
        events = build_event_stream(bars=bars, labels=labels)
        model = _AlwaysTradeModel(p_model=0.90)

        result = run_backtest(
            events=events,
            labels=labels,
            asset="btc",
            strategy="strategy_a",
            model_a=model,
            model_config=_CFG,
            fees_config=_FEES,
        )
        assert len(result) == 1
        assert result.iloc[0]["p_market"] == pytest.approx(0.5)

    def test_fill_uses_p_market_when_no_ticks(self):
        labels = _make_labels(n=1, start="2024-01-01 10:00:00")
        labels["label"] = 1
        bars = _make_bars(n=1, start="2024-01-01 09:30:00")
        events = build_event_stream(bars=bars, labels=labels)
        # p_model=0.90, p_market=0.50 → side=yes → fill_price = p_market = 0.50
        model = _AlwaysTradeModel(p_model=0.90)

        result = run_backtest(
            events=events,
            labels=labels,
            asset="btc",
            strategy="strategy_a",
            model_a=model,
            model_config=_CFG,
            fees_config=_FEES,
        )
        assert len(result) == 1
        # fill_price = p_market = 0.5 (yes side)
        assert result.iloc[0]["fill_price"] == pytest.approx(0.5)


# ── Test 7: strategy_b with model_b=None → empty trade log ───────────────────

class TestStrategyBNoModel:
    def test_strategy_b_no_model_returns_empty(self):
        labels = _make_labels(n=2)
        bars = _make_bars(n=2, start="2024-01-01 09:30:00")
        events = build_event_stream(bars=bars, labels=labels)

        result = run_backtest(
            events=events,
            labels=labels,
            asset="eth",
            strategy="strategy_b",
            model_b=None,
        )
        assert isinstance(result, pd.DataFrame)
        assert result.empty

    def test_strategy_b_no_model_has_correct_columns(self):
        labels = _make_labels(n=1)
        bars = _make_bars(n=1, start="2024-01-01 09:30:00")
        events = build_event_stream(bars=bars, labels=labels)

        result = run_backtest(
            events=events,
            labels=labels,
            asset="eth",
            strategy="strategy_b",
            model_b=None,
        )
        for col in _REQUIRED_COLUMNS:
            assert col in result.columns, f"Missing column: {col}"


# ── Test 8: fee applied on taker fills ────────────────────────────────────────

class TestFeeApplied:
    def test_3pct_fee_on_taker_fill(self):
        labels = _make_labels(n=1, start="2024-01-01 10:00:00")
        labels["label"] = 1
        ticks = _make_kalshi_ticks(
            n=1,
            start="2024-01-01 09:59:00",
            yes_bid=50, yes_ask=60,
        )
        bars = _make_bars(n=1, start="2024-01-01 09:30:00")
        events = build_event_stream(bars=bars, labels=labels, kalshi_ticks=ticks)
        model = _AlwaysTradeModel(p_model=0.90)

        result = run_backtest(
            events=events,
            labels=labels,
            asset="btc",
            strategy="strategy_a",
            model_a=model,
            model_config=_CFG,
            fees_config=_FEES,
            fill_model_type="taker",
            latency_ms=0.0,
        )
        assert len(result) == 1
        row = result.iloc[0]
        # fill_price = 0.60; fee = 0.03 * 0.60 = 0.018
        assert row["fee"] == pytest.approx(0.03 * row["fill_price"])


# ── Test 9: maker fill model integration ─────────────────────────────────────

class TestMakerFillModelIntegration:
    def test_maker_fill_type_accepted(self):
        labels = _make_labels(n=1, start="2024-01-01 10:00:00")
        ticks = _make_kalshi_ticks(n=1, start="2024-01-01 09:59:00")
        bars = _make_bars(n=1, start="2024-01-01 09:30:00")
        events = build_event_stream(bars=bars, labels=labels, kalshi_ticks=ticks)
        model = _AlwaysTradeModel(p_model=0.90)

        # Should not raise — result may be empty if maker RNG doesn't fill
        result = run_backtest(
            events=events,
            labels=labels,
            asset="btc",
            strategy="strategy_a",
            model_a=model,
            model_config=_CFG,
            fees_config=_FEES,
            fill_model_type="maker",
        )
        assert isinstance(result, pd.DataFrame)
        for col in _REQUIRED_COLUMNS:
            assert col in result.columns


# ── Test 10: entry_time and exit_time correctness ─────────────────────────────

class TestEntryExitTime:
    def test_entry_time_is_window_ts_and_exit_15min_later(self):
        labels = _make_labels(n=1, start="2024-01-01 10:00:00")
        ticks = _make_kalshi_ticks(n=1, start="2024-01-01 09:59:00")
        bars = _make_bars(n=1, start="2024-01-01 09:30:00")
        events = build_event_stream(bars=bars, labels=labels, kalshi_ticks=ticks)
        model = _AlwaysTradeModel(p_model=0.90)

        result = run_backtest(
            events=events,
            labels=labels,
            asset="btc",
            strategy="strategy_a",
            model_a=model,
            model_config=_CFG,
            fees_config=_FEES,
        )
        assert len(result) == 1
        row = result.iloc[0]
        assert row["entry_time"] == pd.Timestamp("2024-01-01 10:00:00", tz="UTC")
        assert row["exit_time"] == pd.Timestamp("2024-01-01 10:15:00", tz="UTC")
