import time
from collections import deque

import pytest

from strategies.eth_strategy import ETHStrategy
from strategies.features import MarketFeatures
from strategies.skip_layer import SkipConfig


def _eth_features(above_strike: bool = True):
    now = time.time()
    current = 2100.0 if above_strike else 2050.0
    strike = 2075.0
    f = MarketFeatures(
        asset="ETH",
        ticker="KXETHD-TEST",
        timestamp=now,
        current_price=current,
        strike=strike,
        btc_price=100000.0,
        seconds_left=600.0,
        elapsed_seconds=300.0,
        yes_ask=60.0 if above_strike else 30.0,
        no_ask=42.0 if above_strike else 72.0,
        yes_bid=59.0 if above_strike else 29.0,
        no_bid=41.0 if above_strike else 71.0,
        spread_yes=1.0,
        spread_no=1.0,
        realized_vol_1min=0.002,
    )
    for i in range(60):
        ts = now - (60 - i) * 60
        f.prices_60m.append((ts, current - 5 + i * 0.1))
        f.prices_1m.append((ts, current - 5 + i * 0.1))
    for i in range(40):
        ts = now - (40 - i) * 10
        f.kalshi_price_history.append((ts, 60.0))
    return f


def _populate_bot_btc_globals():
    import bot
    bot.btc_prices.clear()
    now = time.time()
    for i in range(60):
        ts = now - (60 - i) * 60
        bot.btc_prices.append((ts, 100000.0 + i * 2.0))


def test_eth_strategy_decides_trade_or_skip():
    """Smoke test: decide() returns a valid Decision."""
    _populate_bot_btc_globals()
    strat = ETHStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.05,
        stake_dollars=5.0,
    )
    d = strat.decide(_eth_features(above_strike=True))
    assert d.action in ("trade", "skip")
    assert 0.05 <= d.p_model <= 0.95


def test_eth_contributing_signals_populated():
    _populate_bot_btc_globals()
    strat = ETHStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.05,
        stake_dollars=5.0,
    )
    d = strat.decide(_eth_features(above_strike=True))
    if d.action == "trade":
        for key in ("beta", "beta_adj", "variance_ratio", "regime",
                    "regime_adj", "ratio_z", "ratio_adj", "velocity",
                    "velocity_adj", "final_p_yes", "baseline_p_above"):
            assert key in d.contributing_signals, f"missing signal: {key}"


def test_eth_bidirectional_mode_may_choose_either_side():
    """ETH uses BaseStrategy.decide() — bidirectional by default."""
    _populate_bot_btc_globals()
    strat = ETHStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.01,
        stake_dollars=5.0,
    )
    d = strat.decide(_eth_features(above_strike=False))
    assert d.side in (None, "yes", "no")


def test_eth_clamps_final_p_yes_in_range():
    """Even with extreme features, p_yes stays in [0.05, 0.95]."""
    _populate_bot_btc_globals()
    strat = ETHStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.01,
        stake_dollars=5.0,
    )
    f = _eth_features(above_strike=True)
    f.current_price = 2500.0  # 20% above strike
    f.realized_vol_1min = 0.0005
    f.yes_ask = 95.0
    f.no_ask = 6.0
    d = strat.decide(f)
    assert 0.05 <= d.p_model <= 0.95


def test_eth_cold_start_skip():
    _populate_bot_btc_globals()
    strat = ETHStrategy(
        skip_config=SkipConfig(cold_start_samples=200),  # impossibly high
        min_ev=0.05,
        stake_dollars=5.0,
    )
    d = strat.decide(_eth_features(above_strike=True))
    assert d.action == "skip"
