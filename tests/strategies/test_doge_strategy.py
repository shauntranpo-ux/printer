import time

import pytest

from strategies.doge_strategy import DOGEStrategy
from strategies.features import MarketFeatures
from strategies.skip_layer import SkipConfig


def _btc_prices(n=90, base=100000.0, step=2.0):
    now = time.time()
    return [(now - (n - i) * 60, base + i * step) for i in range(n)]


def _doge_features(above_strike: bool = True, btc_flat: bool = False):
    now = time.time()
    current = 0.1050 if above_strike else 0.0950
    strike = 0.1000
    f = MarketFeatures(
        asset="DOGE",
        ticker="KXDOGED-TEST",
        timestamp=now,
        current_price=current,
        strike=strike,
        btc_price=100000.0,
        seconds_left=600.0,
        elapsed_seconds=300.0,
        yes_ask=58.0 if above_strike else 35.0,
        no_ask=44.0 if above_strike else 67.0,
        yes_bid=57.0 if above_strike else 34.0,
        no_bid=43.0 if above_strike else 66.0,
        spread_yes=1.0,
        spread_no=1.0,
        realized_vol_1min=0.004,
    )
    for i in range(60):
        ts = now - (60 - i) * 60
        f.prices_60m.append((ts, current - 0.001 + i * 0.00003))
        f.prices_1m.append((ts, current - 0.001 + i * 0.00003))
    for i in range(40):
        ts = now - (40 - i) * 10
        f.kalshi_price_history.append((ts, 58.0))
    btc_step = 0.0 if btc_flat else 2.0
    for ts, p in _btc_prices(step=btc_step):
        f.btc_prices_60m.append((ts, p))
    return f


def test_doge_decides_trade_or_skip():
    strat = DOGEStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.10,
        stake_dollars=5.0,
    )
    d = strat.decide(_doge_features(above_strike=True))
    assert d.action in ("trade", "skip")


def test_doge_idiosyncratic_mode_forces_skip():
    now = time.time()
    strat = DOGEStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.10,
        stake_dollars=5.0,
    )
    f = _doge_features(above_strike=True, btc_flat=True)
    f.prices_60m.clear()
    doge = 0.10
    for i in range(90):
        ts = now - (90 - i) * 60
        if i < 85:
            # alternating small moves give history a non-zero σ
            doge *= (1.002 if i % 2 == 0 else 0.998)
        else:
            doge *= 1.05            # DOGE spikes hard vs flat-ish history
        f.prices_60m.append((ts, doge))
    f.current_price = doge

    d = strat.decide(f)
    assert d.action == "skip"
    assert "idiosyncratic" in d.reason.lower() or "skip" in d.reason


def test_doge_signals_populated():
    strat = DOGEStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.10,
        stake_dollars=5.0,
    )
    d = strat.decide(_doge_features(above_strike=True))
    if d.action == "trade":
        for key in ("beta", "beta_adj", "momentum_bias", "velocity", "final_p_yes"):
            assert key in d.contributing_signals, f"missing {key}"


def test_doge_clamps_p_yes():
    strat = DOGEStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.01,
        stake_dollars=5.0,
    )
    f = _doge_features(above_strike=True)
    f.current_price = 1.00  # far above strike
    f.realized_vol_1min = 0.0005
    d = strat.decide(f)
    assert 0.05 <= d.p_model <= 0.95


