import time
from unittest.mock import patch

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
    strat = DOGEStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.10,
        stake_dollars=5.0,
    )
    d = strat.decide(_doge_features(above_strike=True))
    if "effective_min_ev" in d.contributing_signals:
        assert abs(d.contributing_signals["effective_min_ev"] - 0.10) < 0.001


def test_doge_signals_populated():
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


# ── D3 retail-FOMO weekend regime tests ────────────────────────────────────

def _force_kalshi_velocity_rising(features):
    """Inject a rising YES-quote series so contract_velocity returns 'rising'."""
    features.kalshi_price_history.clear()
    now = features.timestamp
    for i in range(40):
        ts = now - (40 - i) * 10
        features.kalshi_price_history.append((ts, 50.0 + i * 0.4))


@patch("strategies.doge_strategy.is_weekend_retail_fomo", return_value=True)
@patch("strategies.doge_strategy.current_session", return_value="weekend")
def test_doge_weekend_fomo_lowers_min_ev(mock_session, mock_fomo):
    strat = DOGEStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.10,
        stake_dollars=5.0,
    )
    f = _doge_features(above_strike=True)
    _force_kalshi_velocity_rising(f)
    d = strat.decide(f)
    sig = d.contributing_signals
    if "effective_min_ev" in sig:
        # 0.10 * 0.9 = 0.09 (looser than the 0.125 default weekend threshold)
        assert abs(sig["effective_min_ev"] - 0.09) < 0.001
        assert sig.get("retail_fomo") is True
        assert sig.get("weekend_fomo_size_factor") == 0.5


@patch("strategies.doge_strategy.is_weekend_retail_fomo", return_value=False)
@patch("strategies.doge_strategy.current_session", return_value="weekend")
def test_doge_weekend_without_fomo_keeps_strict_min_ev(mock_session, mock_fomo):
    strat = DOGEStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.10,
        stake_dollars=5.0,
    )
    d = strat.decide(_doge_features(above_strike=True))
    sig = d.contributing_signals
    if "effective_min_ev" in sig:
        assert abs(sig["effective_min_ev"] - 0.125) < 0.001
        assert sig.get("retail_fomo") is False
        assert "weekend_fomo_size_factor" not in sig


def test_is_weekend_retail_window_boundaries():
    from strategies.signals.session_awareness import is_weekend_retail_window
    from datetime import datetime, timezone

    # Friday 15:00 UTC — outside window
    assert not is_weekend_retail_window(
        datetime(2026, 5, 1, 15, 0, tzinfo=timezone.utc).timestamp()
    )
    # Friday 16:00 UTC — opens window
    assert is_weekend_retail_window(
        datetime(2026, 5, 1, 16, 0, tzinfo=timezone.utc).timestamp()
    )
    # Saturday any hour
    assert is_weekend_retail_window(
        datetime(2026, 5, 2, 4, 0, tzinfo=timezone.utc).timestamp()
    )
    # Sunday 21:00 UTC — still in
    assert is_weekend_retail_window(
        datetime(2026, 5, 3, 21, 0, tzinfo=timezone.utc).timestamp()
    )
    # Sunday 22:00 UTC — closes
    assert not is_weekend_retail_window(
        datetime(2026, 5, 3, 22, 0, tzinfo=timezone.utc).timestamp()
    )
    # Monday — outside
    assert not is_weekend_retail_window(
        datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc).timestamp()
    )


def test_is_weekend_retail_fomo_requires_all_gates():
    from strategies.signals.session_awareness import is_weekend_retail_fomo
    from datetime import datetime, timezone

    sat = datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc).timestamp()
    mon = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc).timestamp()

    # All gates pass
    assert is_weekend_retail_fomo(yes_ask_cents=60.0, velocity="rising", now=sat)
    # Wrong day
    assert not is_weekend_retail_fomo(yes_ask_cents=60.0, velocity="rising", now=mon)
    # Wrong velocity
    assert not is_weekend_retail_fomo(yes_ask_cents=60.0, velocity="flat", now=sat)
    assert not is_weekend_retail_fomo(yes_ask_cents=60.0, velocity="falling", now=sat)
    # YES quote too low
    assert not is_weekend_retail_fomo(yes_ask_cents=50.0, velocity="rising", now=sat)
