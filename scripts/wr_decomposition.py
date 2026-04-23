"""
Deep WR decomposition: find what conditions produce the highest win rates.

For each test-set trade, we record ALL signals and the outcome.
Then slice by: BTC 15-min return magnitude, eval time, distance from strike,
vol level, BTC 5-min acceleration, etc.

Goal: empirically identify the conditions under which WR ≥ 90%.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import math
from collections import defaultdict
from datetime import datetime, timezone
from collections import deque

import numpy as np

from strategies.skip_layer import SkipConfig
from strategies.eth_hourly_strategy import ETHHourlyStrategy
from strategies.calibration import IdentityCalibrator
from strategies.backtest.runner import _settle_trade, BacktestTrade
from strategies.features import MarketFeatures
from strategies.fees import taker_fee
from scripts.backtest_hourly import (
    load_prices, load_btc_prices, slice_hourly_events,
)


def ts(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


def run_full_signal_capture(strategy, events, stake, btc_prices_df=None):
    """Run backtest, capture ALL signals for each trade."""
    trades = []
    traded_windows = set()

    btc_history = None
    if btc_prices_df is not None:
        btc_history = list(zip(btc_prices_df["timestamp"].values, btc_prices_df["close"].values))
        btc_arr = np.array([t for t, _ in btc_history])

    for event in events:
        window_key = (event.asset, event.window_start_ts)
        if window_key in traded_windows:
            continue

        prices_60m = deque(maxlen=3600)
        for t, p in event.price_history:
            prices_60m.append((t, p))

        btc_60m = deque(maxlen=3600)
        if btc_history is not None:
            cutoff = event.eval_ts - 3600
            lo = int(np.searchsorted(btc_arr, cutoff))
            hi = int(np.searchsorted(btc_arr, event.eval_ts, side="right"))
            for t, p in btc_history[lo:hi]:
                btc_60m.append((t, p))

        kalshi_hist = deque(maxlen=60)
        for i in range(30):
            kalshi_hist.append((event.eval_ts - (30 - i) * 10, event.orderbook.yes_ask))

        features = MarketFeatures(
            asset=event.asset, ticker="KH-ETH",
            timestamp=event.eval_ts, current_price=event.current_price,
            strike=event.strike, btc_price=0.0,
            seconds_left=event.seconds_left, elapsed_seconds=event.elapsed_seconds,
            yes_ask=event.orderbook.yes_ask, no_ask=event.orderbook.no_ask,
            yes_bid=event.orderbook.yes_bid, no_bid=event.orderbook.no_bid,
            spread_yes=event.orderbook.yes_ask - event.orderbook.yes_bid,
            spread_no=event.orderbook.no_ask - event.orderbook.no_bid,
            prices_1m=deque(maxlen=60), prices_5m=deque(maxlen=300),
            prices_60m=prices_60m, btc_prices_60m=btc_60m,
            kalshi_price_history=kalshi_hist,
            realized_vol_1min=event.realized_vol_1min,
        )

        decision = strategy.decide(features)
        if decision.action == "skip":
            continue

        side = decision.side
        entry_cents = event.orderbook.yes_ask if side == "yes" else event.orderbook.no_ask
        entry_price = entry_cents / 100.0
        if entry_price <= 0 or entry_price >= 1.0:
            continue

        contracts = max(1, int(stake / entry_price))
        actual_stake = contracts * entry_price
        fee = taker_fee(contracts, entry_price)

        outcome, payout, pnl = _settle_trade(
            side=side, entry_cents=entry_cents, contracts=contracts,
            stake=actual_stake, fee=fee,
            strike=event.strike, close_price=event.close_price,
        )

        traded_windows.add(window_key)

        sigs = dict(decision.contributing_signals)
        pct_above = (event.current_price - event.strike) / event.strike * 100.0
        trades.append({
            "outcome": outcome,
            "pnl": pnl,
            "side": side,
            "entry_cents": entry_cents,
            "elapsed_min": event.elapsed_seconds / 60.0,
            "seconds_left": event.seconds_left,
            "pct_above_strike": pct_above,
            "btc_15m": sigs.get("btc_15m"),
            "btc_5m":  sigs.get("btc_5m"),
            "beta_adj": sigs.get("beta_adj", 0.0),
            "accel_adj": sigs.get("accel_adj", 0.0),
            "corr_adj": sigs.get("corr_adj", 0.0),
            "eth_rv": sigs.get("eth_rv", 0.0),
            "vol_ratio": sigs.get("vol_ratio", 1.0),
            "baseline_p": sigs.get("baseline_p_above", 0.5),
            "final_p": sigs.get("final_p_yes", 0.5),
            "ev": float(decision.expected_value or 0.0),
        })

    return trades


def wr_stats(subset):
    if not subset:
        return (0, 0.0, 0.0)
    wins = sum(1 for t in subset if t["outcome"] == "win")
    wr = wins / len(subset) * 100
    pnl = sum(t["pnl"] for t in subset)
    return (len(subset), wr, pnl)


def show_slice(label, subsets: dict):
    print(f"\n{label}")
    print(f"  {'Bucket':<30}  {'N':>6}  {'WR%':>6}  {'PnL($)':>10}")
    print(f"  {'-'*60}")
    for bucket, subset in sorted(subsets.items()):
        n, wr, pnl = wr_stats(subset)
        if n > 0:
            print(f"  {str(bucket):<30}  {n:>6}  {wr:>5.1f}%  {pnl:>+10.2f}")


def main():
    eth_df = load_prices("ETH")
    btc_df = load_btc_prices()

    test_events = slice_hourly_events("ETH", eth_df, ts("2024-01-01"), ts("2026-04-15"), 42)
    print(f"Test events: {len(test_events):,}")

    skip_cfg = SkipConfig(
        max_spread_cents=4.0, min_seconds_left=120.0, min_entry_price_cents=35.0,
        cold_start_samples=60, vol_ratio_threshold=4.0, vol_confirm_mult=1.25,
        vol_oppose_mult=0.70, mom_lock_enabled=True, mom_lock_neutral_tighten=1.0,
        mom_accel_scale=3.0,
    )
    strat = ETHHourlyStrategy(
        skip_config=skip_cfg, min_ev=0.02, stake_dollars=25,
        calibrator=IdentityCalibrator(),
    )

    print("Running signal capture...")
    trades = run_full_signal_capture(strat, test_events, stake=25, btc_prices_df=btc_df)
    print(f"Total trades: {len(trades):,}")
    n, wr, pnl = wr_stats(trades)
    print(f"Overall: N={n}, WR={wr:.1f}%, PnL=${pnl:+.2f}")

    # ---- Slice by BTC 15-min return magnitude ----------------------------
    by_btc15 = defaultdict(list)
    for t in trades:
        b = t["btc_15m"]
        if b is None:
            by_btc15["None"].append(t)
        else:
            mag = abs(b) * 100  # pct
            if mag < 0.1:   by_btc15["<0.1%"].append(t)
            elif mag < 0.2: by_btc15["0.1-0.2%"].append(t)
            elif mag < 0.3: by_btc15["0.2-0.3%"].append(t)
            elif mag < 0.5: by_btc15["0.3-0.5%"].append(t)
            elif mag < 0.75: by_btc15["0.5-0.75%"].append(t)
            elif mag < 1.0: by_btc15["0.75-1.0%"].append(t)
            else:            by_btc15[">1.0%"].append(t)
    show_slice("WR by BTC 15-min return magnitude:", by_btc15)

    # ---- Slice by eval time (minutes into window) -------------------------
    by_time = defaultdict(list)
    for t in trades:
        m = t["elapsed_min"]
        if m <= 10:   by_time["t=5-10min"].append(t)
        elif m <= 20: by_time["t=11-20min"].append(t)
        elif m <= 30: by_time["t=21-30min"].append(t)
        elif m <= 40: by_time["t=31-40min"].append(t)
        elif m <= 50: by_time["t=41-50min"].append(t)
        else:          by_time["t=51-55min"].append(t)
    show_slice("WR by eval time in window:", by_time)

    # ---- Slice by distance from strike (pct) -----------------------------
    by_dist = defaultdict(list)
    for t in trades:
        p = t["pct_above_strike"]
        if p > 2.0:      by_dist[">+2%"].append(t)
        elif p > 1.0:    by_dist["+1-2%"].append(t)
        elif p > 0.5:    by_dist["+0.5-1%"].append(t)
        elif p > 0.0:    by_dist["+0-0.5%"].append(t)
        elif p > -0.5:   by_dist["-0-0.5%"].append(t)
        elif p > -1.0:   by_dist["-0.5-1%"].append(t)
        elif p > -2.0:   by_dist["-1-2%"].append(t)
        else:             by_dist["<-2%"].append(t)
    show_slice("WR by price distance from strike:", by_dist)

    # ---- Slice by side ---------------------------------------------------
    by_side = defaultdict(list)
    for t in trades:
        by_side[t["side"]].append(t)
    show_slice("WR by bet side:", by_side)

    # ---- Slice by BTC 15m magnitude + time (cross-section) ----------------
    print("\nCross: BTC 15m magnitude × eval time (WR% | N trades)")
    mag_bins = ["<0.2%", "0.2-0.5%", ">0.5%"]
    time_bins = ["5-20min", "20-40min", "40-55min"]

    def mag_bin(b):
        if b is None: return None
        m = abs(b) * 100
        if m < 0.2: return "<0.2%"
        elif m < 0.5: return "0.2-0.5%"
        else: return ">0.5%"

    def time_bin(m):
        if m <= 20: return "5-20min"
        elif m <= 40: return "20-40min"
        else: return "40-55min"

    cross = defaultdict(list)
    for t in trades:
        mb = mag_bin(t["btc_15m"])
        tb = time_bin(t["elapsed_min"])
        if mb:
            cross[(mb, tb)].append(t)

    print(f"  {'Time →':<12}", end="")
    for tb in time_bins:
        print(f"  {tb:>16}", end="")
    print()
    for mb in mag_bins:
        print(f"  {mb:<12}", end="")
        for tb in time_bins:
            subset = cross[(mb, tb)]
            if subset:
                n, wr, _ = wr_stats(subset)
                print(f"  {wr:>5.1f}% (n={n:>4})", end="")
            else:
                print(f"  {'---':>14}", end="")
        print()

    # ---- Slice by BTC 15m + distance cross --------------------------------
    print("\nCross: BTC 15m magnitude × price distance from strike (WR% | N trades)")
    dist_bins = [">+1%", "+0-1%", "-1-0%", "<-1%"]

    def dist_bin(p):
        if p > 1.0: return ">+1%"
        elif p > 0: return "+0-1%"
        elif p > -1: return "-1-0%"
        else: return "<-1%"

    cross2 = defaultdict(list)
    for t in trades:
        mb = mag_bin(t["btc_15m"])
        db = dist_bin(t["pct_above_strike"])
        if mb:
            cross2[(mb, db)].append(t)

    print(f"  {'Dist →':<12}", end="")
    for db in dist_bins:
        print(f"  {db:>16}", end="")
    print()
    for mb in mag_bins:
        print(f"  {mb:<12}", end="")
        for db in dist_bins:
            subset = cross2[(mb, db)]
            if subset:
                n, wr, _ = wr_stats(subset)
                print(f"  {wr:>5.1f}% (n={n:>4})", end="")
            else:
                print(f"  {'---':>14}", end="")
        print()

    # ---- High-WR filter: find conditions with WR >= 80% ------------------
    print("\n=== CONDITIONS WITH WR >= 80% ===")
    filters = [
        ("BTC_15m > 0.5% AND same-dir as position",
         lambda t: t["btc_15m"] is not None and abs(t["btc_15m"]) > 0.005
                   and ((t["btc_15m"] > 0 and t["side"] == "yes") or
                        (t["btc_15m"] < 0 and t["side"] == "no"))),
        ("BTC_15m > 0.75%",
         lambda t: t["btc_15m"] is not None and abs(t["btc_15m"]) > 0.0075),
        ("BTC_15m > 1.0%",
         lambda t: t["btc_15m"] is not None and abs(t["btc_15m"]) > 0.01),
        ("Time t>=40min + BTC>0.3%",
         lambda t: t["elapsed_min"] >= 40
                   and t["btc_15m"] is not None and abs(t["btc_15m"]) > 0.003),
        ("Time t>=40min + BTC>0.5%",
         lambda t: t["elapsed_min"] >= 40
                   and t["btc_15m"] is not None and abs(t["btc_15m"]) > 0.005),
        ("Time t>=45min + BTC>0.3%",
         lambda t: t["elapsed_min"] >= 45
                   and t["btc_15m"] is not None and abs(t["btc_15m"]) > 0.003),
        ("|dist|>1% + BTC>0.3%",
         lambda t: abs(t["pct_above_strike"]) > 1.0
                   and t["btc_15m"] is not None and abs(t["btc_15m"]) > 0.003),
        ("|dist|>1% + BTC>0.5%",
         lambda t: abs(t["pct_above_strike"]) > 1.0
                   and t["btc_15m"] is not None and abs(t["btc_15m"]) > 0.005),
        ("|dist|>1.5% + BTC>0.3%",
         lambda t: abs(t["pct_above_strike"]) > 1.5
                   and t["btc_15m"] is not None and abs(t["btc_15m"]) > 0.003),
        ("t>=40 + |dist|>0.5% + BTC>0.3%",
         lambda t: t["elapsed_min"] >= 40
                   and abs(t["pct_above_strike"]) > 0.5
                   and t["btc_15m"] is not None and abs(t["btc_15m"]) > 0.003),
        ("t>=40 + |dist|>1% + BTC>0.3%",
         lambda t: t["elapsed_min"] >= 40
                   and abs(t["pct_above_strike"]) > 1.0
                   and t["btc_15m"] is not None and abs(t["btc_15m"]) > 0.003),
        ("t>=45 + |dist|>0.5% + BTC>0.3%",
         lambda t: t["elapsed_min"] >= 45
                   and abs(t["pct_above_strike"]) > 0.5
                   and t["btc_15m"] is not None and abs(t["btc_15m"]) > 0.003),
        ("t>=45 + |dist|>1% + BTC>0.2%",
         lambda t: t["elapsed_min"] >= 45
                   and abs(t["pct_above_strike"]) > 1.0
                   and t["btc_15m"] is not None and abs(t["btc_15m"]) > 0.002),
        ("t>=45 + |dist|>1% + BTC>0.3%",
         lambda t: t["elapsed_min"] >= 45
                   and abs(t["pct_above_strike"]) > 1.0
                   and t["btc_15m"] is not None and abs(t["btc_15m"]) > 0.003),
        ("t>=45 + |dist|>1.5% + BTC>0.2%",
         lambda t: t["elapsed_min"] >= 45
                   and abs(t["pct_above_strike"]) > 1.5
                   and t["btc_15m"] is not None and abs(t["btc_15m"]) > 0.002),
        ("t>=50 + |dist|>1% + BTC>0.2%",
         lambda t: t["elapsed_min"] >= 50
                   and abs(t["pct_above_strike"]) > 1.0
                   and t["btc_15m"] is not None and abs(t["btc_15m"]) > 0.002),
    ]

    print(f"  {'Filter':<40}  {'N':>5}  {'WR%':>6}  {'PnL':>9}  {'Ann/yr':>10}")
    print(f"  {'-'*80}")
    for name, fn in filters:
        subset = [t for t in trades if fn(t)]
        if not subset:
            continue
        n, wr, pnl = wr_stats(subset)
        ann = pnl / 27 * 12
        print(f"  {name:<40}  {n:>5}  {wr:>5.1f}%  {pnl:>+9.2f}  {ann:>+10.2f}/yr")

    # ---- Ultra-aggressive: find top-percentile WR conditions ------------
    print("\n=== HIGHEST WR COMBINATIONS (min 30 trades) ===")
    # Try all combinations: btc_mag x time x dist
    btc_thresholds = [0.001, 0.002, 0.003, 0.004, 0.005, 0.0075, 0.01]
    time_thresholds = [0, 20, 30, 35, 40, 45, 50]
    dist_thresholds = [0.0, 0.3, 0.5, 0.75, 1.0, 1.5]

    results = []
    for btc_t in btc_thresholds:
        for time_t in time_thresholds:
            for dist_t in dist_thresholds:
                subset = [
                    t for t in trades
                    if t["btc_15m"] is not None
                    and abs(t["btc_15m"]) > btc_t
                    and t["elapsed_min"] >= time_t
                    and abs(t["pct_above_strike"]) > dist_t
                ]
                if len(subset) >= 30:
                    n, wr, pnl = wr_stats(subset)
                    ann = pnl / 27 * 12
                    results.append((wr, n, btc_t, time_t, dist_t, pnl, ann))

    results.sort(reverse=True)
    print(f"  {'WR%':>6}  {'N':>5}  {'BTC>':>7}  {'t>=':>5}  {'|dist|>':>7}  {'Ann/yr':>10}")
    print(f"  {'-'*55}")
    for wr, n, bt, tt, dt, pnl, ann in results[:20]:
        print(f"  {wr:>5.1f}%  {n:>5}  {bt*100:>6.2f}%  {tt:>5}  {dt:>6.1f}%  {ann:>+10.2f}/yr")


if __name__ == "__main__":
    main()
