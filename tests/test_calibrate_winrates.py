"""Unit tests for calibrate_winrates.py — pure computation only, no HTTP."""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import calibrate_winrates as cal


# ── Task 2: Binance fetch ─────────────────────────────────────────────────────

def _make_kline_batch(start_ms, count, price=50000.0):
    rows = []
    for i in range(count):
        ts = start_ms + i * 60_000
        rows.append([ts, str(price), str(price), str(price), str(price),
                     "1", ts + 59999, "0", 0, "0", "0", "0"])
    return rows


def test_fetch_binance_1m_paginates():
    # Fix: mock time.time() so the function's start_ms aligns with our fake batch timestamps
    end_ms   = 1_700_086_400_000   # start_ms + 1 day
    start_ms = 1_700_000_000_000
    batch1   = _make_kline_batch(start_ms, 1000)
    batch2   = _make_kline_batch(start_ms + 1000 * 60_000, 200)

    call_count = 0
    def fake_binance_get(path, params):
        nonlocal call_count
        call_count += 1
        return batch1 if call_count == 1 else batch2

    with patch.object(cal, "_binance_get", side_effect=fake_binance_get), \
         patch("calibrate_winrates.time.time", return_value=end_ms / 1000):
        result = cal.fetch_binance_1m("BTCUSDT", days=1)

    assert len(result) == 1200
    assert result[0] == (start_ms, 50000.0)
    assert call_count == 2


# ── Task 3: S1 EMA simulation ─────────────────────────────────────────────────

def test_compute_ema_basic():
    prices = [100.0] * 10
    assert abs(cal._compute_ema(prices) - 100.0) < 0.001


def test_simulate_s1_continuation_only():
    start_ms = 1_700_000_000_000
    aligned  = (start_ms // (15 * 60_000)) * (15 * 60_000)
    closes   = [(aligned + i * 60_000, 100.1) for i in range(17)]
    cfg      = dict(min_dist=0.0025, ema_short=3, ema_long=10)
    records  = cal.simulate_s1_window(closes[0][0], 100.0, closes, cfg)
    for abs_pct, mins_left, won in records:
        assert abs_pct >= cfg["min_dist"]


def test_simulate_s1_reversal_filtered():
    start_ms    = 1_700_100_000_000
    aligned     = (start_ms // (15 * 60_000)) * (15 * 60_000)
    prices_up   = [(aligned + i * 60_000, 101.0) for i in range(5)]
    prices_down = [(aligned + (5 + i) * 60_000, 99.0) for i in range(12)]
    closes      = prices_up + prices_down
    strike      = closes[0][1]
    cfg         = dict(min_dist=0.0025, ema_short=3, ema_long=10)
    records     = cal.simulate_s1_window(closes[0][0], strike, closes, cfg)
    for abs_pct, mins_left, won in records:
        assert abs_pct > 0


# ── Task 4: S1 bucketing ──────────────────────────────────────────────────────

def test_bucket_s1_basic():
    records = [(0.003, 4.0, True)] * 70 + [(0.003, 4.0, False)] * 30
    table   = cal.bucket_s1(records, min_dist=0.0025, min_samples=50)
    assert (0, 0) in table
    assert abs(table[(0, 0)] - 0.70) < 0.01


def test_bucket_s1_none_for_sparse():
    records = [(0.003, 4.0, True)] * 10
    table   = cal.bucket_s1(records, min_dist=0.0025, min_samples=50)
    assert table.get((0, 0)) is None


def test_bucket_s1_dist_boundaries():
    cases = [
        (0.003,  0),
        (0.006,  1),
        (0.015,  2),
        (0.030,  3),
    ]
    for abs_pct, expected_idx in cases:
        records = [(abs_pct, 4.0, True)] * 60
        table   = cal.bucket_s1(records, min_dist=0.0025, min_samples=50)
        assert (expected_idx, 0) in table, f"abs_pct={abs_pct} expected bucket {expected_idx}"


# ── Task 5: Kalshi market listing ─────────────────────────────────────────────

def test_fetch_kalshi_markets_paginates():
    # Use a fixed "now" so close_times within 60 days pass the cutoff check
    import time as _time
    fake_now   = 1_700_086_400.0   # fixed epoch
    close1_str = "2023-11-15T07:30:00Z"   # within 60 days of fake_now
    close2_str = "2023-11-15T07:15:00Z"

    page1 = {
        "markets": [
            {
                "ticker":       "KXBTCD-23NOV1530-B99000",
                "floor_strike": 99000.0,
                "close_time":   close1_str,
                "open_time":    "2023-11-15T07:15:00Z",
                "result":       "yes",
                "status":       "finalized",
            }
        ] * 5,
        "cursor": "page2",
    }
    page2 = {
        "markets": [
            {
                "ticker":       "KXBTCD-23NOV1515-B99000",
                "floor_strike": 99000.0,
                "close_time":   close2_str,
                "open_time":    "2023-11-15T07:00:00Z",
                "result":       "no",
                "status":       "finalized",
            }
        ] * 3,
        "cursor": "",
    }
    call_count = 0
    def fake_kalshi_get(path, params, key_id, private_key):
        nonlocal call_count
        call_count += 1
        return page1 if call_count == 1 else page2

    with patch.object(cal, "_kalshi_get", side_effect=fake_kalshi_get), \
         patch("calibrate_winrates.time.time", return_value=fake_now):
        markets = cal.fetch_kalshi_markets("KXBTCD", days=60, key_id="k", private_key=None)

    assert len(markets) == 8
    assert call_count == 2
    assert markets[0]["result"] in ("yes", "no")


# ── Task 6: Kalshi price history ──────────────────────────────────────────────

def test_fetch_market_history_extracts_yes_ask():
    fake_response = {
        "history": [
            {"ts": 1715079000, "yes_bid": 43, "yes_ask": 45, "no_bid": 53, "no_ask": 57, "volume": 10},
            {"ts": 1715079060, "yes_bid": 44, "yes_ask": 46, "no_bid": 52, "no_ask": 56, "volume": 5},
        ]
    }
    with patch.object(cal, "_kalshi_get", return_value=fake_response):
        result = cal.fetch_market_history("KXBTCD-25MAY0715-B99000", key_id="k", private_key=None)

    assert result == [(1715079000, 45), (1715079060, 46)]


# ── Task 7: S2 velocity simulation ───────────────────────────────────────────

def test_simulate_s2_window_bullish():
    close_time_s = 1_715_080_000
    open_time_s  = close_time_s - 900
    strike       = 100.0
    history      = [(open_time_s + i * 60, 40 + i) for i in range(15)]
    cfg          = dict(min_dist=0.0035, min_vel_delta=0.80, vel_lookback=4)
    records      = cal.simulate_s2_window(
        open_time_s, close_time_s, strike, 101.0, history, True, cfg
    )
    assert any(won for _, _, won in records)


def test_simulate_s2_reversal_filtered():
    close_time_s = 1_715_090_000
    open_time_s  = close_time_s - 900
    strike       = 100.0
    # yes_ask goes from 30→43 — stays below 50 at all entry points → implied_above=False
    # velocity is rising (YES side) + implied_above=False → reversal → all filtered
    history      = [(open_time_s + i * 60, 30 + i) for i in range(15)]
    cfg          = dict(min_dist=0.0035, min_vel_delta=0.80, vel_lookback=4)
    records      = cal.simulate_s2_window(
        open_time_s, close_time_s, strike, 99.0, history, True, cfg
    )
    assert len(records) == 0


# ── Task 8: S2 bucketing ──────────────────────────────────────────────────────

def test_bucket_s2_basic():
    records = [(0.9, 3.0, True)] * 60 + [(0.9, 3.0, False)] * 40
    table   = cal.bucket_s2(records, min_vel_delta=0.80, min_samples=50)
    assert (0, 0) in table
    assert abs(table[(0, 0)] - 0.60) < 0.01


def test_bucket_s2_vel_boundaries():
    min_v  = 0.80
    cases  = [
        (0.9,  0),   # 1× to 2× min_vel
        (1.7,  1),   # 2× to 4× min_vel
        (3.5,  2),   # >= 4× min_vel
    ]
    for vel_delta, expected_idx in cases:
        records = [(vel_delta, 3.0, True)] * 60
        table   = cal.bucket_s2(records, min_vel_delta=min_v, min_samples=50)
        assert (expected_idx, 0) in table, f"vel_delta={vel_delta} expected bucket {expected_idx}"


# ── Task 10: bot_strategy lookup functions ────────────────────────────────────

def test_s1_lookup_uses_table():
    import bot_strategy as bs
    assert hasattr(bs, "_s1_lookup_win_rate"), \
        "_s1_lookup_win_rate not found in bot_strategy"


def test_s1_lookup_falls_back_to_tanh():
    import bot_strategy as bs
    # BTC (dist_idx=2, time_idx=1) = None — hits tanh fallback
    # dist_idx=2: abs_pct=0.015 in [0.010, 0.020); time_idx=1: mins_left=7.0 in [6.0, 9.0)
    result = bs._s1_lookup_win_rate("BTC", abs_pct=0.015, mins_left=7.0)
    assert 0.5 < result < 0.85, f"Fallback win_prob {result} out of expected range"
