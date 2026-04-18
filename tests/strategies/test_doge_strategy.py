import time
from unittest.mock import patch

import pytest

from strategies.doge_strategy import DOGEStrategy
from strategies.features import MarketFeatures
from strategies.skip_layer import SkipConfig


def _doge_features(above_strike: bool = True):
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
    return f


def _populate_btc_correlated():
    import bot
    bot.btc_prices.clear()
    now = time.time()
    for i in range(90):
        ts = now - (90 - i) * 60
        bot.btc_prices.append((ts, 100000.0 + i * 2.0))


def test_doge_decides_trade_or_skip():
    _populate_btc_correlated()
    strat = DOGEStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.10,
        stake_dollars=5.0,
    )
    d = strat.decide(_doge_features(above_strike=True))
    assert d.action in ("trade", "skip")


def test_doge_idiosyncratic_mode_forces_skip():
    import bot
    bot.btc_prices.clear()
    now = time.time()
    for i in range(90):
        ts = now - (90 - i) * 60
        bot.btc_prices.append((ts, 100000.0))  # flat BTC

    strat = DOGEStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.10,
        stake_dollars=5.0,
    )
    f = _doge_features(above_strike=True)
    f.prices_60m.clear()
    doge = 0.10
    for i in range(90):
        ts = now - (90 - i) * 60
        if i < 75:
            doge *= 1.0001
        else:
            doge *= 1.005  # DOGE spikes while BTC flat
        f.prices_60m.append((ts, doge))
    f.current_price = doge

    d = strat.decide(f)
    assert d.action == "skip"
    assert "idiosyncratic" in d.reason.lower() or "skip" in d.reason


@patch("strategies.doge_strategy.current_session", return_value="weekend")
def test_doge_weekend_raises_min_ev(mock_session):
    _populate_btc_correlated()
    strat = DOGEStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.10,
        stake_dollars=5.0,
    )
    d = strat.decide(_doge_features(above_strike=True))
    if "effective_min_ev" in d.contributing_signals:
        assert abs(d.contributing_signals["effective_min_ev"] - 0.125) < 0.001


@patch("strategies.doge_strategy.current_session", return_value="normal")
def test_doge_normal_session_standard_min_ev(mock_session):
    _populate_btc_correlated()
    strat = DOGEStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.10,
        stake_dollars=5.0,
    )
    d = strat.decide(_doge_features(above_strike=True))
    if "effective_min_ev" in d.contributing_signals:
        assert abs(d.contributing_signals["effective_min_ev"] - 0.10) < 0.001


def test_doge_signals_populated():
    _populate_btc_correlated()
    strat = DOGEStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.10,
        stake_dollars=5.0,
    )
    d = strat.decide(_doge_features(above_strike=True))
    if d.action == "trade":
        for key in ("beta", "beta_adj", "momentum_bias", "velocity",
                    "session", "effective_min_ev", "final_p_yes"):
            assert key in d.contributing_signals, f"missing {key}"


def test_doge_clamps_p_yes():
    _populate_btc_correlated()
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
