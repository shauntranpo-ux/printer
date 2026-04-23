"""Tests for strategy_c.scanner.StrategyC2Scanner."""
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "strategies"))

from strategy_c.scanner import StrategyC2Scanner
from shared.types import ArbitrageSignal


_CONFIG = {
    "c2_scanner": {
        "monotonicity_tolerance": 0.002,
        "convexity_tolerance": 0.002,
        "bounds_epsilon": 0.005,
    },
    "fees": {
        "kalshi": {"taker_fee_rate": 0.03},
        "safety_margin": 0.005,
    },
}


def _valid_snapshot(probs=None):
    strikes = [100.0, 200.0, 300.0, 400.0, 500.0]
    if probs is None:
        # Linearly spaced → butterfly = 0 for every consecutive triple; no violations
        probs = [0.85, 0.675, 0.50, 0.325, 0.15]
    return {
        "event_id": "test",
        "event_close_time": None,
        "strikes": [
            {
                "strike": k,
                "yes_bid": max(0.01, p - 0.01),
                "yes_ask": min(0.99, p + 0.01),
                "no_bid": max(0.01, 1 - p - 0.01),
                "no_ask": min(0.99, 1 - p + 0.01),
                "last_price": p,
                "volume": 100.0,
                "market_id": f"mkt_{int(k)}",
            }
            for k, p in zip(strikes, probs)
        ],
    }


class TestStrategyC2Scanner:
    def test_valid_ladder_returns_empty_list(self):
        scanner = StrategyC2Scanner(_CONFIG)
        signals = scanner.scan_snapshot(_valid_snapshot())
        assert signals == []

    def test_returns_list(self):
        scanner = StrategyC2Scanner(_CONFIG)
        signals = scanner.scan_snapshot(_valid_snapshot())
        assert isinstance(signals, list)

    def test_monotonicity_violation_detected(self):
        # Inject a large monotonicity violation (probs[1] > probs[0] by 0.50)
        probs = [0.30, 0.80, 0.40, 0.20, 0.10]
        scanner = StrategyC2Scanner(_CONFIG)
        signals = scanner.scan_snapshot(_valid_snapshot(probs))
        mono = [s for s in signals if s.violation_type == "monotonicity"]
        assert len(mono) >= 1

    def test_signals_are_arbitrage_signal_instances(self):
        probs = [0.30, 0.80, 0.40, 0.20, 0.10]
        scanner = StrategyC2Scanner(_CONFIG)
        signals = scanner.scan_snapshot(_valid_snapshot(probs))
        for s in signals:
            assert isinstance(s, ArbitrageSignal)

    def test_signals_have_positive_profit(self):
        probs = [0.30, 0.80, 0.40, 0.20, 0.10]
        scanner = StrategyC2Scanner(_CONFIG)
        signals = scanner.scan_snapshot(_valid_snapshot(probs))
        for s in signals:
            assert s.theoretical_profit_per_contract > 0

    def test_malformed_snapshot_returns_empty(self):
        scanner = StrategyC2Scanner(_CONFIG)
        signals = scanner.scan_snapshot({"event_id": "bad", "event_close_time": None, "strikes": []})
        assert signals == []

    def test_convexity_violation_detected(self):
        # Large convexity violation at K2 (200)
        probs = [0.90, 0.10, 0.85, 0.20, 0.10]
        scanner = StrategyC2Scanner(_CONFIG)
        signals = scanner.scan_snapshot(_valid_snapshot(probs))
        conv = [s for s in signals if s.violation_type == "convexity"]
        assert len(conv) >= 1
