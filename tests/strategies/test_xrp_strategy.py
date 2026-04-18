import time
import json
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
    for i in range(40):
        ts = now - (40 - i) * 10
        f.kalshi_price_history.append((ts, 62.0))
    return f


def _populate_btc_globals():
    import bot
    bot.btc_prices.clear()
    now = time.time()
    for i in range(60):
        ts = now - (60 - i) * 60
        bot.btc_prices.append((ts, 100000.0 + i * 2.0))


def _empty_calendar(tmp_path):
    p = tmp_path / "events.json"
    p.write_text(json.dumps({"events": []}))
    return EventCalendar(path=p)


def test_xrp_decides_trade_or_skip(tmp_path):
    _populate_btc_globals()
    strat = XRPStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.05,
        stake_dollars=5.0,
        event_calendar=_empty_calendar(tmp_path),
    )
    d = strat.decide(_xrp_features(above_strike=True))
    assert d.action in ("trade", "skip")


def test_xrp_event_calendar_hard_skip(tmp_path):
    _populate_btc_globals()
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
    _populate_btc_globals()
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
    import bot
    bot.btc_prices.clear()
    now = time.time()
    for i in range(60):
        ts = now - (60 - i) * 60
        bot.btc_prices.append((ts, 100000.0 + i * 5.0))

    strat = XRPStrategy(
        skip_config=SkipConfig(cold_start_samples=10),
        min_ev=0.01,
        stake_dollars=5.0,
        event_calendar=_empty_calendar(tmp_path),
    )
    f = _xrp_features(above_strike=True)
    import random
    random.seed(42)
    f.prices_60m.clear()
    current = 2.50
    for i in range(60):
        ts = now - (60 - i) * 60
        current += random.gauss(0, 0.001)
        f.prices_60m.append((ts, current))
    f.current_price = current

    d = strat.decide(f)
    if d.action == "trade":
        weight = d.contributing_signals.get("btc_signal_weight", 1.0)
        assert weight < 0.15


def test_xrp_signals_populated(tmp_path):
    _populate_btc_globals()
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
    _populate_btc_globals()
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
