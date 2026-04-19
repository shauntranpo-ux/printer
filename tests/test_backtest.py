"""Smoke tests for the backtest machinery."""

import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_kalshi_amm_smoke():
    from strategies.backtest.kalshi_amm import simulate_orderbook
    ob = simulate_orderbook(
        current_price=100500.0,
        strike=100000.0,
        seconds_left=600.0,
        realized_vol_1min=0.002,
        asset="BTC",
        seed=42,
    )
    assert 1.0 < ob.yes_ask < 100.0
    assert 1.0 < ob.no_ask < 100.0
    assert ob.yes_ask > ob.yes_bid
    assert ob.no_ask > ob.no_bid
    # 0.5% above strike, ~10min left, vol ~0.002/min — p_above ~0.78
    # yes_ask should land in 65-90c range
    assert 50 < ob.yes_ask < 95


def test_kalshi_amm_below_strike():
    from strategies.backtest.kalshi_amm import simulate_orderbook
    ob = simulate_orderbook(
        current_price=99500.0,
        strike=100000.0,
        seconds_left=600.0,
        realized_vol_1min=0.002,
        asset="BTC",
        seed=99,
    )
    # Below strike: YES should be < 50
    assert ob.yes_ask < 55
    assert ob.no_ask > 45


def test_kalshi_amm_reproducible():
    from strategies.backtest.kalshi_amm import simulate_orderbook
    ob1 = simulate_orderbook(100000.0, 100000.0, 300.0, 0.001, "ETH", seed=7)
    ob2 = simulate_orderbook(100000.0, 100000.0, 300.0, 0.001, "ETH", seed=7)
    assert ob1 == ob2


def test_window_generator_produces_valid_events():
    from strategies.backtest.window_generator import generate_events
    now = time.time()
    rows = []
    for i in range(180):
        rows.append({
            "timestamp": now - (180 - i) * 60,
            "close": 100000.0 + i * 2.0,
        })
    df = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)

    events = list(generate_events(df, "BTC", seed=42))
    if len(events) == 0:
        pytest.skip("synthesized data too short for a full window alignment")

    for e in events[:5]:
        assert e.asset == "BTC"
        assert e.window_close_ts > e.window_start_ts
        assert 60.0 <= e.seconds_left <= 14 * 60
        assert e.orderbook.yes_ask > e.orderbook.yes_bid
        assert e.current_price > 0
        assert e.close_price > 0


def test_event_to_features_populates_histories():
    from strategies.backtest.window_generator import BacktestEvent
    from strategies.backtest.kalshi_amm import SimulatedOrderbook
    from strategies.backtest.runner import event_to_features

    now = time.time()
    history = [(now - (60 - i) * 60, 100000.0 + i * 5) for i in range(60)]

    event = BacktestEvent(
        asset="BTC",
        window_start_ts=now - 300,
        window_close_ts=now + 600,
        strike=100000.0,
        eval_ts=now,
        seconds_left=600.0,
        elapsed_seconds=300.0,
        current_price=100300.0,
        close_price=100400.0,
        orderbook=SimulatedOrderbook(
            yes_ask=70.0, yes_bid=68.0, no_ask=33.0, no_bid=31.0
        ),
        price_history=history,
        realized_vol_1min=0.002,
    )

    f = event_to_features(event)
    assert f.asset == "BTC"
    assert f.current_price == 100300.0
    assert len(f.prices_60m) == 60
    assert len(f.prices_1m) >= 1
    assert f.realized_vol_1min == pytest.approx(0.002)


def test_settle_trade_yes_wins_above_strike():
    from strategies.backtest.runner import _settle_trade
    outcome, payout, pnl = _settle_trade(
        side="yes", entry_cents=70.0, contracts=7, stake=4.90,
        fee=0.18, strike=100000.0, close_price=100500.0,
    )
    assert outcome == "win"
    assert payout == 7.0
    assert pnl == pytest.approx(7.0 - 4.90 - 0.18, abs=1e-6)


def test_settle_trade_yes_loses_below_strike():
    from strategies.backtest.runner import _settle_trade
    outcome, payout, pnl = _settle_trade(
        side="yes", entry_cents=70.0, contracts=7, stake=4.90,
        fee=0.18, strike=100000.0, close_price=99800.0,
    )
    assert outcome == "loss"
    assert payout == 0.0
    assert pnl == pytest.approx(-(4.90 + 0.18), abs=1e-6)


def test_settle_trade_yes_loses_at_exact_strike():
    from strategies.backtest.runner import _settle_trade
    # Exactly at strike: conservative, YES loses
    outcome, _, _ = _settle_trade(
        side="yes", entry_cents=50.0, contracts=10, stake=5.0,
        fee=0.18, strike=100000.0, close_price=100000.0,
    )
    assert outcome == "loss"


def test_settle_trade_no_wins_below_strike():
    from strategies.backtest.runner import _settle_trade
    outcome, payout, pnl = _settle_trade(
        side="no", entry_cents=35.0, contracts=14, stake=4.90,
        fee=0.22, strike=100000.0, close_price=99800.0,
    )
    assert outcome == "win"
    assert payout == 14.0


def test_run_backtest_smoke():
    """End-to-end smoke: generate events, run ETH strategy, inspect trades."""
    from strategies.backtest.window_generator import generate_events
    from strategies.backtest.runner import run_backtest
    from scripts.backtest_walk_forward import make_strategy

    now = time.time()
    rows = []
    for i in range(300):
        rows.append({
            "timestamp": now - (300 - i) * 60,
            "close": 2000.0 + (i % 20) * 5,
        })
    df = pd.DataFrame(rows)

    events = list(generate_events(df, "ETH", seed=42))
    if not events:
        pytest.skip("synthesized data too short")

    strat = make_strategy("ETH", calibrator=None)
    trades = run_backtest(strat, events, stake_dollars=5.0)
    assert isinstance(trades, list)
    for t in trades:
        assert t.side in ("yes", "no")
        assert 0 < t.entry_price_cents < 100
        assert t.outcome in ("win", "loss")
        # PnL accounting check
        if t.outcome == "win":
            assert t.payout_dollars > 0
        else:
            assert t.payout_dollars == 0.0


def test_one_trade_per_window_respected():
    """With one_trade_per_window=True, each window appears at most once."""
    from strategies.backtest.window_generator import generate_events
    from strategies.backtest.runner import run_backtest
    from scripts.backtest_walk_forward import make_strategy

    now = time.time()
    rows = [{"timestamp": now - (300 - i) * 60, "close": 2000.0 + i * 1}
            for i in range(300)]
    df = pd.DataFrame(rows)
    events = list(generate_events(df, "ETH", seed=1))
    if not events:
        pytest.skip("too short")

    strat = make_strategy("ETH", calibrator=None)
    trades = run_backtest(strat, events, stake_dollars=5.0, one_trade_per_window=True)

    window_starts = [t.window_start_ts for t in trades]
    assert len(window_starts) == len(set(window_starts)), "duplicate window in trades"


def test_compute_metrics_empty():
    from scripts.backtest_walk_forward import compute_metrics
    m = compute_metrics([], total_windows=100)
    assert m["total_trades"] == 0
    assert m["win_rate"] == 0.0
    assert m["trade_rate"] == 0.0


def test_btc_prices_60m_injected_into_features():
    """btc_prices_history passed to event_to_features populates btc_prices_60m."""
    from collections import deque
    from strategies.backtest.runner import event_to_features
    from strategies.backtest.window_generator import BacktestEvent
    from strategies.backtest.kalshi_amm import SimulatedOrderbook

    now = time.time()
    event = BacktestEvent(
        asset="ETH",
        eval_ts=now,
        window_start_ts=now - 600,
        window_close_ts=now + 300,
        seconds_left=300.0,
        elapsed_seconds=600.0,
        current_price=3000.0,
        strike=3000.0,
        close_price=3010.0,
        price_history=[(now - i * 60, 3000.0) for i in range(60, 0, -1)],
        orderbook=SimulatedOrderbook(yes_ask=52.0, yes_bid=50.0, no_ask=51.0, no_bid=49.0),
        realized_vol_1min=0.001,
    )
    btc_history = [(now - i * 60, 95000.0 + i) for i in range(61, 0, -1)]

    features = event_to_features(event, btc_prices_history=btc_history)
    assert len(features.btc_prices_60m) > 0, "btc_prices_60m should be populated"
    assert all(ts <= now for ts, _ in features.btc_prices_60m)
    assert all(ts >= now - 3600 for ts, _ in features.btc_prices_60m)


def test_btc_prices_60m_empty_when_no_history():
    """event_to_features with no btc_prices_history leaves btc_prices_60m empty."""
    from strategies.backtest.runner import event_to_features
    from strategies.backtest.window_generator import BacktestEvent
    from strategies.backtest.kalshi_amm import SimulatedOrderbook

    now = time.time()
    event = BacktestEvent(
        asset="ETH",
        eval_ts=now,
        window_start_ts=now - 600,
        window_close_ts=now + 300,
        seconds_left=300.0,
        elapsed_seconds=600.0,
        current_price=3000.0,
        strike=3000.0,
        close_price=3010.0,
        price_history=[(now - i * 60, 3000.0) for i in range(60, 0, -1)],
        orderbook=SimulatedOrderbook(yes_ask=52.0, yes_bid=50.0, no_ask=51.0, no_bid=49.0),
        realized_vol_1min=0.001,
    )
    features = event_to_features(event)
    assert len(features.btc_prices_60m) == 0
