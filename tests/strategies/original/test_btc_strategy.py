import time
from datetime import datetime, timezone

import pytest

from strategies.original.btc_strategy import BTCStrategy
from strategies.features import MarketFeatures
from strategies.skip_layer import SkipConfig
from strategies.original.signals.btc_diurnal_obi import (
    current_btc_diurnal_band,
    is_funding_reset_window,
    kalshi_book_obi,
    b3_obi_adjustment,
)


def _utc(month, day, hour, minute=0):
    return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc).timestamp()


def _btc_features(now: float, above_strike: bool = True,
                  yes_bid=58.0, no_bid=40.0, yes_ask=60.0, no_ask=42.0):
    current = 100100.0 if above_strike else 99900.0
    strike = 100000.0
    f = MarketFeatures(
        asset="BTC",
        ticker="KXBTCD-TEST",
        timestamp=now,
        current_price=current,
        strike=strike,
        btc_price=current,
        seconds_left=600.0,
        elapsed_seconds=300.0,
        yes_ask=yes_ask,
        no_ask=no_ask,
        yes_bid=yes_bid,
        no_bid=no_bid,
        spread_yes=yes_ask - yes_bid,
        spread_no=no_ask - no_bid,
        realized_vol_1min=0.001,
    )
    for i in range(60):
        ts = now - (60 - i) * 60
        f.prices_60m.append((ts, current - 5 + i * 0.1))
        f.prices_1m.append((ts, current - 5 + i * 0.1))
    for i in range(40):
        ts = now - (40 - i) * 10
        f.kalshi_price_history.append((ts, yes_ask))
    return f


# ── diurnal helpers ─────────────────────────────────────────────────────────

def test_diurnal_bands_classification():
    # Friday 11:00 UTC → peak (10-12 band)
    assert current_btc_diurnal_band(_utc(5, 1, 11)) == "peak"
    # Friday 23:00 UTC → peak (22-24 band)
    assert current_btc_diurnal_band(_utc(5, 1, 23)) == "peak"
    # Friday 19:00 UTC → trough (18-21 band)
    assert current_btc_diurnal_band(_utc(5, 1, 19)) == "trough"
    # Friday 13:00 UTC → neutral
    assert current_btc_diurnal_band(_utc(5, 1, 13)) == "neutral"


def test_funding_reset_guard():
    # Exactly 16:00 UTC → guarded
    assert is_funding_reset_window(_utc(5, 1, 16, 0))
    # 16:04 UTC → still in +/- 5 min guard
    assert is_funding_reset_window(_utc(5, 1, 16, 4))
    # 16:06 UTC → outside guard
    assert not is_funding_reset_window(_utc(5, 1, 16, 6))
    # 23:56 UTC → inside the 00:00 wrap-around guard
    assert is_funding_reset_window(_utc(5, 1, 23, 56))
    # 00:03 UTC → inside the 00:00 wrap-around guard
    assert is_funding_reset_window(_utc(5, 2, 0, 3))


def test_kalshi_book_obi_directionality():
    # YES priced higher than NO → positive OBI
    obi_up = kalshi_book_obi(yes_bid_c=60, no_bid_c=38, yes_ask_c=62, no_ask_c=40)
    assert obi_up is not None and obi_up > 0
    # NO priced higher than YES → negative OBI
    obi_down = kalshi_book_obi(yes_bid_c=38, no_bid_c=60, yes_ask_c=40, no_ask_c=62)
    assert obi_down is not None and obi_down < 0
    # Symmetric book → near zero
    obi_flat = kalshi_book_obi(yes_bid_c=49, no_bid_c=49, yes_ask_c=51, no_ask_c=51)
    assert obi_flat is not None and abs(obi_flat) < 1e-6


def test_b3_obi_adjustment_gating():
    # Trough → zero
    adj, info = b3_obi_adjustment(obi=0.10, band="trough", funding_reset=False)
    assert adj == 0.0 and info["obi_used"] is False
    # Funding reset → zero even at peak
    adj, info = b3_obi_adjustment(obi=0.10, band="peak", funding_reset=True)
    assert adj == 0.0
    # Below threshold → zero
    adj, info = b3_obi_adjustment(obi=0.01, band="peak", funding_reset=False,
                                  obi_threshold=0.04)
    assert adj == 0.0
    # Peak + above threshold → full magnitude
    adj, info = b3_obi_adjustment(obi=+0.10, band="peak", funding_reset=False,
                                  obi_threshold=0.04, adj_magnitude=0.04)
    assert adj == pytest.approx(0.04)
    # Neutral + above threshold → half magnitude
    adj, info = b3_obi_adjustment(obi=+0.10, band="neutral", funding_reset=False,
                                  obi_threshold=0.04, adj_magnitude=0.04)
    assert adj == pytest.approx(0.02)


# ── BTCStrategy integration ─────────────────────────────────────────────────

def test_btc_skip_in_trough():
    strat = BTCStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.05,
        stake_dollars=5.0,
    )
    f = _btc_features(_utc(5, 1, 19))   # 19:00 UTC = trough
    d = strat.decide(f)
    assert d.action == "skip"
    assert "trough" in d.reason


def test_btc_skip_in_funding_reset():
    strat = BTCStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.05,
        stake_dollars=5.0,
    )
    f = _btc_features(_utc(5, 1, 8, 2))   # 08:02 UTC, inside guard
    d = strat.decide(f)
    assert d.action == "skip"
    assert "funding" in d.reason


def test_btc_decides_in_peak():
    strat = BTCStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.001,
        stake_dollars=5.0,
    )
    f = _btc_features(_utc(5, 1, 11))   # 11:00 UTC = peak
    d = strat.decide(f)
    assert d.action in ("trade", "skip")
    if d.action == "trade":
        assert d.contributing_signals.get("diurnal_band") == "peak"


def test_btc_obi_signal_present_in_signals():
    strat = BTCStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.001,
        stake_dollars=5.0,
    )
    f = _btc_features(_utc(5, 1, 11), yes_bid=62, no_bid=37, yes_ask=64, no_ask=39)
    d = strat.decide(f)
    sig = d.contributing_signals
    if d.action == "trade":
        assert "obi" in sig
        assert "obi_adj" in sig
        assert "diurnal_band" in sig



