"""Tests for strategy_c.selection.event_selector."""
import sys
import os

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "strategies"))

from strategy_c.selection.event_selector import select_positions


_CONFIG = {
    "selection": {
        "max_positions_per_event": 2,
        "min_strike_spacing_count": 3,
    }
}


def _make_candidates(strikes_and_edges, ladder_ranks=None):
    """Build a candidates DataFrame; ladder_rank defaults to sequential index."""
    rows = []
    for i, (strike, edge) in enumerate(strikes_and_edges):
        rows.append({
            "strike": strike,
            "edge": edge,
            "ladder_rank": ladder_ranks[i] if ladder_ranks else i,
            "moneyness_bucket": "atm",
        })
    return pd.DataFrame(rows)


class TestSelectPositions:
    def test_empty_input_returns_empty(self):
        df = pd.DataFrame(columns=["strike", "edge", "ladder_rank"])
        result = select_positions(df, _CONFIG)
        assert result.empty

    def test_single_candidate_always_selected(self):
        df = _make_candidates([(100.0, 0.08)])
        result = select_positions(df, _CONFIG)
        assert len(result) == 1
        assert result.iloc[0]["strike"] == 100.0

    def test_adjacent_candidates_only_top_selected(self):
        # Three candidates all within spacing=2 of the top pick (ranks 0,1,2)
        # → spacing constraint (min_spacing=3) prevents any 2nd pick
        candidates = [(100.0, 0.10), (105.0, 0.09), (110.0, 0.08)]
        df = _make_candidates(candidates, ladder_ranks=[0, 1, 2])
        result = select_positions(df, _CONFIG)
        assert len(result) == 1
        assert result.iloc[0]["strike"] == pytest.approx(100.0)

    def test_spaced_candidates_two_selected(self):
        # Two candidates separated by 5 positions (> min_strike_spacing_count=3)
        candidates = [(100.0, 0.10), (200.0, 0.08)]
        df = _make_candidates(candidates, ladder_ranks=[0, 5])
        result = select_positions(df, _CONFIG)
        assert len(result) == 2

    def test_result_ranked_by_abs_edge(self):
        candidates = [(100.0, 0.05), (200.0, 0.12), (300.0, 0.08)]
        df = _make_candidates(candidates, ladder_ranks=[0, 5, 10])
        result = select_positions(df, _CONFIG)
        # Best |edge| first
        assert result.iloc[0]["strike"] == pytest.approx(200.0)

    def test_max_positions_cap_enforced(self):
        # Even if all candidates are well-spaced, cap at max_positions_per_event
        candidates = [(100.0 * i, 0.10 - i * 0.01) for i in range(1, 6)]
        df = _make_candidates(candidates, ladder_ranks=[i * 5 for i in range(5)])
        result = select_positions(df, _CONFIG)
        assert len(result) <= 2

    def test_negative_edge_also_valid(self):
        # Edge can be negative (buy NO); |edge| is what matters for ranking
        candidates = [(100.0, -0.12), (200.0, 0.05)]
        df = _make_candidates(candidates, ladder_ranks=[0, 10])
        result = select_positions(df, _CONFIG)
        assert result.iloc[0]["edge"] == pytest.approx(-0.12)

    def test_preserves_extra_columns(self):
        df = _make_candidates([(100.0, 0.09)])
        df["side"] = "yes"
        result = select_positions(df, _CONFIG)
        assert "side" in result.columns

    def test_custom_config_max_one(self):
        config = {"selection": {"max_positions_per_event": 1, "min_strike_spacing_count": 3}}
        candidates = [(100.0, 0.15), (200.0, 0.10)]
        df = _make_candidates(candidates, ladder_ranks=[0, 10])
        result = select_positions(df, config)
        assert len(result) == 1
