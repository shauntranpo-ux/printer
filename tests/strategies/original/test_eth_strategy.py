import time
from collections import deque

import pytest

from strategies.original.eth_strategy import ETHStrategy
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
        f.btc_prices_60m.append((ts, 100000.0 + i * 2.0))
    for i in range(40):
        ts = now - (40 - i) * 10
        f.kalshi_price_history.append((ts, 60.0))
    return f


def test_eth_strategy_decides_trade_or_skip():
    """Smoke test: decide() returns a valid Decision."""
    strat = ETHStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.05,
        stake_dollars=5.0,
    )
    d = strat.decide(_eth_features(above_strike=True))
    assert d.action in ("trade", "skip")
    assert 0.05 <= d.p_model <= 0.95


def test_eth_contributing_signals_populated():
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
    strat = ETHStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.01,
        stake_dollars=5.0,
    )
    d = strat.decide(_eth_features(above_strike=False))
    assert d.side in (None, "yes", "no")


def test_eth_clamps_final_p_yes_in_range():
    """Even with extreme features, p_yes stays in [0.05, 0.95]."""
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
    strat = ETHStrategy(
        skip_config=SkipConfig(cold_start_samples=200),  # impossibly high
        min_ev=0.05,
        stake_dollars=5.0,
    )
    d = strat.decide(_eth_features(above_strike=True))
    assert d.action == "skip"


# ── E1 vol-gated ratio mean-revert tests ───────────────────────────────────

def _eth_features_with_ratio_overshoot(z_sign: int, vol: float):
    """
    Build an ETH/BTC ratio overshoot of |z| ~ 2 with controllable
    realized_vol_1min so we can exercise the E1 vol gate.
    """
    f = _eth_features(above_strike=True)
    f.realized_vol_1min = vol
    now = f.timestamp
    f.prices_60m.clear()
    f.btc_prices_60m.clear()
    btc = 100000.0
    base_ratio = 0.021
    eth = btc * base_ratio
    for i in range(60):
        ts = now - (60 - i) * 60
        if i < 50:
            # Build the rolling-mean ratio.
            f.prices_60m.append((ts, eth))
            f.btc_prices_60m.append((ts, btc))
        else:
            # Push ratio up or down sharply over the last 10 buckets.
            shocked = base_ratio * (1.0 + 0.03 * z_sign)
            f.prices_60m.append((ts, btc * shocked))
            f.btc_prices_60m.append((ts, btc))
    f.current_price = f.prices_60m[-1][1]
    return f


def test_e1_band_fires_in_low_vol_overshoot_up():
    strat = ETHStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.001,
        stake_dollars=5.0,
    )
    f = _eth_features_with_ratio_overshoot(z_sign=+1, vol=0.001)
    d = strat.decide(f)
    sig = d.contributing_signals
    if "ratio_z" in sig and sig["ratio_z"] is not None and abs(sig["ratio_z"]) >= 1.2:
        assert sig["e1_vol_regime"] == "ranging"
        assert sig["e1_band_active"] is True
        # Overshoot up → mean-revert nudge points down (negative ratio_adj)
        assert sig["ratio_adj"] < 0


def test_e1_band_suppressed_in_trending_high_vol():
    strat = ETHStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.001,
        stake_dollars=5.0,
    )
    f = _eth_features_with_ratio_overshoot(z_sign=+1, vol=0.005)
    d = strat.decide(f)
    sig = d.contributing_signals
    if "ratio_z" in sig:
        assert sig["e1_vol_regime"] == "trending"
        assert sig["e1_band_active"] is False
        assert sig["ratio_adj"] == 0.0


def test_e1_band_inactive_below_threshold_falls_back_to_linear():
    strat = ETHStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.001,
        stake_dollars=5.0,
    )
    f = _eth_features(above_strike=True)
    f.realized_vol_1min = 0.001  # ranging
    d = strat.decide(f)
    sig = d.contributing_signals
    if "ratio_z" in sig and sig["ratio_z"] is not None and abs(sig["ratio_z"]) < 1.2:
        assert sig["e1_band_active"] is False
        # Linear scaling path is still active inside the band
        assert isinstance(sig["ratio_adj"], float)



