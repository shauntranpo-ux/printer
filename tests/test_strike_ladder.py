"""Tests for strategy_c.features.strike_ladder."""
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "strategies"))

from strategy_c.features.strike_ladder import parse_ladder


def _valid_snapshot(strikes=None):
    if strikes is None:
        strikes = [100.0, 200.0, 300.0, 400.0, 500.0]
    return {
        "event_id": "test_event",
        "event_close_time": None,
        "strikes": [
            {
                "strike": k,
                "yes_bid": 0.40,
                "yes_ask": 0.60,
                "no_bid": 0.40,
                "no_ask": 0.60,
                "last_price": 0.50,
                "volume": 100.0,
                "market_id": f"mkt_{int(k)}",
            }
            for k in strikes
        ],
    }


class TestParseLadderValid:
    def test_returns_dataframe_with_expected_columns(self):
        df = parse_ladder(_valid_snapshot())
        for col in ("strike", "yes_bid", "yes_ask", "no_bid", "no_ask",
                    "mid_price", "implied_probability", "volume", "market_id"):
            assert col in df.columns

    def test_row_count_matches_strikes(self):
        strikes = [100.0, 200.0, 300.0]
        df = parse_ladder(_valid_snapshot(strikes))
        assert len(df) == 3

    def test_strikes_sorted_ascending(self):
        snapshot = _valid_snapshot([500.0, 100.0, 300.0])
        df = parse_ladder(snapshot)
        assert df["strike"].tolist() == sorted(df["strike"].tolist())

    def test_implied_probability_equals_mid_price(self):
        df = parse_ladder(_valid_snapshot())
        assert (df["implied_probability"] == df["mid_price"]).all()

    def test_mid_price_is_average_of_bid_ask(self):
        df = parse_ladder(_valid_snapshot())
        expected = (df["yes_bid"] + df["yes_ask"]) / 2.0
        assert (abs(df["mid_price"] - expected) < 1e-10).all()

    def test_tuple_format_accepted(self):
        snapshot = {
            "event_id": "t",
            "event_close_time": None,
            "strikes": [
                (100.0, 0.4, 0.6, 0.4, 0.6, 0.5, 50.0),
                (200.0, 0.3, 0.5, 0.5, 0.7, 0.4, 30.0),
            ],
        }
        df = parse_ladder(snapshot)
        assert len(df) == 2


class TestParseLadderValidation:
    def test_non_monotone_raises(self):
        # Same strike twice = duplicate → should raise
        snapshot = {
            "event_id": "t",
            "event_close_time": None,
            "strikes": [
                {"strike": 100.0, "yes_bid": 0.4, "yes_ask": 0.6, "no_bid": 0.4, "no_ask": 0.6,
                 "last_price": 0.5, "volume": 10.0, "market_id": "a"},
                {"strike": 100.0, "yes_bid": 0.3, "yes_ask": 0.5, "no_bid": 0.5, "no_ask": 0.7,
                 "last_price": 0.4, "volume": 10.0, "market_id": "b"},
            ],
        }
        with pytest.raises(ValueError, match="Duplicate strikes"):
            parse_ladder(snapshot)

    def test_price_above_one_raises(self):
        snapshot = {
            "event_id": "t",
            "event_close_time": None,
            "strikes": [
                {"strike": 100.0, "yes_bid": 1.2, "yes_ask": 1.5, "no_bid": 0.0, "no_ask": 0.1,
                 "last_price": 1.2, "volume": 10.0, "market_id": "a"},
            ],
        }
        with pytest.raises(ValueError, match=r"\[0, 1\]"):
            parse_ladder(snapshot)

    def test_bid_exceeds_ask_raises(self):
        snapshot = {
            "event_id": "t",
            "event_close_time": None,
            "strikes": [
                {"strike": 100.0, "yes_bid": 0.7, "yes_ask": 0.5, "no_bid": 0.3, "no_ask": 0.5,
                 "last_price": 0.6, "volume": 10.0, "market_id": "a"},
            ],
        }
        with pytest.raises(ValueError, match="yes_bid > yes_ask"):
            parse_ladder(snapshot)

    def test_empty_snapshot_raises(self):
        with pytest.raises(ValueError):
            parse_ladder({"event_id": "t", "event_close_time": None, "strikes": []})

    def test_edge_rows_with_no_quotes_dropped(self):
        # Mix of rows with quotes and a zero-quote row; zero-quote row should be dropped
        snapshot = {
            "event_id": "t",
            "event_close_time": None,
            "strikes": [
                {"strike": 100.0, "yes_bid": 0.0, "yes_ask": 0.0, "no_bid": 0.0, "no_ask": 0.0,
                 "last_price": 0.0, "volume": 0.0, "market_id": ""},
                {"strike": 200.0, "yes_bid": 0.4, "yes_ask": 0.6, "no_bid": 0.4, "no_ask": 0.6,
                 "last_price": 0.5, "volume": 50.0, "market_id": "a"},
                {"strike": 300.0, "yes_bid": 0.2, "yes_ask": 0.4, "no_bid": 0.6, "no_ask": 0.8,
                 "last_price": 0.3, "volume": 30.0, "market_id": "b"},
            ],
        }
        df = parse_ladder(snapshot)
        assert len(df) == 2
        assert 100.0 not in df["strike"].tolist()
