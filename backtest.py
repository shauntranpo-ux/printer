#!/usr/bin/env python3
"""
backtest.py — Strategy backtest for KXBTC15M printer_brain trading logic.

Loads 7.4M rows of BTC 1-min data, segments into 15-minute windows matching
Kalshi market structure, runs the full printer_brain decision loop (entry,
stop-loss monitoring, expiry), and writes results to stress_test_results.

Usage:
    python backtest.py
    python backtest.py --start-year 2023 --ev 0.20 --amount 5
    python backtest.py --sweep          # runs multiple EV thresholds
"""

import argparse
import itertools
import json
import math
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
CSV_PATH = r'C:\Users\alxnt\Downloads\btcusd_1-min_data.csv'
DB_PATH  = r'C:\Users\alxnt\kalshi-bot\kalshi_bot.db'

# ─────────────────────────────────────────────────────────────────────────────
# Empirical win-probability table — identical to bot.py _BV3_TABLE
# Rows = distance bucket, Cols = minutes remaining (1-min to 13-min)
# ─────────────────────────────────────────────────────────────────────────────
_BV3_TABLE = [
    # 1min   2min   3min   4min   5min   6min   7min   8min   9min  10min  11min  12min  13min
    [0.850, 0.796, 0.758, 0.727, 0.705, 0.686, 0.672, 0.656, 0.639, 0.624, 0.606, 0.595, 0.578],  # 0.0-0.1%
    [0.980, 0.956, 0.931, 0.904, 0.876, 0.856, 0.833, 0.807, 0.783, 0.752, 0.733, 0.706, 0.675],  # 0.1-0.2%
    [0.994, 0.983, 0.967, 0.951, 0.933, 0.909, 0.889, 0.868, 0.835, 0.811, 0.788, 0.756, 0.713],  # 0.2-0.3%
    [0.997, 0.990, 0.981, 0.968, 0.950, 0.935, 0.917, 0.893, 0.874, 0.840, 0.816, 0.778, 0.741],  # 0.3-0.4%
    [0.998, 0.993, 0.987, 0.977, 0.962, 0.948, 0.932, 0.908, 0.883, 0.869, 0.835, 0.809, 0.782],  # 0.4-0.5%
    [0.998, 0.997, 0.988, 0.979, 0.968, 0.960, 0.944, 0.925, 0.913, 0.876, 0.849, 0.824, 0.781],  # 0.5-0.6%
    [0.999, 0.994, 0.994, 0.979, 0.974, 0.963, 0.947, 0.936, 0.914, 0.897, 0.872, 0.839, 0.817],  # 0.6-0.75%
    [0.999, 0.996, 0.995, 0.988, 0.982, 0.968, 0.963, 0.942, 0.917, 0.905, 0.884, 0.845, 0.818],  # 0.75-1.0%
    [1.000, 0.999, 0.994, 0.992, 0.984, 0.980, 0.967, 0.964, 0.935, 0.919, 0.911, 0.862, 0.820],  # 1.0-1.25%
    [1.000, 0.997, 0.995, 0.991, 0.986, 0.972, 0.971, 0.960, 0.942, 0.921, 0.904, 0.874, 0.820],  # 1.25%+
]
_BV3_DIST_BOUNDS = [0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.0075, 0.010, 0.0125]


def _empirical_win_prob(abs_pct: float, mins_left: float) -> float:
    bidx = len(_BV3_DIST_BOUNDS)
    for i, bound in enumerate(_BV3_DIST_BOUNDS):
        if abs_pct < bound:
            bidx = i
            break
    bidx = min(bidx, len(_BV3_TABLE) - 1)
    row  = _BV3_TABLE[bidx]
    if mins_left < 1.0:
        return min(0.997, row[0] + 0.005)
    if mins_left >= 13.0:
        return row[12]
    t_low  = int(mins_left) - 1
    t_high = t_low + 1
    frac   = mins_left - int(mins_left)
    if t_high > 12:
        return row[12]
    return row[t_low] + (row[t_high] - row[t_low]) * frac


# ─────────────────────────────────────────────────────────────────────────────
# AMM price simulator
# ─────────────────────────────────────────────────────────────────────────────

def simulate_amm_prices(btc_price: float, strike: float, rng: random.Random) -> tuple[float, float]:
    """
    Simulate realistic Kalshi AMM yes_ask / no_ask prices based on BTC
    distance from strike.  Matches observed live pricing behaviour:

        < 0.1%  distance → yes_ask 45-55c  (near 50/50)
        0.1-0.3% distance → favoured side 62-75c
        0.3%+   distance → favoured side 77-92c

    Returns (yes_ask_cents, no_ask_cents).  Sum is always > 100 (AMM spread).
    """
    pct    = (btc_price - strike) / strike
    ap     = abs(pct) * 100   # distance as a percentage
    above  = pct > 0
    spread = rng.uniform(3.0, 6.0)

    if ap < 0.10:
        # Coin-flip zone — tiny edge to whichever side BTC is on
        base = rng.uniform(49.0, 54.0) if above else rng.uniform(46.0, 51.0)
        yes_ask = base
    elif ap < 0.30:
        if above:
            yes_ask = rng.uniform(62.0, 75.0)
        else:
            yes_ask = 100 - rng.uniform(62.0, 75.0)  # NO favoured → YES is cheap
    else:
        if above:
            yes_ask = rng.uniform(77.0, 92.0)
        else:
            yes_ask = 100 - rng.uniform(77.0, 92.0)

    yes_ask = max(3.0, min(97.0, yes_ask))
    no_ask  = max(3.0, min(97.0, 100.0 + spread - yes_ask))
    return yes_ask, no_ask


def simulate_bid(side: str, yes_ask: float, no_ask: float, rng: random.Random) -> float:
    """Bid price for an existing position = ask minus a small spread."""
    spread = rng.uniform(2.0, 4.0)
    if side == "yes":
        return max(1.0, yes_ask - spread)
    else:
        return max(1.0, no_ask - spread)


# ─────────────────────────────────────────────────────────────────────────────
# Brain decision (stateless replica of printer_brain from bot.py)
# ─────────────────────────────────────────────────────────────────────────────

def brain_decide(
    btc_price: float,
    strike: float,
    yes_ask: float,
    no_ask: float,
    mins_left: float,
    mom_label: str,
    prob_scale: float = 1.0,
    min_ev: float = 0.15,
    bullish_wr: float = 0.5,
    bearish_wr: float = 0.5,
) -> dict:
    pct_above = (btc_price - strike) / strike
    abs_pct   = abs(pct_above)
    above     = pct_above > 0

    win_prob_raw = _empirical_win_prob(abs_pct, mins_left)

    if mom_label == "bullish":
        mom_adj = +0.05 if above else -0.05
    elif mom_label == "bearish":
        mom_adj = +0.05 if not above else -0.05
    else:
        mom_adj = 0.0

    win_prob = win_prob_raw + mom_adj
    win_prob = 0.50 + (win_prob - 0.50) * prob_scale
    win_prob = max(0.10, min(0.997, win_prob))

    prob_yes = win_prob if above else (1.0 - win_prob)
    prob_no  = 1.0 - prob_yes

    yes_ev = prob_yes - (yes_ask / 100)
    no_ev  = prob_no  - (no_ask  / 100)

    if bullish_wr < 0.35: yes_ev -= 0.04
    if bearish_wr < 0.35: no_ev  -= 0.04

    if yes_ev >= no_ev:
        side, best_ev, entry_c, true_p = "yes", yes_ev, yes_ask, prob_yes
    else:
        side, best_ev, entry_c, true_p = "no",  no_ev,  no_ask,  prob_no

    action     = "trade" if best_ev >= min_ev else "skip"
    confidence = min(99, max(0, int(true_p * 100)))

    return {
        "action":     action,
        "side":       side,
        "confidence": confidence,
        "win_prob":   float(true_p),
        "ev":         float(best_ev),
        "entry_c":    float(entry_c),
    }


def compute_momentum(recent_closes: list) -> str:
    """3-min momentum label from a list of recent close prices."""
    if len(recent_closes) < 2:
        return "neutral"
    pct = (recent_closes[-1] - recent_closes[0]) / recent_closes[0]
    if pct > 0.005:  return "bullish"
    if pct < -0.005: return "bearish"
    return "neutral"


# ─────────────────────────────────────────────────────────────────────────────
# Main backtest
# ─────────────────────────────────────────────────────────────────────────────

def run_backtest(
    start_year: int   = 2020,
    min_ev: float     = 0.15,
    trade_amount: float = 5.0,
    stop_loss_pct: float = 35.0,
    watch_minutes: int  = 1,
    seed: int = 42,
    verbose: bool = True,
    # Monte Carlo extra filters (defaults = no filtering)
    entry_lo_sec: int   = 0,
    entry_hi_sec: int   = 900,
    min_confidence: int = 0,
    ob_imbalance_thresh: float = 0.0,
) -> dict:
    rng = random.Random(seed)

    # ── Load data ─────────────────────────────────────────────────────────────
    if verbose:
        print(f"\nLoading {CSV_PATH}...")
    t0 = time.time()
    df = pd.read_csv(
        CSV_PATH,
        dtype={"Timestamp": "float64", "Open": "float64", "High": "float64",
               "Low": "float64", "Close": "float64", "Volume": "float64"},
        engine="c",
    )
    df["ts"] = df["Timestamp"].astype("int64")
    df = df.dropna(subset=["Open", "Close"])
    df = df[df["Close"] > 0]
    if verbose:
        print(f"  Loaded {len(df):,} rows in {time.time()-t0:.1f}s")

    # Filter by year
    df["year"] = pd.to_datetime(df["ts"], unit="s").dt.year
    df = df[df["year"] >= start_year].copy()
    if verbose:
        print(f"  Filtered to {start_year}+: {len(df):,} rows")

    # ── 15-minute window assignment ───────────────────────────────────────────
    df["window_start"]     = (df["ts"] // 900) * 900
    df["minute_in_window"] = (df["ts"] - df["window_start"]) // 60
    df = df.sort_values(["window_start", "ts"])

    # Strike = open of minute 0
    strikes = (
        df[df["minute_in_window"] == 0]
        .groupby("window_start")["Open"]
        .first()
        .rename("strike")
    )

    # Final close = last candle in window
    finals = (
        df.groupby("window_start")
        .apply(lambda g: g.loc[g["minute_in_window"].idxmax(), "Close"],
               include_groups=False)
        .rename("final_close")
    )

    windows = strikes.to_frame().join(finals).dropna()
    windows = windows[(windows["strike"] > 0) & (windows["final_close"] > 0)]

    if verbose:
        print(f"  Windows: {len(windows):,}")

    # Build price lookup: window_start → {minute_in_window → close}
    price_lookup = (
        df.groupby("window_start")
        .apply(lambda g: dict(zip(g["minute_in_window"].tolist(),
                                   g["Close"].tolist())),
               include_groups=False)
    )

    # ── Simulation loop ───────────────────────────────────────────────────────
    trades         = []
    skipped        = 0
    total_windows  = len(windows)

    if verbose:
        print(f"\nSimulating {total_windows:,} windows "
              f"(min_ev={min_ev:.0%}, stop_loss={stop_loss_pct:.0f}%, "
              f"trade=${trade_amount:.0f})...")

    last_print = time.time()

    for i, (window_start, row) in enumerate(windows.iterrows()):
        if verbose and time.time() - last_print > 5:
            pct_done = i / total_windows * 100
            print(f"  {pct_done:.0f}%  ({i:,}/{total_windows:,} windows, "
                  f"{len(trades):,} trades so far)...")
            last_print = time.time()

        strike      = row["strike"]
        final_close = row["final_close"]
        prices      = price_lookup.get(window_start, {})

        if not prices or len(prices) < 3:
            skipped += 1
            continue

        # Try each minute after WATCH phase
        trade_placed = False
        for minute in range(watch_minutes, 14):
            btc = prices.get(minute)
            if btc is None:
                continue

            mins_left = float(15 - minute)

            # 3-minute momentum
            recent = [prices[m] for m in range(max(0, minute - 3), minute + 1)
                      if m in prices]
            mom = compute_momentum(recent)

            # Simulate AMM prices
            yes_ask, no_ask = simulate_amm_prices(btc, strike, rng)

            # Brain decision
            brain = brain_decide(btc, strike, yes_ask, no_ask, mins_left,
                                  mom, min_ev=min_ev)

            if brain["action"] != "trade":
                continue

            # ── Monte Carlo extra filters ──────────────────────────────────────
            elapsed_sec = minute * 60
            if not (entry_lo_sec <= elapsed_sec <= entry_hi_sec):
                continue
            if brain["confidence"] < min_confidence:
                continue
            if ob_imbalance_thresh > 0.0:
                # Simulate order-book imbalance: momentum direction + distance
                # + noise. Range [0, 1]; higher = more one-sided book.
                abs_pct_cur = abs((btc - strike) / strike)
                base_imbal  = min(0.95, abs_pct_cur * 20.0)   # 0→0, 1%→0.2
                if mom == "neutral":
                    base_imbal *= 0.5
                noise = rng.gauss(0, 0.05)
                ob_imbalance = max(0.0, min(1.0, base_imbal + abs(noise)))
                if ob_imbalance < ob_imbalance_thresh:
                    continue

            # ── Entry ─────────────────────────────────────────────────────────
            side       = brain["side"]
            entry_c    = brain["entry_c"]
            confidence = brain["confidence"]
            win_prob   = brain["win_prob"]
            ev         = brain["ev"]
            contracts  = max(1, int(trade_amount * 100 / entry_c))
            sl_price   = entry_c * (1.0 - stop_loss_pct / 100.0)

            # ── Stop-loss monitoring for remaining minutes ─────────────────────
            exit_reason = "expiry"
            exit_price  = None

            for chk_min in range(minute + 1, 15):
                chk_btc = prices.get(chk_min)
                if chk_btc is None:
                    continue
                chk_mins_left = float(15 - chk_min)
                chk_yes_ask, chk_no_ask = simulate_amm_prices(chk_btc, strike, rng)
                chk_bid = simulate_bid(side, chk_yes_ask, chk_no_ask, rng)

                if chk_bid <= sl_price:
                    exit_reason = "stop_loss"
                    exit_price  = chk_bid
                    break
                if chk_mins_left <= 2.0 and chk_bid < entry_c * 0.6:
                    exit_reason = "late_bail"
                    exit_price  = chk_bid
                    break

            # ── Expiry outcome ────────────────────────────────────────────────
            if exit_reason == "expiry":
                above_at_close = final_close > strike
                won = (side == "yes" and above_at_close) or \
                      (side == "no"  and not above_at_close)
                exit_price = 100.0 if won else 0.0
            else:
                won = (exit_price is not None) and (exit_price > entry_c)

            pnl        = (exit_price - entry_c) * contracts / 100.0
            profit_pct = (exit_price - entry_c) / entry_c * 100.0 if entry_c else 0.0

            trades.append({
                "window_start": int(window_start),
                "minute_in":    minute,
                "side":         side,
                "entry_c":      round(entry_c,    1),
                "sl_price":     round(sl_price,   1),
                "exit_price":   round(exit_price, 1),
                "exit_reason":  exit_reason,
                "outcome":      "win" if won else "loss",
                "pnl":          round(pnl,        4),
                "profit_pct":   round(profit_pct, 2),
                "confidence":   confidence,
                "win_prob":     round(win_prob,   4),
                "ev":           round(ev,         4),
                "mom":          mom,
            })

            trade_placed = True
            break  # one trade per window

        if not trade_placed:
            skipped += 1

    # ── Statistics ────────────────────────────────────────────────────────────
    if not trades:
        print("No trades placed. Try reducing --ev threshold.")
        return {}

    total  = len(trades)
    wins   = sum(1 for t in trades if t["outcome"] == "win")
    losses = total - wins
    wr     = wins / total

    pnls       = [t["pnl"] for t in trades]
    total_pnl  = sum(pnls)

    # Max drawdown (on cumulative PnL curve)
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        cum += p
        if cum > peak:
            peak = cum
        if peak > 0:
            dd = (peak - cum) / peak * 100
            if dd > max_dd:
                max_dd = dd

    # Annualised Sharpe (each trade is ~15 min; ~35,040 slots/year)
    if len(pnls) > 1:
        mu  = sum(pnls) / len(pnls)
        var = sum((p - mu) ** 2 for p in pnls) / (len(pnls) - 1)
        sd  = math.sqrt(var)
        sharpe = (mu / sd * math.sqrt(35040)) if sd > 0 else 0.0
    else:
        sharpe = 0.0

    # Max consecutive losses
    max_cl, cur_cl = 0, 0
    for t in trades:
        if t["outcome"] == "loss":
            cur_cl += 1
            max_cl = max(max_cl, cur_cl)
        else:
            cur_cl = 0

    avg_conf       = sum(t["confidence"]  for t in trades) / total
    avg_profit_pct = sum(t["profit_pct"]  for t in trades) / total

    # Entry minute distribution
    minute_dist = {}
    for t in trades:
        m = t["minute_in"]
        minute_dist[m] = minute_dist.get(m, 0) + 1

    # Side distribution
    yes_trades = sum(1 for t in trades if t["side"] == "yes")
    no_trades  = total - yes_trades

    # Exit reason breakdown
    exit_dist = {}
    for t in trades:
        r = t["exit_reason"]
        exit_dist[r] = exit_dist.get(r, 0) + 1

    # Stop-loss win rate vs expiry win rate
    sl_exits = [t for t in trades if t["exit_reason"] in ("stop_loss", "late_bail")]
    exp_exits = [t for t in trades if t["exit_reason"] == "expiry"]
    sl_wr  = sum(1 for t in sl_exits  if t["outcome"] == "win") / len(sl_exits)  if sl_exits  else 0.0
    exp_wr = sum(1 for t in exp_exits if t["outcome"] == "win") / len(exp_exits) if exp_exits else 0.0

    result = {
        "start_year":             start_year,
        "min_ev":                 min_ev,
        "trade_amount_dollars":   trade_amount,
        "stop_loss_pct":          stop_loss_pct,
        "total_windows":          total_windows,
        "windows_skipped":        skipped,
        "total_trades":           total,
        "wins":                   wins,
        "losses":                 losses,
        "win_rate":               round(wr,         4),
        "total_pnl_dollars":      round(total_pnl,  2),
        "max_drawdown_percent":   round(max_dd,     2),
        "sharpe_ratio":           round(sharpe,     3),
        "max_consecutive_losses": max_cl,
        "avg_confidence":         round(avg_conf,       1),
        "avg_profit_percent":     round(avg_profit_pct, 2),
        "yes_trades":             yes_trades,
        "no_trades":              no_trades,
        "exit_dist":              exit_dist,
        "sl_win_rate":            round(sl_wr,  3),
        "expiry_win_rate":        round(exp_wr, 3),
        "minute_dist":            dict(sorted(minute_dist.items())),
    }
    return result


# ─────────────────────────────────────────────────────────────────────────────
# DB writer
# ─────────────────────────────────────────────────────────────────────────────

def write_to_db(r: dict, start_year: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        INSERT INTO stress_test_results (
            run_ts, start_date, end_date,
            total_markets, total_trades, win_rate,
            total_pnl_dollars, max_drawdown_percent,
            avg_confidence, avg_profit_percent,
            sharpe_ratio, max_consecutive_losses
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        datetime.now(timezone.utc).isoformat(),
        f"{start_year}-01-01",
        "2026-12-31",
        r["total_windows"],
        r["total_trades"],
        r["win_rate"],
        r["total_pnl_dollars"],
        r["max_drawdown_percent"],
        r["avg_confidence"],
        r["avg_profit_percent"],
        r["sharpe_ratio"],
        r["max_consecutive_losses"],
    ))
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Report printer
# ─────────────────────────────────────────────────────────────────────────────

def print_report(r: dict) -> None:
    if not r:
        return
    W = 60
    print("\n" + "=" * W)
    print(f"  BACKTEST RESULTS  —  {r['start_year']}+  |  min_ev={r['min_ev']:.0%}")
    print("=" * W)
    print(f"  Windows simulated : {r['total_windows']:>10,}")
    print(f"  Trades placed     : {r['total_trades']:>10,}  "
          f"({r['total_trades']/r['total_windows']*100:.1f}% of windows)")
    print(f"  YES / NO split    : {r['yes_trades']:,} / {r['no_trades']:,}")
    print("-" * W)
    print(f"  Win rate          : {r['win_rate']*100:>10.1f}%")
    print(f"  Total P&L         : ${r['total_pnl_dollars']:>9.2f}")
    print(f"  Avg profit/trade  : {r['avg_profit_percent']:>9.1f}%")
    print(f"  Avg confidence    : {r['avg_confidence']:>10.1f}")
    print("-" * W)
    print(f"  Max drawdown      : {r['max_drawdown_percent']:>9.1f}%")
    print(f"  Sharpe ratio      : {r['sharpe_ratio']:>10.3f}")
    print(f"  Max consec losses : {r['max_consecutive_losses']:>10}")
    print("-" * W)
    print(f"  Exit at expiry    : {r['exit_dist'].get('expiry',0):>6,}  WR={r['expiry_win_rate']*100:.1f}%")
    print(f"  Stop-loss exits   : {r['exit_dist'].get('stop_loss',0):>6,}  WR={r['sl_win_rate']*100:.1f}%")
    print(f"  Late bail exits   : {r['exit_dist'].get('late_bail',0):>6,}")
    print("-" * W)
    print("  Entry minute distribution:")
    for m, n in sorted(r["minute_dist"].items()):
        bar = "█" * (n * 30 // max(r["minute_dist"].values()))
        print(f"    min {m:2d}: {n:6,}  {bar}")
    print("=" * W)


# ─────────────────────────────────────────────────────────────────────────────
# EV sweep — test multiple thresholds and rank them
# ─────────────────────────────────────────────────────────────────────────────

def run_sweep(start_year: int, trade_amount: float, stop_loss_pct: float) -> None:
    thresholds = [0.05, 0.10, 0.12, 0.15, 0.18, 0.20, 0.25, 0.30]
    results = []

    for ev in thresholds:
        print(f"\n── Sweep: min_ev={ev:.0%} ──────────────────────────────")
        r = run_backtest(
            start_year=start_year,
            min_ev=ev,
            trade_amount=trade_amount,
            stop_loss_pct=stop_loss_pct,
            verbose=False,
        )
        if r:
            results.append(r)
            print(f"  trades={r['total_trades']:,}  "
                  f"wr={r['win_rate']*100:.1f}%  "
                  f"pnl=${r['total_pnl_dollars']:.2f}  "
                  f"sharpe={r['sharpe_ratio']:.2f}  "
                  f"maxdd={r['max_drawdown_percent']:.1f}%")

    if not results:
        return

    print("\n" + "=" * 80)
    print("  SWEEP SUMMARY — ranked by Sharpe ratio")
    print("=" * 80)
    print(f"{'min_ev':>8}  {'trades':>8}  {'win_rate':>9}  {'pnl':>8}  "
          f"{'sharpe':>7}  {'max_dd':>7}  {'cons_loss':>9}")
    print("-" * 80)
    for r in sorted(results, key=lambda x: -x["sharpe_ratio"]):
        print(f"  {r['min_ev']:>5.0%}  {r['total_trades']:>8,}  "
              f"{r['win_rate']*100:>8.1f}%  "
              f"${r['total_pnl_dollars']:>7.2f}  "
              f"{r['sharpe_ratio']:>7.3f}  "
              f"{r['max_drawdown_percent']:>6.1f}%  "
              f"{r['max_consecutive_losses']:>9}")


# ─────────────────────────────────────────────────────────────────────────────
# Monte Carlo parameter search
# ─────────────────────────────────────────────────────────────────────────────

MONTE_CARLO_OUT = r'C:\Users\alxnt\kalshi-bot\monte_carlo_results.json'

PARAM_SPACE = {
    "min_ev":              [0.08, 0.10, 0.12, 0.15, 0.18, 0.20, 0.25],
    "stop_loss_pct":       [20.0, 25.0, 30.0, 35.0, 40.0, 50.0],
    "entry_window":        [(360, 540), (420, 600), (480, 660), (360, 660), (300, 780)],
    "min_confidence":      [50, 55, 60, 65, 70],
    "ob_imbalance_thresh": [0.05, 0.10, 0.15, 0.20],
}

# Pre-computed pool of all unique combinations (2,520 total)
_ALL_COMBOS = list(itertools.product(
    PARAM_SPACE["min_ev"],
    PARAM_SPACE["stop_loss_pct"],
    PARAM_SPACE["entry_window"],
    PARAM_SPACE["min_confidence"],
    PARAM_SPACE["ob_imbalance_thresh"],
))


def run_monte_carlo(n_simulations: int = 10_000, start_year: int = 2020,
                    trade_amount: float = 5.0) -> None:
    """
    Randomly sample from PARAM_SPACE, run the full backtest for each sample,
    track the top-20 by Sharpe ratio, write results to monte_carlo_results.json,
    and print a final summary.

    ob_imbalance_thresh and min_confidence act as additional filters applied
    on top of the existing printer_brain logic: a trade is only taken when the
    simulated order-book imbalance exceeds the threshold AND the brain confidence
    is at or above the minimum.

    entry_window = (lo_sec, hi_sec) — a trade in a window must originate between
    lo_sec and hi_sec seconds into the 15-minute window (900 s total).
    """
    rng_mc = random.Random(0)          # reproducible sampling
    top20: list[dict] = []             # sorted best→worst by Sharpe
    all_results: list[dict] = []

    total_combos = len(_ALL_COMBOS)
    effective_n  = min(n_simulations, total_combos)
    print(f"\nMonte Carlo — {effective_n:,} simulations "
          f"({total_combos:,} unique combos, seed=0)")
    print(f"  start_year={start_year}, trade_amount=${trade_amount}")
    print("  Writing progress to:", MONTE_CARLO_OUT)
    print()

    # Shuffle once, then iterate — avoids duplicate runs while staying random
    combo_pool = list(_ALL_COMBOS)
    rng_mc.shuffle(combo_pool)
    combos_to_run = combo_pool[:effective_n]

    t0 = time.time()
    for i, (min_ev, stop_loss_pct, entry_window, min_confidence,
            ob_thresh) in enumerate(combos_to_run, 1):

        # run_backtest uses seconds_elapsed implicitly via minute_in.
        # We pass extra kwargs as a filter wrapper.
        r = run_backtest(
            start_year    = start_year,
            min_ev        = min_ev,
            trade_amount  = trade_amount,
            stop_loss_pct = stop_loss_pct,
            entry_lo_sec  = entry_window[0],
            entry_hi_sec  = entry_window[1],
            min_confidence = min_confidence,
            ob_imbalance_thresh = ob_thresh,
            verbose       = False,
        )

        if not r:
            continue

        r["params"] = {
            "min_ev":              min_ev,
            "stop_loss_pct":       stop_loss_pct,
            "entry_window":        list(entry_window),
            "min_confidence":      min_confidence,
            "ob_imbalance_thresh": ob_thresh,
        }
        all_results.append(r)

        # Maintain top-20 sorted by Sharpe (descending)
        top20.append(r)
        top20.sort(key=lambda x: -x["sharpe_ratio"])
        if len(top20) > 20:
            top20.pop()

        # Progress every 50 runs
        if i % 50 == 0 or i == effective_n:
            elapsed = time.time() - t0
            eta     = (elapsed / i) * (effective_n - i)
            best_s  = top20[0]["sharpe_ratio"] if top20 else 0.0
            print(f"  [{i:>5}/{effective_n}]  "
                  f"elapsed={elapsed:.0f}s  eta={eta:.0f}s  "
                  f"best_sharpe={best_s:.3f}")

            # Incremental save so user can Ctrl-C early
            _save_mc_results(top20, all_results, i, effective_n)

    _save_mc_results(top20, all_results, effective_n, effective_n)
    _print_mc_summary(top20)


def _save_mc_results(top20: list, all_results: list,
                     done: int, total: int) -> None:
    """Write current top-20 + metadata to JSON."""
    payload = {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "simulations_done": done,
        "simulations_total": total,
        "top_20": [
            {
                "rank":              rank + 1,
                "sharpe_ratio":      r["sharpe_ratio"],
                "win_rate":          r["win_rate"],
                "total_pnl_dollars": r["total_pnl_dollars"],
                "total_trades":      r["total_trades"],
                "max_drawdown_pct":  r["max_drawdown_percent"],
                "params":            r["params"],
            }
            for rank, r in enumerate(top20)
        ],
    }
    try:
        with open(MONTE_CARLO_OUT, "w") as f:
            json.dump(payload, f, indent=2)
    except OSError as e:
        print(f"  [warn] could not write {MONTE_CARLO_OUT}: {e}")


def _print_mc_summary(top20: list) -> None:
    if not top20:
        print("\nNo valid results found.")
        return

    best = top20[0]
    W = 70
    print("\n" + "=" * W)
    print("  MONTE CARLO SUMMARY — top 20 by Sharpe ratio")
    print("=" * W)
    print(f"  {'Rank':>4}  {'Sharpe':>7}  {'WinRate':>8}  "
          f"{'PnL':>8}  {'Trades':>7}  {'MaxDD':>7}  params")
    print("-" * W)
    for rank, r in enumerate(top20, 1):
        p = r["params"]
        param_str = (f"ev={p['min_ev']:.0%} sl={p['stop_loss_pct']:.0f}% "
                     f"win={p['entry_window'][0]}-{p['entry_window'][1]}s "
                     f"conf={p['min_confidence']} ob={p['ob_imbalance_thresh']:.2f}")
        print(f"  {rank:>4}  {r['sharpe_ratio']:>7.3f}  "
              f"{r['win_rate']*100:>7.1f}%  "
              f"${r['total_pnl_dollars']:>7.2f}  "
              f"{r['total_trades']:>7,}  "
              f"{r['max_drawdown_percent']:>6.1f}%  {param_str}")
    print("=" * W)
    print(f"\n  BEST PARAMS:")
    bp = best["params"]
    print(f"    min_ev              = {bp['min_ev']:.0%}")
    print(f"    stop_loss_pct       = {bp['stop_loss_pct']:.0f}%")
    print(f"    entry_window        = {bp['entry_window'][0]}–{bp['entry_window'][1]} s into window")
    print(f"    min_confidence      = {bp['min_confidence']}")
    print(f"    ob_imbalance_thresh = {bp['ob_imbalance_thresh']:.2f}")
    print(f"\n  Expected win rate     : {best['win_rate']*100:.1f}%")
    ann_return = (best["total_pnl_dollars"] / (best["total_trades"] * 5.0) *
                  35_040 * 5.0) if best["total_trades"] else 0
    print(f"  Expected annual PnL   : ${ann_return:,.0f}  (at $5/trade, 35k slots/yr)")
    print(f"  Sharpe ratio          : {best['sharpe_ratio']:.3f}")
    print(f"\n  Full results saved to : {MONTE_CARLO_OUT}")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backtest printer_brain strategy against historical BTC data"
    )
    parser.add_argument("--start-year",   type=int,   default=2020,
                        help="First year of data to include (default: 2020)")
    parser.add_argument("--ev",           type=float, default=0.15,
                        help="Min EV threshold, e.g. 0.15 = 15%% (default: 0.15)")
    parser.add_argument("--amount",       type=float, default=5.0,
                        help="Trade amount in dollars (default: 5)")
    parser.add_argument("--stop-loss",    type=float, default=35.0,
                        help="Stop-loss percent (default: 35)")
    parser.add_argument("--sweep",        action="store_true",
                        help="Run multiple EV thresholds and compare results")
    parser.add_argument("--monte-carlo",  action="store_true",
                        help="Run Monte Carlo parameter search (10k simulations)")
    parser.add_argument("--mc-sims",      type=int,   default=10_000,
                        help="Number of Monte Carlo simulations (default: 10000)")
    parser.add_argument("--no-db",        action="store_true",
                        help="Skip writing to kalshi_bot.db")
    args = parser.parse_args()

    if args.monte_carlo:
        run_monte_carlo(
            n_simulations = args.mc_sims,
            start_year    = args.start_year,
            trade_amount  = args.amount,
        )
    elif args.sweep:
        run_sweep(args.start_year, args.amount, args.stop_loss)
    else:
        result = run_backtest(
            start_year    = args.start_year,
            min_ev        = args.ev,
            trade_amount  = args.amount,
            stop_loss_pct = args.stop_loss,
        )
        if result:
            print_report(result)
            if not args.no_db:
                write_to_db(result, args.start_year)
                print(f"\nResults saved to {DB_PATH} → stress_test_results table.")
