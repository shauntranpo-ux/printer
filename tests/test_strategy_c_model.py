"""Tests for strategy_c.model.StrategyC1Model."""
import sys
import os
import math

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "strategies"))

from strategy_c.model import StrategyC1Model


_CONFIG = {
    "volatility_reference": {"annualized": 0.50},
    "probability": {
        "risk_free_rate": 0.0,
        "drift_adjustment_enabled": False,
        "drift_adjustment_weight": None,
    },
    "moneyness": {
        "deep_itm_log_moneyness_cutoff": -0.02,
        "itm_log_moneyness_cutoff": -0.005,
        "atm_log_moneyness_cutoff": 0.005,
        "otm_log_moneyness_cutoff": 0.02,
    },
    "calibration": {
        "per_bucket": {
            "deep_itm": "isotonic", "itm": "isotonic", "atm": "isotonic",
            "otm": "isotonic", "deep_otm": "platt",
        },
        "artifact_paths": {
            "deep_itm": None, "itm": None, "atm": None, "otm": None, "deep_otm": None,
        },
    },
    "vol_term_structure": {
        "sub_interval_minutes": 15,
        "regime_multipliers": {},
    },
    "thresholds": {
        "base_edge_above_fee": {
            "us_afternoon": 0.02,
        },
        "moneyness_multiplier": {
            "deep_itm": 2.0, "itm": 1.0, "atm": 1.0, "otm": 1.0, "deep_otm": 2.0,
        },
        "longshot_buy_penalty": 0.02,
    },
    "selection": {
        "max_positions_per_event": 2,
        "min_strike_spacing_count": 3,
    },
}


def _make_snapshot(spot=100.0, strikes=None, tte=3600.0):
    if strikes is None:
        strikes = [90.0, 95.0, 100.0, 105.0, 110.0]
    # Create a decreasing implied_probability CDF
    snapshot_strikes = []
    for k in sorted(strikes):
        prob = max(0.05, min(0.95, 0.5 - 0.05 * (k - spot)))
        snapshot_strikes.append({
            "strike": k,
            "yes_bid": max(0.01, prob - 0.01),
            "yes_ask": min(0.99, prob + 0.01),
            "no_bid": max(0.01, 1 - prob - 0.01),
            "no_ask": min(0.99, 1 - prob + 0.01),
            "last_price": prob,
            "volume": 50.0,
            "market_id": f"mkt_{int(k)}",
        })
    return {
        "event_id": "test",
        "event_close_time": pd.Timestamp("2024-01-01 17:00:00", tz="UTC"),
        "timestamp_now": None,
        "timestamp_expiry": None,
        "spot_price": spot,
        "time_to_expiry_seconds": tte,
        "strikes": snapshot_strikes,
    }


class TestStrategyC1ModelInterface:
    def setup_method(self):
        self.model = StrategyC1Model(_CONFIG)

    def test_predict_surface_returns_dataframe(self):
        snap = _make_snapshot()
        df = self.model.predict_surface(snap, {}, _CONFIG)
        assert isinstance(df, pd.DataFrame)

    def test_predict_surface_has_required_columns(self):
        snap = _make_snapshot()
        df = self.model.predict_surface(snap, {}, _CONFIG)
        for col in ("strike", "p_model", "p_market", "edge", "moneyness_bucket"):
            assert col in df.columns

    def test_predict_surface_row_count(self):
        strikes = [90.0, 95.0, 100.0, 105.0, 110.0]
        snap = _make_snapshot(strikes=strikes)
        df = self.model.predict_surface(snap, {}, _CONFIG)
        assert len(df) == len(strikes)

    def test_rank_candidates_returns_dataframe(self):
        snap = _make_snapshot()
        surface = self.model.predict_surface(snap, {}, _CONFIG)
        # Inject a large edge to ensure at least one candidate passes
        surface = surface.copy()
        surface["edge"] = 0.15   # well above threshold
        surface["p_model"] = surface["p_market"] + 0.15
        candidates = self.model.rank_candidates(surface, _CONFIG)
        assert isinstance(candidates, pd.DataFrame)

    def test_rank_candidates_sorted_by_abs_edge(self):
        snap = _make_snapshot()
        surface = self.model.predict_surface(snap, {}, _CONFIG)
        surface = surface.copy()
        surface["edge"] = [0.15, -0.20, 0.10, 0.18, -0.05]
        candidates = self.model.rank_candidates(surface, _CONFIG)
        if len(candidates) > 1:
            abs_edges = candidates["edge"].abs().tolist()
            for i in range(len(abs_edges) - 1):
                assert abs_edges[i] >= abs_edges[i + 1]


class TestShouldTradeStrike:
    def setup_method(self):
        self.model = StrategyC1Model(_CONFIG)

    def test_large_edge_clears_threshold(self):
        # |edge| = 0.15 >> taker(0.03) + margin(0.005) + regime(0.02) = 0.055
        assert self.model.should_trade_strike(0.15, "atm", "us_afternoon", _CONFIG) is True

    def test_small_edge_below_threshold(self):
        assert self.model.should_trade_strike(0.01, "atm", "us_afternoon", _CONFIG) is False

    def test_deep_otm_buy_yes_requires_extra_penalty(self):
        # min_edge for deep_otm: (0.03+0.005+0.02)*2.0 + 0.02 = 0.131
        # edge = 0.12 should fail (just below)
        assert self.model.should_trade_strike(0.12, "deep_otm", "us_afternoon", _CONFIG) is False
        # edge = 0.15 should pass
        assert self.model.should_trade_strike(0.15, "deep_otm", "us_afternoon", _CONFIG) is True

    def test_deep_otm_sell_yes_no_penalty(self):
        # edge < 0 (buying NO) -> no longshot penalty
        # min_edge = (0.03+0.005+0.02)*2.0 = 0.11
        assert self.model.should_trade_strike(-0.12, "deep_otm", "us_afternoon", _CONFIG) is True

    def test_null_regime_uses_default(self):
        # Regime not in config -> uses 0.02 default
        assert self.model.should_trade_strike(0.15, "atm", "unknown_regime", _CONFIG) is True

    def test_deep_itm_multiplier_applied(self):
        # min_edge for deep_itm: (0.03+0.005+0.02)*2.0 = 0.11
        assert self.model.should_trade_strike(0.10, "deep_itm", "us_afternoon", _CONFIG) is False
        assert self.model.should_trade_strike(0.12, "deep_itm", "us_afternoon", _CONFIG) is True
