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
        f.btc_prices_60m.append((ts, 100000.0 + i * 2.0))
    for i in range(40):
        ts = now - (40 - i) * 10
        f.kalshi_price_history.append((ts, 60.0))
    return f


@patch("strategies.sol_strategy.check_solana_health", return_value=(True, "ok"))
def test_sol_decides_trade_or_skip_when_healthy(mock_health):
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


# ── S1 cross-venue funding-rate dispersion ───────────────────────────────

from strategies.signals.funding_dispersion import (
    FundingDispersionMonitor,
    funding_dispersion_adjustment,
    S1_DISPERSION_FIRE_ABS,
    S1_FUNDING_ADJ_MAX,
)


def test_funding_dispersion_adjustment_thresholds():
    # Spread above threshold (Binance > Hyperliquid) → expect down nudge
    adj, info = funding_dispersion_adjustment(0.012)
    assert adj < 0
    assert abs(adj) == S1_FUNDING_ADJ_MAX
    assert info["funding_signal"] == "fire"
    # Symmetric on the other side
    adj, info = funding_dispersion_adjustment(-0.012)
    assert adj > 0
    assert abs(adj) == S1_FUNDING_ADJ_MAX
    # Below threshold → zero
    adj, info = funding_dispersion_adjustment(S1_DISPERSION_FIRE_ABS / 2)
    assert adj == 0.0
    assert info["funding_signal"] == "below_threshold"
    # Missing data → zero
    adj, info = funding_dispersion_adjustment(None)
    assert adj == 0.0
    assert info["funding_signal"] == "no_data"


def test_funding_monitor_inject_and_dispersion():
    m = FundingDispersionMonitor("SOL")
    assert m.current_dispersion() is None
    m.inject("binance", 0.0050)
    m.inject("hyperliquid", -0.0020)
    assert m.current_dispersion() == pytest.approx(0.0070)


def test_funding_monitor_stale_entries_dropped():
    m = FundingDispersionMonitor("SOL", max_age_secs=0.1)
    m.inject("binance", 0.001)
    m.inject("hyperliquid", -0.001)
    time.sleep(0.2)
    assert m.current_dispersion() is None


@patch("strategies.sol_strategy.check_solana_health", return_value=(True, "ok"))
def test_sol_funding_signal_drives_p_yes(mock_health):
    monitor = FundingDispersionMonitor("SOL")
    monitor.inject("binance", 0.012)        # crowded longs on Binance
    monitor.inject("hyperliquid", -0.001)
    strat = SOLStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.001,
        stake_dollars=5.0,
        funding_monitor=monitor,
    )
    f = _sol_features(above_strike=True)
    d = strat.decide(f)
    sig = d.contributing_signals
    if "funding_adj" in sig:
        # Crowded Binance longs → expect down nudge → negative funding_adj
        assert sig["funding_adj"] < 0
        assert sig["funding_signal"] == "fire"


@patch("strategies.sol_strategy.check_solana_health", return_value=(True, "ok"))
def test_sol_no_funding_data_falls_back_silently(mock_health):
    monitor = FundingDispersionMonitor("SOL")  # empty cache
    strat = SOLStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.001,
        stake_dollars=5.0,
        funding_monitor=monitor,
    )
    f = _sol_features(above_strike=True)
    d = strat.decide(f)
    sig = d.contributing_signals
    if "funding_adj" in sig:
        assert sig["funding_adj"] == 0.0
        assert sig["funding_signal"] == "no_data"


@patch("strategies.sol_strategy.check_solana_health", return_value=(True, "ok"))
def test_sol_disagreeing_signals_attenuate_beta(mock_health):
    """Funding says down (positive Binance spread), BTC moving up → halve beta."""
    monitor = FundingDispersionMonitor("SOL")
    monitor.inject("binance", 0.012)
    monitor.inject("hyperliquid", -0.001)
    strat = SOLStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.001,
        stake_dollars=5.0,
        funding_monitor=monitor,
    )
    f = _sol_features(above_strike=True)
    # Sharp BTC up move → positive beta_adj before attenuation
    now = f.timestamp
    f.btc_prices_60m.clear()
    for i in range(60):
        f.btc_prices_60m.append((now - (60 - i) * 60, 100000.0 + i * 30.0))
    d = strat.decide(f)
    sig = d.contributing_signals
    if "beta_adj_attenuated" in sig:
        assert sig["beta_adj_attenuated"] is True


# ── Gate A integration test ───────────────────────────────────────────────────

@patch("strategies.sol_strategy.check_solana_health", return_value=(True, "ok"))
def test_gate_a_blocks_yes_when_kalshi_falling(mock_health):
    """Gate A: falling velocity must block a YES trade for SOL."""
    strat = SOLStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.001,
        stake_dollars=5.0,
    )
    f = _sol_features(above_strike=True)
    f.kalshi_price_history.clear()
    for i in range(40):
        f.kalshi_price_history.append((float(i), 60.0 - i * 0.3))
    d = strat.decide(f)
    assert d.action == "skip"
    assert "gate_a" in d.reason
