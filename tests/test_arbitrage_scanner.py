"""Tests for strategy_c.ladder.arbitrage_scanner."""
import sys
import os

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "strategies"))

from strategy_c.ladder.arbitrage_scanner import LadderArbitrageScanner


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


def _make_ladder(strikes, probs):
    rows = []
    for k, p in zip(strikes, probs):
        rows.append({
            "strike": k,
            "yes_bid": max(0.0, p - 0.01),
            "yes_ask": min(1.0, p + 0.01),
            "no_bid": max(0.0, 1 - p - 0.01),
            "no_ask": min(1.0, 1 - p + 0.01),
            "mid_price": p,
            "implied_probability": p,
            "volume": 100.0,
            "market_id": f"mkt_{int(k)}",
        })
    return pd.DataFrame(rows)


class TestLadderArbitrageScannerMonotone:
    def test_valid_monotone_ladder_no_signals(self):
        # Strictly decreasing probs -> no violations
        strikes = [100.0, 200.0, 300.0, 400.0, 500.0]
        probs = [0.80, 0.60, 0.40, 0.20, 0.10]
        df = _make_ladder(strikes, probs)
        scanner = LadderArbitrageScanner()
        signals = scanner.scan(df, _CONFIG)
        mono = [s for s in signals if s.violation_type == "monotonicity"]
        assert len(mono) == 0

    def test_monotonicity_violation_emits_signal(self):
        # p(K=200) > p(K=100) with large gap (should exceed hurdle)
        strikes = [100.0, 200.0, 300.0]
        probs = [0.30, 0.80, 0.10]   # p(200) > p(100) by 0.50 - clear violation
        df = _make_ladder(strikes, probs)
        scanner = LadderArbitrageScanner()
        signals = scanner.scan(df, _CONFIG)
        mono = [s for s in signals if s.violation_type == "monotonicity"]
        assert len(mono) >= 1
        # Signal should name both involved strikes
        assert 100.0 in mono[0].strikes_involved
        assert 200.0 in mono[0].strikes_involved

    def test_monotonicity_signal_recommended_legs(self):
        strikes = [100.0, 200.0, 300.0]
        probs = [0.30, 0.80, 0.10]
        df = _make_ladder(strikes, probs)
        scanner = LadderArbitrageScanner()
        signals = [s for s in scanner.scan(df, _CONFIG) if s.violation_type == "monotonicity"]
        assert len(signals) >= 1
        sig = signals[0]
        sides = {leg[1] for leg in sig.recommended_legs}
        # Should recommend trading both YES and NO sides
        assert "yes" in sides
        assert "no" in sides

    def test_monotonicity_signal_profit_positive(self):
        strikes = [100.0, 200.0]
        probs = [0.20, 0.85]   # large violation
        df = _make_ladder(strikes, probs)
        scanner = LadderArbitrageScanner()
        signals = [s for s in scanner.scan(df, _CONFIG) if s.violation_type == "monotonicity"]
        assert len(signals) >= 1
        assert signals[0].theoretical_profit_per_contract > 0


class TestLadderArbitrageScannerConvexity:
    def test_valid_convex_ladder_no_signals(self):
        # CDF should be convex: p(K1) - 2*p(K2) + p(K3) should be near 0 or positive
        strikes = [100.0, 200.0, 300.0]
        probs = [0.70, 0.50, 0.30]   # perfectly linear -> butterfly = 0
        df = _make_ladder(strikes, probs)
        scanner = LadderArbitrageScanner()
        signals = [s for s in scanner.scan(df, _CONFIG) if s.violation_type == "convexity"]
        assert len(signals) == 0

    def test_convexity_violation_emits_signal(self):
        # Inject large convexity violation: p2 is too low
        strikes = [100.0, 200.0, 300.0]
        probs = [0.90, 0.10, 0.80]   # p(K1) - 2*p(K2) + p(K3) = 0.90 - 0.20 + 0.80 = 1.50 >> 0
        df = _make_ladder(strikes, probs)
        scanner = LadderArbitrageScanner()
        signals = [s for s in scanner.scan(df, _CONFIG) if s.violation_type == "convexity"]
        assert len(signals) >= 1
        sig = signals[0]
        assert set(sig.strikes_involved) == {100.0, 200.0, 300.0}
        assert sig.theoretical_profit_per_contract > 0

    def test_convexity_signal_recommended_legs(self):
        strikes = [100.0, 200.0, 300.0]
        probs = [0.90, 0.10, 0.80]
        df = _make_ladder(strikes, probs)
        scanner = LadderArbitrageScanner()
        signals = [s for s in scanner.scan(df, _CONFIG) if s.violation_type == "convexity"]
        assert len(signals) >= 1
        sig = signals[0]
        assert len(sig.recommended_legs) == 3  # K1, 2*K2, K3


class TestLadderArbitrageScannerBounds:
    def test_empty_ladder_no_signals(self):
        df = pd.DataFrame(columns=["strike", "implied_probability"])
        scanner = LadderArbitrageScanner()
        signals = scanner.scan(df, _CONFIG)
        assert signals == []

    def test_sub_hurdle_violation_not_emitted(self):
        # Tiny monotonicity violation: below the fee hurdle
        strikes = [100.0, 200.0]
        probs = [0.500, 0.501]   # violation = 0.001 << hurdle
        df = _make_ladder(strikes, probs)
        scanner = LadderArbitrageScanner()
        signals = scanner.scan(df, _CONFIG)
        mono = [s for s in signals if s.violation_type == "monotonicity"]
        assert len(mono) == 0
