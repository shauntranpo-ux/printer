"""
Tests for the 80c entry-price cap.

At 80c+ entries the Kalshi fee drag exceeds any realistic edge. This cap is
enforced post-decision in every strategy — no strategy may emit a trade
Decision where the chosen side's ask is >= cfg.max_entry_price_cents.
"""

import sys, os
_src = os.path.join(os.path.dirname(__file__), "..", "..", "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

import pytest
from strategies.skip_layer import check_entry_price_cap, SkipConfig


def test_cap_helper_allows_below_80c():
    cfg = SkipConfig()
    assert check_entry_price_cap(79.0, "yes", cfg) is None
    assert check_entry_price_cap(50.0, "no", cfg) is None
    assert check_entry_price_cap(1.0, "yes", cfg) is None


def test_cap_helper_rejects_at_80c():
    cfg = SkipConfig()
    reason = check_entry_price_cap(80.0, "yes", cfg)
    assert reason is not None
    assert "price_cap" in reason
    assert "yes_ask=80c" in reason


def test_cap_helper_rejects_above_80c():
    cfg = SkipConfig()
    reason = check_entry_price_cap(100.0, "no", cfg)
    assert reason is not None
    assert "no_ask=100c" in reason
    assert "fee drag" in reason


def test_cap_helper_respects_custom_max():
    cfg = SkipConfig(max_entry_price_cents=70.0)
    assert check_entry_price_cap(69.0, "yes", cfg) is None
    assert check_entry_price_cap(70.0, "yes", cfg) is not None


def test_dwell_window_rejects_trade_at_100c_no_ask():
    """Regression: dashboard showed entry=100c no_ask trades losing money every expiry."""
    from strategies.dwell_window_strategy import DwellWindowStrategy
    from strategies.features import MarketFeatures
    from collections import deque

    # Build features: deeply-OTM 60-min ETH market at t=35min
    prices = deque([(float(t), 2315.0) for t in range(0, 60 * 60, 60)])
    btc_prices = deque([(float(t), 86000.0) for t in range(0, 60 * 60, 60)])

    import time as _time
    f = MarketFeatures(
        asset="ETH",
        ticker="KXETHD-26APR2316-T3089.99",
        timestamp=_time.time(),
        current_price=2315.0,
        strike=3089.99,
        btc_price=86000.0,
        seconds_left=25 * 60.0,
        elapsed_seconds=35 * 60.0,
        yes_ask=1.0,     # deeply OTM: YES essentially worthless
        no_ask=100.0,    # NO guaranteed to win — fee drag if bought
        yes_bid=0.0,
        no_bid=99.0,
        spread_yes=1.0,
        spread_no=1.0,
        prices_60m=prices,
        btc_prices_60m=btc_prices,
    )

    strat = DwellWindowStrategy("ETH", skip_config=SkipConfig(), stake_dollars=25.0, calibrator=None)
    decision = strat.decide(f)

    assert decision.action == "skip", (
        f"Expected skip at no_ask=100c but got action={decision.action}, "
        f"reason={decision.reason}"
    )
    # Reason may be price_cap or an earlier gate (cold_start, dwell, etc.) —
    # what matters is the trade was NOT placed.
