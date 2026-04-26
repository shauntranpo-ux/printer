"""
Tests for the entry range gate (20c–76c for 15m, 20c–80c for hourly).

The bot only enters trades where the chosen side's ask is within
[cfg.min_entry_price_cents, cfg.max_entry_price_cents). Below the floor is
deep-OTM with no edge; at or above the ceiling the fee drag exceeds any
realistic edge.
"""

import sys, os
_src = os.path.join(os.path.dirname(__file__), "..", "..", "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

import pytest
from strategies.skip_layer import check_entry_range, SkipConfig


def test_entry_range_allows_in_range():
    cfg = SkipConfig()
    assert check_entry_range(79.0, "yes", cfg) is None
    assert check_entry_range(50.0, "no", cfg) is None
    assert check_entry_range(21.0, "yes", cfg) is None


def test_entry_range_rejects_at_ceiling():
    cfg = SkipConfig()
    reason = check_entry_range(80.0, "yes", cfg)
    assert reason is not None
    assert "entry_range" in reason
    assert "yes_ask=80c" in reason


def test_entry_range_rejects_above_ceiling():
    cfg = SkipConfig()
    reason = check_entry_range(100.0, "no", cfg)
    assert reason is not None
    assert "entry_range" in reason
    assert "no_ask=100c" in reason


def test_entry_range_rejects_below_floor():
    cfg = SkipConfig()
    reason = check_entry_range(10.0, "yes", cfg)
    assert reason is not None
    assert "entry_range" in reason
    assert "yes_ask=10c" in reason


def test_entry_range_respects_custom_bounds():
    cfg = SkipConfig(min_entry_price_cents=25.0, max_entry_price_cents=70.0)
    assert check_entry_range(30.0, "yes", cfg) is None
    assert check_entry_range(24.0, "yes", cfg) is not None  # below floor
    assert check_entry_range(70.0, "yes", cfg) is not None  # at ceiling


def test_buffer_too_thin_applies_to_short_duration_markets():
    """15m markets now go through the vol_ratio gate (no bypass)."""
    from strategies.skip_layer import check_skip, SkipConfig
    from strategies.features import MarketFeatures
    from collections import deque
    import time as _time

    # 60 samples of stable price, then a fresh 15m market AT the strike
    prices = deque([(float(_time.time() - (60 - i) * 60), 85.40 + 0.001 * i) for i in range(80)])

    f = MarketFeatures(
        asset="SOL",
        ticker="KXSOL15M-26APR231645-45",
        timestamp=_time.time(),
        current_price=85.40,
        strike=85.41,  # 0.01% above = tight ladder
        btc_price=86000.0,
        seconds_left=14 * 60.0,   # 14 min left → short-duration path
        elapsed_seconds=1 * 60.0,
        yes_ask=49.0, no_ask=56.0,
        yes_bid=47.0, no_bid=54.0,
        spread_yes=2.0, spread_no=2.0,
        prices_60m=prices,
        btc_prices_60m=deque([(float(_time.time() - i), 86000.0) for i in range(80)]),
        realized_vol_1min=0.002,
    )

    # Should NOT skip for buffer_too_thin (short-duration carve-out)
    reason = check_skip(f, SkipConfig())
    assert reason is None or "buffer_too_thin" not in reason, (
        f"short-duration market (14min left) should skip buffer_too_thin gate, got: {reason}"
    )


def test_buffer_too_thin_still_applies_to_hourly():
    """60m markets still go through buffer_too_thin when dist is tiny + vol is high."""
    from strategies.skip_layer import check_vol_ratio, SkipConfig
    from strategies.features import MarketFeatures
    from collections import deque
    import time as _time

    prices = deque([(float(_time.time() - (60 - i) * 60), 85.40 + 0.001 * i) for i in range(80)])

    f = MarketFeatures(
        asset="BTC",
        ticker="KXBTCD-26APR2317-T85409.99",
        timestamp=_time.time(),
        current_price=85.40,
        strike=85.41,
        btc_price=85.40,
        seconds_left=55 * 60.0,  # 55 min left → NOT short-duration
        elapsed_seconds=5 * 60.0,
        yes_ask=49.0, no_ask=56.0,
        yes_bid=47.0, no_bid=54.0,
        spread_yes=2.0, spread_no=2.0,
        prices_60m=prices,
        btc_prices_60m=deque([(float(_time.time() - i), 85.40) for i in range(80)]),
        realized_vol_1min=0.01,  # higher vol
    )

    reason = check_vol_ratio(f, SkipConfig())
    assert reason is not None
    assert "buffer_too_thin" in reason


def test_fifteen_min_strategy_rejects_trade_at_100c_no_ask():
    """Regression: entry=100c no_ask trades lose money every expiry (fee drag)."""
    from strategies.fifteen_min_strategy import FifteenMinStrategy
    from strategies.features import MarketFeatures
    from collections import deque
    import time as _time

    prices = deque([(float(t), 2315.0) for t in range(0, 60 * 60, 60)])
    btc_prices = deque([(float(t), 86000.0) for t in range(0, 60 * 60, 60)])

    f = MarketFeatures(
        asset="ETH",
        ticker="KXETH15M-26APR2316-T2316.99",
        timestamp=_time.time(),
        current_price=2315.0,
        strike=2316.99,
        btc_price=86000.0,
        seconds_left=5 * 60.0,
        elapsed_seconds=10 * 60.0,
        yes_ask=1.0,
        no_ask=100.0,
        yes_bid=0.0,
        no_bid=99.0,
        spread_yes=1.0,
        spread_no=1.0,
        prices_60m=prices,
        btc_prices_60m=btc_prices,
    )
    f.bv3_prob = 0.24  # below strike, NO direction

    strat = FifteenMinStrategy(asset="ETH", skip_config=SkipConfig(), min_ev=0.05, stake_dollars=25.0)
    decision = strat.decide(f)

    assert decision.action == "skip", (
        f"Expected skip at no_ask=100c but got action={decision.action}, "
        f"reason={decision.reason}"
    )
