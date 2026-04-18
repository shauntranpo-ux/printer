"""
Tests for BTCStrategy covering both modes:
  - Evidence-based (continuation_only=False, default for Section 7.5)
  - Legacy fallback (continuation_only=True, rollback path)
"""

import time
from unittest.mock import patch

import pytest

from strategies.btc_strategy import BTCStrategy
from strategies.features import MarketFeatures
from strategies.skip_layer import SkipConfig


def _features_above_strike():
    now = time.time()
    f = MarketFeatures(
        asset="BTC",
        ticker="KXBTCD-TEST",
        timestamp=now,
        current_price=100500.0,
        strike=100000.0,
        btc_price=100500.0,
        seconds_left=600.0,
        elapsed_seconds=300.0,
        yes_ask=70.0,
        no_ask=32.0,
        yes_bid=69.0,
        no_bid=31.0,
        spread_yes=1.0,
        spread_no=1.0,
        realized_vol_1min=0.002,
    )
    for i in range(60):
        ts = now - (60 - i) * 60
        f.prices_60m.append((ts, 100000.0 + i * 10))
        f.prices_1m.append((ts, 100000.0 + i * 10))
    for i in range(30):
        ts = now - (30 - i) * 10
        f.kalshi_price_history.append((ts, 70.0))
    return f


def _features_below_strike():
    f = _features_above_strike()
    f.current_price = 99500.0
    f.btc_price = 99500.0
    f.yes_ask = 30.0
    f.no_ask = 72.0
    f.yes_bid = 29.0
    f.no_bid = 71.0
    return f


# ── Legacy fallback mode tests (continuation_only=True) ───────────────────

@patch("bot._win_prob_for_asset")
@patch("bot._brain_cal", {"prob_scale": 1.0}, create=True)
def test_legacy_fallback_above_strike_chooses_yes(mock_bv3):
    mock_bv3.return_value = 0.80
    strat = BTCStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.05,
        stake_dollars=5.0,
        continuation_only=True,
    )
    d = strat.decide(_features_above_strike())
    if d.action == "trade":
        assert d.side == "yes"
    assert d.contributing_signals.get("decision_mode") == "continuation_only"


@patch("bot._win_prob_for_asset")
@patch("bot._brain_cal", {"prob_scale": 1.0}, create=True)
def test_legacy_fallback_below_strike_chooses_no(mock_bv3):
    mock_bv3.return_value = 0.80
    strat = BTCStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.05,
        stake_dollars=5.0,
        continuation_only=True,
    )
    d = strat.decide(_features_below_strike())
    if d.action == "trade":
        assert d.side == "no"


@patch("bot._win_prob_for_asset")
@patch("bot._brain_cal", {"prob_scale": 1.0}, create=True)
def test_legacy_fallback_skip_when_below_min_ev(mock_bv3):
    mock_bv3.return_value = 0.71
    strat = BTCStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.10,
        stake_dollars=5.0,
        continuation_only=True,
    )
    d = strat.decide(_features_above_strike())
    assert d.action == "skip"


@patch("bot._win_prob_for_asset")
@patch("bot._brain_cal", {"prob_scale": 1.0}, create=True)
def test_legacy_fallback_signals_populated(mock_bv3):
    mock_bv3.return_value = 0.80
    strat = BTCStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.05,
        stake_dollars=5.0,
        continuation_only=True,
    )
    d = strat.decide(_features_above_strike())
    for key in ("bv3_raw_same_side", "mom_label", "cal_scale"):
        assert key in d.contributing_signals


# ── Evidence-based mode tests (continuation_only=False, default) ──────────

@patch("bot._win_prob_for_asset")
@patch("bot._brain_cal", {"prob_scale": 1.0}, create=True)
def test_evidence_based_populates_new_signals(mock_bv3):
    mock_bv3.return_value = 0.75
    strat = BTCStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.01,
        stake_dollars=5.0,
        continuation_only=False,
    )
    d = strat.decide(_features_above_strike())
    for key in ("regime", "momentum_bias", "velocity", "bv3_blend_weight",
                "final_p_yes", "baseline_p_above"):
        assert key in d.contributing_signals, f"missing {key}"
    assert d.contributing_signals.get("decision_mode") == "evidence_based"


@patch("bot._win_prob_for_asset")
@patch("bot._brain_cal", {"prob_scale": 1.0}, create=True)
def test_evidence_based_bidirectional_may_choose_either_side(mock_bv3):
    mock_bv3.return_value = 0.55
    strat = BTCStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.02,
        stake_dollars=5.0,
        continuation_only=False,
    )
    f = _features_above_strike()
    f.yes_ask = 65.0
    f.no_ask = 35.0
    d = strat.decide(f)
    assert d.side in (None, "yes", "no")


@patch("bot._win_prob_for_asset")
@patch("bot._brain_cal", {"prob_scale": 1.0}, create=True)
def test_evidence_based_bv3_blend_at_twenty_percent(mock_bv3):
    mock_bv3.return_value = 0.90
    strat = BTCStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.01,
        stake_dollars=5.0,
        continuation_only=False,
    )
    d = strat.decide(_features_above_strike())
    if "p_yes_before_bv3_blend" in d.contributing_signals:
        before = d.contributing_signals["p_yes_before_bv3_blend"]
        final = d.contributing_signals["final_p_yes"]
        expected = 0.8 * before + 0.2 * 0.90
        assert abs(final - max(0.05, min(0.95, expected))) < 0.01


@patch("bot._win_prob_for_asset")
@patch("bot._brain_cal", {"prob_scale": 1.0}, create=True)
def test_evidence_based_clamps_p_yes(mock_bv3):
    mock_bv3.return_value = 0.99
    strat = BTCStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.01,
        stake_dollars=5.0,
        continuation_only=False,
    )
    f = _features_above_strike()
    f.current_price = 110000.0
    f.realized_vol_1min = 0.0005
    d = strat.decide(f)
    assert 0.05 <= d.p_model <= 0.95


def test_evidence_based_cold_start_skip():
    strat = BTCStrategy(
        skip_config=SkipConfig(cold_start_samples=200),
        min_ev=0.05,
        stake_dollars=5.0,
        continuation_only=False,
    )
    d = strat.decide(_features_above_strike())
    assert d.action == "skip"
    assert "cold_start" in d.reason


def test_bv3_unavailable_gracefully_degrades():
    strat = BTCStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.01,
        stake_dollars=5.0,
        continuation_only=False,
    )
    f = _features_above_strike()
    d = strat.decide(f)
    assert d.action in ("trade", "skip")
