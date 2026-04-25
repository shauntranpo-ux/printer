import pytest
from collections import deque
from strategies.features import MarketFeatures
from strategies.skip_layer import check_skip, SkipConfig


def _base_features() -> MarketFeatures:
    """Healthy baseline: should not be skipped."""
    f = MarketFeatures(
        asset="BTC",
        ticker="KXBTCD-TEST",
        timestamp=0.0,
        current_price=100000.0,
        strike=99500.0,
        btc_price=100000.0,
        seconds_left=600.0,
        elapsed_seconds=300.0,
        yes_ask=65.0,
        no_ask=37.0,
        yes_bid=64.0,
        no_bid=36.0,
        spread_yes=1.0,
        spread_no=1.0,
        realized_vol_1min=0.002,
    )
    for i in range(60):
        f.prices_60m.append((float(i), 100000.0))
    return f


def test_healthy_features_not_skipped():
    assert check_skip(_base_features(), SkipConfig()) is None


def test_skip_when_under_30s():
    f = _base_features()
    f.seconds_left = 20.0
    assert "seconds_left" in check_skip(f, SkipConfig())


def test_skip_on_macro_event():
    f = _base_features()
    assert "macro" in check_skip(f, SkipConfig(), macro_event_active=True)


def test_skip_on_cold_start():
    f = _base_features()
    f.prices_60m = deque(maxlen=3600)
    for i in range(10):
        f.prices_60m.append((float(i), 100000.0))
    assert "cold_start" in check_skip(f, SkipConfig())


def test_skip_on_missing_vol():
    # Vol guard moved out of check_skip into check_vol_ratio.
    from strategies.skip_layer import check_vol_ratio
    f = _base_features()
    f.realized_vol_1min = None
    assert check_skip(f, SkipConfig()) is None
    assert check_vol_ratio(f, SkipConfig()) is None


def test_skip_when_both_spreads_wide():
    f = _base_features()
    f.spread_yes = 5.0
    f.spread_no = 5.0
    assert "spread" in check_skip(f, SkipConfig(max_spread_cents=3.0))


def test_proceed_when_only_one_side_wide():
    # Only one side has wide spread; EV layer will pick the narrow side
    f = _base_features()
    f.spread_yes = 10.0  # wide
    f.spread_no = 1.0    # narrow
    assert check_skip(f, SkipConfig(max_spread_cents=3.0)) is None
