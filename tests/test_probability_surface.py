"""Tests for strategy_c.probability.probability_surface."""
import sys
import os
import math

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "strategies"))

from strategy_c.probability.probability_surface import ProbabilitySurface
from strategy_c.probability.digital_call import flat_vol_to_integrated_variance


def _make_ladder(strikes, spot=100.0):
    """Build a minimal synthetic ladder DataFrame with implied_probability."""
    rows = []
    for k in strikes:
        # Use a realistic CDF: higher strike → lower prob
        prob = max(0.01, min(0.99, 0.5 - 0.05 * (k - spot)))
        rows.append({
            "strike": k,
            "yes_bid": max(0.0, prob - 0.01),
            "yes_ask": min(1.0, prob + 0.01),
            "no_bid": max(0.0, 1 - prob - 0.01),
            "no_ask": min(1.0, 1 - prob + 0.01),
            "mid_price": prob,
            "implied_probability": prob,
            "volume": 100.0,
            "market_id": f"mkt_{int(k)}",
        })
    return pd.DataFrame(rows)


_BASE_CONFIG = {
    "volatility_reference": {"annualized": 0.50},
    "probability": {"risk_free_rate": 0.0},
    "moneyness": {
        "deep_itm_log_moneyness_cutoff": -0.02,
        "itm_log_moneyness_cutoff": -0.005,
        "atm_log_moneyness_cutoff": 0.005,
        "otm_log_moneyness_cutoff": 0.02,
    },
}


class TestProbabilitySurface:
    def setup_method(self):
        self.surface = ProbabilitySurface(calibrators={})

    def test_output_has_one_row_per_strike(self):
        strikes = [90.0, 95.0, 100.0, 105.0, 110.0]
        ladder = _make_ladder(strikes)
        iv = flat_vol_to_integrated_variance(0.02, 3600.0)
        df = self.surface.evaluate(100.0, ladder, 3600.0, iv, {}, _BASE_CONFIG)
        assert len(df) == len(strikes)
        assert set(df["strike"].tolist()) == set(strikes)

    def test_model_probs_monotone_non_increasing(self):
        # Under GBM, P(S_T > K) must decrease as K increases
        strikes = [85.0, 90.0, 95.0, 100.0, 105.0, 110.0, 115.0]
        ladder = _make_ladder(strikes)
        iv = flat_vol_to_integrated_variance(0.02, 3600.0)
        df = self.surface.evaluate(100.0, ladder, 3600.0, iv, {}, _BASE_CONFIG)
        df = df.sort_values("strike")
        probs = df["p_model"].tolist()
        for i in range(len(probs) - 1):
            assert probs[i] >= probs[i + 1] - 1e-10, (
                f"Monotonicity violated: p({strikes[i]})={probs[i]:.4f} < "
                f"p({strikes[i+1]})={probs[i+1]:.4f}"
            )

    def test_edge_column_is_signed(self):
        strikes = [90.0, 100.0, 110.0]
        ladder = _make_ladder(strikes)
        iv = flat_vol_to_integrated_variance(0.02, 3600.0)
        df = self.surface.evaluate(100.0, ladder, 3600.0, iv, {}, _BASE_CONFIG)
        assert "edge" in df.columns
        assert df["edge"].dtype.kind == "f"  # float
        # Edge can be positive or negative — just verify it's computed correctly
        for _, row in df.iterrows():
            assert abs(row["edge"] - (row["p_model"] - row["p_market"])) < 1e-10

    def test_output_columns_present(self):
        ladder = _make_ladder([95.0, 100.0, 105.0])
        iv = flat_vol_to_integrated_variance(0.02, 3600.0)
        df = self.surface.evaluate(100.0, ladder, 3600.0, iv, {}, _BASE_CONFIG)
        for col in ("strike", "p_model", "p_market", "edge", "moneyness_bucket"):
            assert col in df.columns, f"Missing column: {col}"

    def test_empty_ladder_returns_empty(self):
        empty = pd.DataFrame(columns=["strike", "yes_bid", "yes_ask", "no_bid", "no_ask",
                                       "mid_price", "implied_probability", "volume", "market_id"])
        iv = flat_vol_to_integrated_variance(0.02, 3600.0)
        df = self.surface.evaluate(100.0, empty, 3600.0, iv, {}, _BASE_CONFIG)
        assert df.empty

    def test_probabilities_in_unit_interval(self):
        strikes = list(range(70, 135, 5))
        ladder = _make_ladder([float(k) for k in strikes])
        iv = flat_vol_to_integrated_variance(0.02, 3600.0)
        df = self.surface.evaluate(100.0, ladder, 3600.0, iv, {}, _BASE_CONFIG)
        assert (df["p_model"] >= 0.0).all()
        assert (df["p_model"] <= 1.0).all()
