"""Unit tests for bot_hourly.py — pure signal functions only, no HTTP."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot_hourly as bh


# ── dwell_features ────────────────────────────────────────────────────────────

def test_dwell_features_returns_none_if_too_few_bars():
    assert bh.dwell_features([100.0] * 9, 100.0) is None


def test_dwell_features_all_above():
    prices = [101.0] * 20
    feat = bh.dwell_features(prices, 100.0)
    assert feat is not None
    assert feat["dwell_frac"] == 1.0
    assert feat["streak_frac"] == 1.0
    assert feat["is_itm"] is True


def test_dwell_features_all_below():
    prices = [99.0] * 20
    feat = bh.dwell_features(prices, 100.0)
    assert feat is not None
    assert feat["dwell_frac"] == 1.0
    assert feat["streak_frac"] == 1.0
    assert feat["is_itm"] is False


def test_dwell_features_split_50_50():
    prices = [99.0] * 10 + [101.0] * 10
    feat = bh.dwell_features(prices, 100.0)
    assert feat["is_itm"] is True
    assert abs(feat["dwell_frac"] - 0.5) < 0.01
    assert feat["streak_frac"] == 0.5


def test_dwell_features_streak_fraction():
    # 15 below, 5 above → current=above, streak=5/20
    prices = [99.0] * 15 + [101.0] * 5
    feat = bh.dwell_features(prices, 100.0)
    assert feat["is_itm"] is True
    assert abs(feat["streak_frac"] - 5 / 20) < 0.001
    assert abs(feat["dwell_frac"] - 5 / 20) < 0.001


def test_dwell_features_exactly_10_bars():
    feat = bh.dwell_features([101.0] * 10, 100.0)
    assert feat is not None
    assert feat["dwell_frac"] == 1.0


# ── dwell_signal ──────────────────────────────────────────────────────────────

def test_dwell_signal_yes_when_above_and_thresholds_met():
    prices = [101.0] * 20
    assert bh.dwell_signal(prices, 100.0) == "yes"


def test_dwell_signal_no_when_below_and_thresholds_met():
    prices = [99.0] * 20
    assert bh.dwell_signal(prices, 100.0) == "no"


def test_dwell_signal_none_if_dwell_below_threshold():
    # 75% above < 80% → no signal
    prices = [101.0] * 15 + [99.0] * 5
    sig = bh.dwell_signal(prices, 100.0)
    assert sig is None


def test_dwell_signal_none_if_streak_below_threshold():
    # 85% above (17/20) satisfies dwell, but streak only 3/20=15% → no signal
    prices = [101.0] * 14 + [99.0] * 3 + [101.0] * 3
    sig = bh.dwell_signal(prices, 100.0)
    assert sig is None


def test_dwell_signal_none_if_too_few_bars():
    assert bh.dwell_signal([101.0] * 5, 100.0) is None


# ── late_signal ───────────────────────────────────────────────────────────────

def test_late_signal_yes_when_above_and_high_ask():
    result = bh.late_signal(current_price=100_400, strike=100_000, yes_ask_c=90.0, no_ask_c=10.0)
    assert result is not None
    side, entry = result
    assert side == "yes"
    assert entry == 90.0


def test_late_signal_no_when_below_and_high_no_ask():
    result = bh.late_signal(current_price=99_600, strike=100_000, yes_ask_c=10.0, no_ask_c=90.0)
    assert result is not None
    side, entry = result
    assert side == "no"
    assert entry == 90.0


def test_late_signal_none_if_dist_too_small():
    # 0.2% < 0.3% threshold
    result = bh.late_signal(current_price=100_200, strike=100_000, yes_ask_c=90.0, no_ask_c=10.0)
    assert result is None


def test_late_signal_none_if_entry_too_low():
    # dist OK but yes_ask only 80c < 85c threshold
    result = bh.late_signal(current_price=100_400, strike=100_000, yes_ask_c=80.0, no_ask_c=10.0)
    assert result is None


def test_late_signal_none_at_exact_boundary_dist():
    # exactly 0.3% → qualifies
    result = bh.late_signal(current_price=100_300, strike=100_000, yes_ask_c=90.0, no_ask_c=10.0)
    assert result is not None
    assert result[0] == "yes"


def test_late_signal_none_at_exact_boundary_entry():
    # exactly 85c → qualifies
    result = bh.late_signal(current_price=100_400, strike=100_000, yes_ask_c=85.0, no_ask_c=10.0)
    assert result is not None
    assert result[0] == "yes"


def test_late_signal_no_side_none_if_no_ask_low():
    result = bh.late_signal(current_price=99_600, strike=100_000, yes_ask_c=10.0, no_ask_c=80.0)
    assert result is None
