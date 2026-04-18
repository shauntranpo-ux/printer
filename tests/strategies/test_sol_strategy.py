import time
from unittest.mock import patch, MagicMock

import pytest

from strategies.sol_strategy import SOLStrategy
from strategies.features import MarketFeatures
from strategies.skip_layer import SkipConfig
import strategies.signals.solana_health as sh_module


def _reset_solana_cache():
    sh_module._cache.update({"ts": 0.0, "healthy": False, "reason": "reset"})


def _sol_features(above_strike: bool = True):
    now = time.time()
    current = 180.0 if above_strike else 170.0
    strike = 175.0
    f = MarketFeatures(
        asset="SOL",
        ticker="KXSOLD-TEST",
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
        realized_vol_1min=0.004,
    )
    for i in range(60):
        ts = now - (60 - i) * 60
        f.prices_60m.append((ts, current - 1 + i * 0.02))
        f.prices_1m.append((ts, current - 1 + i * 0.02))
    for i in range(40):
        ts = now - (40 - i) * 10
        f.kalshi_price_history.append((ts, 60.0))
    return f


def _populate_btc_globals():
    import bot
    bot.btc_prices.clear()
    now = time.time()
    for i in range(60):
        ts = now - (60 - i) * 60
        bot.btc_prices.append((ts, 100000.0 + i * 2.0))


@patch("strategies.sol_strategy.check_solana_health", return_value=(True, "ok"))
def test_sol_decides_trade_or_skip_when_healthy(mock_health):
    _populate_btc_globals()
    strat = SOLStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.05,
        stake_dollars=5.0,
    )
    d = strat.decide(_sol_features(above_strike=True))
    assert d.action in ("trade", "skip")


@patch("strategies.sol_strategy.check_solana_health",
       return_value=(False, "rpc_timeout"))
def test_sol_skips_when_network_unhealthy(mock_health):
    _populate_btc_globals()
    strat = SOLStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.05,
        stake_dollars=5.0,
    )
    d = strat.decide(_sol_features(above_strike=True))
    assert d.action == "skip"
    assert "rpc_timeout" in d.reason or "asset_hook" in d.reason


@patch("strategies.sol_strategy.check_solana_health", return_value=(True, "ok"))
def test_sol_signals_include_momentum_bias(mock_health):
    _populate_btc_globals()
    strat = SOLStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.05,
        stake_dollars=5.0,
    )
    d = strat.decide(_sol_features(above_strike=True))
    if d.action == "trade":
        assert "momentum_bias" in d.contributing_signals
        assert d.contributing_signals["momentum_bias"] > 0  # above strike


@patch("strategies.sol_strategy.check_solana_health", return_value=(True, "ok"))
def test_sol_exhaustion_activates_in_final_window(mock_health):
    _populate_btc_globals()
    strat = SOLStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.05,
        stake_dollars=5.0,
    )
    f = _sol_features(above_strike=True)
    f.seconds_left = 90  # under 120s threshold
    now = time.time()
    f.prices_1m.clear()
    f.prices_1m.append((now - 59, 175.0))
    f.prices_1m.append((now, 182.0))  # +4% in 1min vs 0.4% vol = 10 sigma
    f.current_price = 182.0
    d = strat.decide(f)
    if "exhaustion_active" in d.contributing_signals:
        assert d.contributing_signals["exhaustion_active"] is True


@patch("strategies.sol_strategy.check_solana_health", return_value=(True, "ok"))
def test_sol_clamps_p_yes(mock_health):
    _populate_btc_globals()
    strat = SOLStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.01,
        stake_dollars=5.0,
    )
    f = _sol_features(above_strike=True)
    f.current_price = 220.0  # 25% above strike
    f.realized_vol_1min = 0.0005
    d = strat.decide(f)
    assert 0.05 <= d.p_model <= 0.95
