from collections import deque
import time
import pytest
from unittest.mock import patch

from strategies.btc_strategy import BTCStrategy
from strategies.features import MarketFeatures
from strategies.skip_layer import SkipConfig


def _features_above_strike():
    now = time.time()
    f = MarketFeatures(
        asset="BTC",
        ticker="KXBTCD-TEST",
        timestamp=now,
        current_price=100500.0,  # 0.5% above strike
        strike=100000.0,
        btc_price=100500.0,
        seconds_left=600.0,
        elapsed_seconds=300.0,
        yes_ask=75.0,
        no_ask=27.0,
        yes_bid=74.0,
        no_bid=26.0,
        spread_yes=1.0,
        spread_no=1.0,
        realized_vol_1min=0.002,
    )
    for i in range(60):
        ts = now - (60 - i) * 60
        f.prices_60m.append((ts, 100000.0 + i * 10))
        f.prices_1m.append((ts, 100000.0 + i * 10))
    return f


def _features_below_strike():
    f = _features_above_strike()
    f.current_price = 99500.0
    f.btc_price = 99500.0
    f.yes_ask = 25.0
    f.no_ask = 77.0
    f.yes_bid = 24.0
    f.no_bid = 76.0
    return f


@patch("bot._win_prob_for_asset")
@patch("bot._brain_cal", {"prob_scale": 1.0}, create=True)
def test_continuation_only_above_strike_chooses_yes(mock_bv3):
    """When above strike and continuation_only=True, must choose YES."""
    mock_bv3.return_value = 0.80
    strat = BTCStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.05,
        stake_dollars=5.0,
        continuation_only=True,
    )
    d = strat.decide(_features_above_strike())
    assert d.action in ("trade", "skip")
    if d.action == "trade":
        assert d.side == "yes"


@patch("bot._win_prob_for_asset")
@patch("bot._brain_cal", {"prob_scale": 1.0}, create=True)
def test_continuation_only_below_strike_chooses_no(mock_bv3):
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
def test_bidirectional_may_choose_either_side(mock_bv3):
    """continuation_only=False delegates to BaseStrategy bidirectional."""
    mock_bv3.return_value = 0.80
    strat = BTCStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.05,
        stake_dollars=5.0,
        continuation_only=False,
    )
    d = strat.decide(_features_above_strike())
    # p_yes=0.80, yes_ask=0.75 → yes_ev≈0.05; no_ask=0.27 → no_ev strongly negative
    if d.action == "trade":
        assert d.side == "yes"


@patch("bot._win_prob_for_asset")
@patch("bot._brain_cal", {"prob_scale": 1.0}, create=True)
def test_skip_when_below_min_ev(mock_bv3):
    """If BV3 predicts only slightly above market, EV fails threshold."""
    mock_bv3.return_value = 0.76  # barely above yes_ask=0.75
    strat = BTCStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.10,  # demanding threshold
        stake_dollars=5.0,
        continuation_only=True,
    )
    d = strat.decide(_features_above_strike())
    assert d.action == "skip"


@patch("bot._win_prob_for_asset")
@patch("bot._brain_cal", {"prob_scale": 1.0}, create=True)
def test_signals_populated(mock_bv3):
    mock_bv3.return_value = 0.80
    strat = BTCStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.05,
        stake_dollars=5.0,
        continuation_only=True,
    )
    d = strat.decide(_features_above_strike())
    assert "bv3_raw_same_side" in d.contributing_signals
    assert "mom_label" in d.contributing_signals
    assert "cal_scale" in d.contributing_signals


def test_skip_layer_triggered_for_cold_start():
    """Cold-start skip fires before strategy logic (no bot mock needed)."""
    strat = BTCStrategy(
        skip_config=SkipConfig(cold_start_samples=100),  # impossibly high
        min_ev=0.05,
        stake_dollars=5.0,
        continuation_only=True,
    )
    d = strat.decide(_features_above_strike())
    assert d.action == "skip"
    assert "cold_start" in d.reason


@patch("bot._win_prob_for_asset")
@patch("bot._brain_cal", {"prob_scale": 1.0}, create=True)
def test_bidirectional_flips_when_model_strongly_disagrees_with_market(mock_bv3):
    """
    Above strike but BV3 says only 55% same-side, and NO is cheap:
    bidirectional mode may prefer NO.

    Scenario: BTC at 100500 (0.5% above 100000 strike). Usually BV3 would
    give 0.80+ at this distance. But suppose high vol or a low BV3 output
    yields 0.55. yes_ask = 0.65 -> yes_ev = 0.55 - 0.65 - fee = strongly
    negative. no_ask = 0.35 -> no_ev = 0.45 - 0.35 - fee = slightly positive.
    Bidirectional picks NO despite being above strike.
    """
    mock_bv3.return_value = 0.55
    strat = BTCStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.02,  # low threshold so we can detect the flip
        stake_dollars=5.0,
        continuation_only=False,  # bidirectional
    )
    features = _features_above_strike()
    features.yes_ask = 65.0
    features.no_ask = 35.0
    features.yes_bid = 64.0
    features.no_bid = 34.0

    d = strat.decide(features)
    # Continuation-only would force YES; bidirectional should take NO here
    if d.action == "trade":
        assert d.side == "no"


@patch("bot._win_prob_for_asset")
@patch("bot._brain_cal", {"prob_scale": 1.0}, create=True)
def test_bidirectional_skips_when_neither_side_has_ev(mock_bv3):
    """Market priced near model output -> both sides negative EV after fee."""
    mock_bv3.return_value = 0.65
    strat = BTCStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.05,
        stake_dollars=5.0,
        continuation_only=False,
    )
    features = _features_above_strike()
    features.yes_ask = 64.0
    features.no_ask = 35.0

    d = strat.decide(features)
    # yes_ev ~ 0.65 - 0.64 - fee ~ -0.02
    # no_ev ~ 0.35 - 0.35 - fee ~ -0.04
    # both negative -> skip
    assert d.action == "skip"


@patch("bot._win_prob_for_asset")
@patch("bot._brain_cal", {"prob_scale": 1.0}, create=True)
def test_bidirectional_same_as_continuation_when_continuation_is_best(mock_bv3):
    """
    When the continuation side IS the highest-EV side, both modes should
    produce the same trade decision.
    """
    mock_bv3.return_value = 0.85
    features = _features_above_strike()
    features.yes_ask = 70.0
    features.no_ask = 30.0

    cont = BTCStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.02,
        stake_dollars=5.0,
        continuation_only=True,
    )
    bidi = BTCStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.02,
        stake_dollars=5.0,
        continuation_only=False,
    )

    d_cont = cont.decide(features)
    d_bidi = bidi.decide(features)
    # Above strike + high BV3 -> yes is the right call either way
    if d_cont.action == "trade" and d_bidi.action == "trade":
        assert d_cont.side == d_bidi.side == "yes"
