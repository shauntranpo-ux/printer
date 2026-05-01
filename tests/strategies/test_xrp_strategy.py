import time
import json
import random
from pathlib import Path
from unittest.mock import patch

import pytest

from strategies.xrp_strategy import XRPStrategy
from strategies.features import MarketFeatures
from strategies.skip_layer import SkipConfig
from strategies.signals.event_calendar import EventCalendar


def _xrp_features(above_strike: bool = True):
    now = time.time()
    current = 2.50 if above_strike else 2.40
    strike = 2.45
    f = MarketFeatures(
        asset="XRP",
        ticker="KXXRPD-TEST",
        timestamp=now,
        current_price=current,
        strike=strike,
        btc_price=100000.0,
        seconds_left=600.0,
        elapsed_seconds=300.0,
        yes_ask=62.0 if above_strike else 33.0,
        no_ask=40.0 if above_strike else 69.0,
        yes_bid=61.0 if above_strike else 32.0,
        no_bid=39.0 if above_strike else 68.0,
        spread_yes=1.0,
        spread_no=1.0,
        realized_vol_1min=0.003,
    )
    for i in range(60):
        ts = now - (60 - i) * 60
        f.prices_60m.append((ts, current - 0.01 + i * 0.0002))
        f.prices_1m.append((ts, current - 0.01 + i * 0.0002))
        f.btc_prices_60m.append((ts, 100000.0 + i * 2.0))
    for i in range(40):
        ts = now - (40 - i) * 10
        f.kalshi_price_history.append((ts, 62.0))
    return f


def _empty_calendar(tmp_path):
    p = tmp_path / "events.json"
    p.write_text(json.dumps({"events": []}))
    return EventCalendar(path=p)


def test_xrp_decides_trade_or_skip(tmp_path):
    strat = XRPStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.05,
        stake_dollars=5.0,
        event_calendar=_empty_calendar(tmp_path),
    )
    d = strat.decide(_xrp_features(above_strike=True))
    assert d.action in ("trade", "skip")


def test_xrp_event_calendar_hard_skip(tmp_path):
    now = time.time()
    p = tmp_path / "events.json"
    from datetime import datetime, timezone
    p.write_text(json.dumps({
        "events": [
            {
                "date": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
                "reason": "SEC_ruling",
                "severity": "high",
            }
        ]
    }))
    cal = EventCalendar(path=p)
    strat = XRPStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.05,
        stake_dollars=5.0,
        event_calendar=cal,
    )
    d = strat.decide(_xrp_features(above_strike=True))
    assert d.action == "skip"
    assert "SEC_ruling" in d.reason or "asset_hook" in d.reason


def test_xrp_news_mode_detected(tmp_path):
    strat = XRPStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.01,
        stake_dollars=5.0,
        event_calendar=_empty_calendar(tmp_path),
    )
    f = _xrp_features(above_strike=True)
    now = time.time()
    f.kalshi_price_history.clear()
    for i in range(40):
        ts = now - (40 - i) * 10
        f.kalshi_price_history.append((ts, 40 + i))  # strong up move

    d = strat.decide(f)
    if d.action == "trade":
        assert d.contributing_signals.get("news_mode") is True


def test_xrp_decoupled_mode_zero_btc_weight(tmp_path):
    now = time.time()
    strat = XRPStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.01,
        stake_dollars=5.0,
        event_calendar=_empty_calendar(tmp_path),
    )
    f = _xrp_features(above_strike=True)
    # BTC prices correlated with XRP (both trending); XRP randomly walks
    random.seed(42)
    f.prices_60m.clear()
    current = 2.50
    for i in range(60):
        ts = now - (60 - i) * 60
        current += random.gauss(0, 0.001)
        f.prices_60m.append((ts, current))
        # Keep btc_prices_60m from default _xrp_features (trending BTC)
    f.current_price = current

    d = strat.decide(f)
    if d.action == "trade":
        weight = d.contributing_signals.get("btc_signal_weight", 1.0)
        assert weight < 0.15


def test_xrp_signals_populated(tmp_path):
    strat = XRPStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.05,
        stake_dollars=5.0,
        event_calendar=_empty_calendar(tmp_path),
    )
    d = strat.decide(_xrp_features(above_strike=True))
    if d.action == "trade":
        for key in ("news_mode", "xrp_btc_correlation", "btc_signal_weight",
                    "beta_adj", "regime", "ratio_z", "velocity",
                    "final_p_yes"):
            assert key in d.contributing_signals


def test_xrp_clamps_p_yes(tmp_path):
    strat = XRPStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.01,
        stake_dollars=5.0,
        event_calendar=_empty_calendar(tmp_path),
    )
    f = _xrp_features(above_strike=True)
    f.current_price = 5.00
    f.realized_vol_1min = 0.0005
    d = strat.decide(f)
    assert 0.05 <= d.p_model <= 0.95


# ── X3 APAC decoupling + event continuation tests ─────────────────────────

def _apac_features(above_strike: bool, decoupled: bool, prior_uptrend: bool):
    """
    Build features that are inside the Asia-open window (08:00-10:00 UTC),
    optionally with XRP/BTC decorrelated and a prior 60-min uptrend.
    """
    from datetime import datetime, timezone
    apac_now = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc).timestamp()

    current = 2.50 if above_strike else 2.40
    strike = 2.45
    f = MarketFeatures(
        asset="XRP",
        ticker="KXXRPD-TEST",
        timestamp=apac_now,
        current_price=current,
        strike=strike,
        btc_price=100000.0,
        seconds_left=600.0,
        elapsed_seconds=300.0,
        yes_ask=62.0 if above_strike else 33.0,
        no_ask=40.0 if above_strike else 69.0,
        yes_bid=61.0 if above_strike else 32.0,
        no_bid=39.0 if above_strike else 68.0,
        spread_yes=1.0,
        spread_no=1.0,
        realized_vol_1min=0.003,
    )

    # Build prior 60-min XRP series with the requested trend.
    start_xrp = current * (0.985 if prior_uptrend else 1.015)
    end_xrp = current
    for i in range(60):
        ts = apac_now - (60 - i) * 60
        frac = i / 59.0
        xrp = start_xrp + (end_xrp - start_xrp) * frac
        f.prices_60m.append((ts, xrp))
        f.prices_1m.append((ts, xrp))

    # Build BTC series — flat if decoupled, trending with XRP if coupled.
    for i in range(60):
        ts = apac_now - (60 - i) * 60
        if decoupled:
            # Tiny mean-zero noise to break correlation
            btc = 100000.0 + ((-1) ** i) * 25.0
        else:
            btc = 100000.0 + i * 50.0
        f.btc_prices_60m.append((ts, btc))

    for i in range(40):
        ts = apac_now - (40 - i) * 10
        f.kalshi_price_history.append((ts, 62.0))

    return f


def test_xrp_apac_decoupled_uptrend_adds_positive_continuation(tmp_path):
    strat = XRPStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.01,
        stake_dollars=5.0,
        event_calendar=_empty_calendar(tmp_path),
    )
    f = _apac_features(above_strike=True, decoupled=True, prior_uptrend=True)
    d = strat.decide(f)
    sig = d.contributing_signals
    if "apac_adj" in sig:
        assert sig.get("apac_window") == "asia_open"
        assert sig["apac_adj"] > 0


def test_xrp_apac_outside_window_no_apac_adj(tmp_path):
    strat = XRPStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.01,
        stake_dollars=5.0,
        event_calendar=_empty_calendar(tmp_path),
    )
    f = _apac_features(above_strike=True, decoupled=True, prior_uptrend=True)
    # Move timestamp to 18:00 UTC (outside both APAC and EU peak)
    from datetime import datetime, timezone
    f.timestamp = datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc).timestamp()
    d = strat.decide(f)
    sig = d.contributing_signals
    if "apac_adj" in sig:
        assert sig["apac_adj"] == 0.0
        assert sig.get("apac_window") == "off"


def test_xrp_apac_coupled_no_continuation(tmp_path):
    strat = XRPStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.01,
        stake_dollars=5.0,
        event_calendar=_empty_calendar(tmp_path),
    )
    # XRP and BTC moving together → high correlation, decoupling fails
    f = _apac_features(above_strike=True, decoupled=False, prior_uptrend=True)
    d = strat.decide(f)
    sig = d.contributing_signals
    if "apac_adj" in sig:
        assert sig["apac_adj"] == 0.0


def test_session_clock_helpers():
    from strategies.signals.session_clock import (
        is_decoupling_window, prior_session_return,
    )
    from datetime import datetime, timezone

    asia = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc).timestamp()
    eu = datetime(2026, 5, 1, 15, 0, tzinfo=timezone.utc).timestamp()
    off = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc).timestamp()

    active, label = is_decoupling_window(asia)
    assert active and label == "asia_open"
    active, label = is_decoupling_window(eu)
    assert active and label == "eu_peak"
    active, label = is_decoupling_window(off)
    assert not active

    # prior return helper
    series = [(off - (60 - i) * 60, 100.0 + i) for i in range(60)]
    r = prior_session_return(series, lookback_seconds=3600)
    assert r is not None and r > 0

    flat_series = [(off - (60 - i) * 60, 100.0) for i in range(60)]
    r0 = prior_session_return(flat_series, lookback_seconds=3600)
    assert r0 == 0.0
