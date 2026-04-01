#!/usr/bin/env python3
"""
backtesting/stress_test.py  --  Execution noise stress test.

Applies randomised slippage, latency-induced misses, and partial fills
to the base trade list from a single backtest run.  Because the heavy
work (iterating all BTC windows) is done only ONCE, 500 iterations
complete in seconds, not hours.

Noise model (per iteration)
---------------------------
  Slippage     : uniform(0, max_slippage_bps) per trade, adverse
                 (raises entry_c, reducing P&L)
  Latency miss : each trade has a probability of being rejected
                 based on the supplied latency level
  Partial fill : placed trades filled at uniform(50%, 100%) of contracts

Degradation curve
-----------------
  Deterministic sweep: exact slippage 0..20 bps in 2 bps steps, no
  partial-fill noise, applied to all trades.

Latency miss table
------------------
  For each level in [0, 50, 100, 200, 500, 1000] ms: expected fraction
  of trades missed and resulting P&L impact.

Usage
-----
  python backtest.py --stress-test
  python backtest.py --stress-test --st-iters 500 --st-max-slippage 20
  python backtest.py --stress-test --st-latency-ms 100

Output
------
  results/stress_test_report.json
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
import time
from datetime import datetime, timezone

# ── Paths ─────────────────────────────────────────────────────────────────────
_THIS_DIR    = os.path.dirname(os.path.abspath(__file__))
_BASE_DIR    = os.path.dirname(_THIS_DIR)
_RESULTS_DIR = os.path.join(_BASE_DIR, "results")
REPORT_PATH  = os.path.join(_RESULTS_DIR, "stress_test_report.json")

# ── Latency miss probability model ────────────────────────────────────────────
# Fraction of orders rejected / window missed at each latency level.
LATENCY_LEVELS    = [0, 50, 100, 200, 500, 1000]
LATENCY_MISS_PROB = {
    0:    0.000,
    50:   0.003,
    100:  0.010,
    200:  0.030,
    500:  0.070,
    1000: 0.150,
}

SLIPPAGE_SWEEP_BPS = list(range(0, 22, 2))   # 0, 2, 4, ..., 20


# ── Core helpers ──────────────────────────────────────────────────────────────

def _recalc_pnl(
    trade: dict,
    slippage_bps: float,
    fill_pct: float,
    trade_amount: float,
) -> float | None:
    """
    Return noisy P&L for one trade after adverse slippage and partial fill.

    Returns None when slippage pushes entry_c to an invalid level (>= 99c),
    meaning the trade would not have been placed.
    """
    entry_c     = trade["entry_c"]
    noisy_entry = entry_c * (1.0 + slippage_bps / 10_000.0)

    if noisy_entry >= 99.0:
        return None               # cannot buy this contract at this price

    exit_price = trade["exit_price"]
    contracts  = max(1, int(trade_amount * 100.0 / noisy_entry))
    pnl        = (exit_price - noisy_entry) * contracts / 100.0
    return pnl * fill_pct


def _metrics(pnls: list[float]) -> dict:
    """Aggregate stats from a P&L sequence (one iteration)."""
    if not pnls:
        return {"total_pnl": 0.0, "win_rate": 0.0, "sharpe": 0.0,
                "max_drawdown": 0.0, "avg_pnl": 0.0}

    total = len(pnls)
    total_pnl = sum(pnls)
    win_rate  = sum(1 for p in pnls if p > 0) / total

    mu  = total_pnl / total
    var = sum((p - mu) ** 2 for p in pnls) / max(total - 1, 1)
    sd  = math.sqrt(var) if var > 0 else 0.0
    sharpe = mu / sd * math.sqrt(35_040) if sd > 0 else 0.0

    # Max drawdown on cumulative P&L curve
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        cum += p
        if cum > peak:
            peak = cum
        if peak > 0:
            dd = (peak - cum) / peak * 100.0
            if dd > max_dd:
                max_dd = dd

    return {
        "total_pnl":    total_pnl,
        "win_rate":     win_rate,
        "sharpe":       sharpe,
        "max_drawdown": max_dd,
        "avg_pnl":      mu,
    }


def _percentile(lst: list[float], pct: float) -> float:
    if not lst:
        return 0.0
    s = sorted(lst)
    idx = max(0, min(int(len(s) * pct / 100.0), len(s) - 1))
    return s[idx]


# ── Public entry point ────────────────────────────────────────────────────────

def run_stress_test(
    n_iters:          int   = 500,
    max_slippage_bps: float = 20.0,
    latency_ms:       int   = 0,
    trade_amount:     float = 5.0,
    start_year:       int   = 2020,
    seed:             int   = 42,
) -> None:
    """
    Full stress-test pipeline:
      1. Load best params from monte_carlo_results.json
      2. Run backtest once to capture all trades
      3. Monte Carlo noise simulation (n_iters)
      4. Deterministic slippage degradation curve
      5. Latency miss table
      6. Print + save report
    """
    if _BASE_DIR not in sys.path:
        sys.path.insert(0, _BASE_DIR)

    import backtest as bt       # noqa: PLC0415  (lazy import, avoids circular dep)

    # ── Load best params ──────────────────────────────────────────────────────
    params = bt._load_best_params()

    print("\n" + "=" * 68)
    print("  EXECUTION NOISE STRESS TEST")
    print("=" * 68)
    print(f"  Params   :  ev={params['min_ev']:.0%}  "
          f"sl={params['stop_loss_pct']:.0f}%  conf={params['min_confidence']}")
    print(f"  Iters    :  {n_iters:,}")
    print(f"  Max slip :  {max_slippage_bps:.0f} bps")
    print(f"  Latency  :  {latency_ms} ms  "
          f"(miss prob {LATENCY_MISS_PROB.get(latency_ms, 0)*100:.1f}%)")
    print(f"  Trade $  :  ${trade_amount:.2f}")
    print()

    # ── Single base backtest — captures full trade list ───────────────────────
    print("  Running base backtest (once) ...")
    t0 = time.time()
    base_trades: list[dict] = []
    base_result = bt.run_backtest(
        start_year     = start_year,
        min_ev         = params["min_ev"],
        trade_amount   = trade_amount,
        stop_loss_pct  = params["stop_loss_pct"],
        min_confidence = params["min_confidence"],
        verbose        = False,
        _trades_out    = base_trades,
    )

    if not base_result or not base_trades:
        print("  [error] Base backtest returned no trades. Aborting.")
        return

    n_base = len(base_trades)
    base_pnl = base_result["total_pnl_dollars"]
    print(f"  Base: {n_base:,} trades  P&L=${base_pnl:.2f}  "
          f"WR={base_result['win_rate']*100:.1f}%  "
          f"Sharpe={base_result['sharpe_ratio']:.3f}  "
          f"({time.time()-t0:.1f}s)")
    print()

    # ── Monte Carlo noise iterations ──────────────────────────────────────────
    print(f"  Running {n_iters:,} noise iterations ...")
    rng = random.Random(seed)

    miss_prob  = LATENCY_MISS_PROB.get(latency_ms, 0.0)

    iter_totals:    list[float] = []
    iter_winrates:  list[float] = []
    iter_sharpes:   list[float] = []
    iter_drawdowns: list[float] = []

    for _ in range(n_iters):
        iter_pnls: list[float] = []
        for trade in base_trades:
            # Latency miss
            if rng.random() < miss_prob:
                continue
            # Random slippage: 0 to max_slippage_bps
            slip  = rng.uniform(0.0, max_slippage_bps)
            # Partial fill: 50-100%
            fill  = rng.uniform(0.50, 1.0)
            noisy = _recalc_pnl(trade, slip, fill, trade_amount)
            if noisy is not None:
                iter_pnls.append(noisy)

        if iter_pnls:
            m = _metrics(iter_pnls)
            iter_totals.append(m["total_pnl"])
            iter_winrates.append(m["win_rate"])
            iter_sharpes.append(m["sharpe"])
            iter_drawdowns.append(m["max_drawdown"])

    n_valid = len(iter_totals)
    if n_valid == 0:
        print("  [error] All iterations produced no trades. Aborting.")
        return

    be_count = sum(1 for p in iter_totals if p >= 0)
    mc = {
        "base_pnl":           round(base_pnl, 2),
        "mean_pnl":           round(sum(iter_totals) / n_valid, 2),
        "median_pnl":         round(_percentile(iter_totals, 50), 2),
        "p5_pnl":             round(_percentile(iter_totals, 5),  2),
        "p95_pnl":            round(_percentile(iter_totals, 95), 2),
        "mean_win_rate":      round(sum(iter_winrates)  / n_valid, 4),
        "mean_sharpe":        round(sum(iter_sharpes)   / n_valid, 3),
        "mean_max_drawdown":  round(sum(iter_drawdowns) / n_valid, 2),
        "breakeven_rate":     round(be_count / n_valid, 4),
        "iterations_valid":   n_valid,
    }

    # ── Deterministic degradation curve ──────────────────────────────────────
    print("  Computing degradation curve ...")
    degradation: list[dict] = []
    for bps in SLIPPAGE_SWEEP_BPS:
        bps_pnls = [
            p for trade in base_trades
            if (p := _recalc_pnl(trade, bps, 1.0, trade_amount)) is not None
        ]
        total_p    = round(sum(bps_pnls), 2)
        pct_delta  = round((total_p - base_pnl) / abs(base_pnl) * 100, 1) if base_pnl != 0 else 0.0
        degradation.append({
            "slippage_bps": bps,
            "total_pnl":    total_p,
            "pct_vs_base":  pct_delta,
            "trades_valid": len(bps_pnls),
        })

    # ── Latency miss table ────────────────────────────────────────────────────
    print("  Computing latency miss table ...")
    avg_ppt = base_pnl / n_base if n_base else 0.0
    latency_table: list[dict] = []
    for lat in LATENCY_LEVELS:
        mp          = LATENCY_MISS_PROB[lat]
        exp_missed  = n_base * mp
        pnl_impact  = round(exp_missed * avg_ppt, 2)
        latency_table.append({
            "latency_ms":              lat,
            "miss_probability":        mp,
            "expected_missed_trades":  round(exp_missed, 1),
            "pnl_impact":              pnl_impact,
            "net_pnl":                 round(base_pnl - pnl_impact, 2),
        })

    # ── Print + save ──────────────────────────────────────────────────────────
    _print_summary(base_result, mc, degradation, latency_table,
                   n_iters, max_slippage_bps, latency_ms)
    _save_report(params, base_result, mc, degradation, latency_table,
                 n_iters, max_slippage_bps, latency_ms, trade_amount)


# ── Output helpers ────────────────────────────────────────────────────────────

def _print_summary(
    base_r: dict,
    mc: dict,
    degradation: list[dict],
    latency_table: list[dict],
    n_iters: int,
    max_slip: float,
    latency_ms: int,
) -> None:
    W = 68
    print("\n" + "=" * W)
    print("  STRESS TEST RESULTS")
    print("=" * W)
    print(f"  Base P&L       : ${base_r['total_pnl_dollars']:>10.2f}"
          f"  ({base_r['total_trades']:,} trades)")
    print(f"  Base win rate  : {base_r['win_rate']*100:>9.1f}%")
    print(f"  Base Sharpe    : {base_r['sharpe_ratio']:>10.3f}")
    print()

    slip_range = f"0-{max_slip:.0f} bps"
    lat_note   = f"latency={latency_ms}ms"
    print(f"  -- Noise Simulation ({n_iters} iters, slip {slip_range},"
          f" fill 50-100%, {lat_note}) --")
    print(f"  Mean P&L       : ${mc['mean_pnl']:>10.2f}")
    print(f"  Median P&L     : ${mc['median_pnl']:>10.2f}")
    print(f"  P5  P&L        : ${mc['p5_pnl']:>10.2f}")
    print(f"  P95 P&L        : ${mc['p95_pnl']:>10.2f}")
    print(f"  Mean win rate  : {mc['mean_win_rate']*100:>9.1f}%")
    print(f"  Mean Sharpe    : {mc['mean_sharpe']:>10.3f}")
    print(f"  Mean max DD    : {mc['mean_max_drawdown']:>9.1f}%")
    print(f"  Break-even     : {mc['breakeven_rate']*100:>9.1f}%"
          f"  (iterations with P&L >= 0)")

    # Break-even verdict
    be = mc["breakeven_rate"]
    if be >= 0.90:
        be_verdict = "ROBUST"
    elif be >= 0.70:
        be_verdict = "ACCEPTABLE"
    elif be >= 0.50:
        be_verdict = "FRAGILE"
    else:
        be_verdict = "WARN — strategy loses money under most noise scenarios"
    print(f"  Verdict        : {be_verdict}")

    print()
    print("  -- Degradation Curve (deterministic slippage, no fill noise) --")
    print(f"  {'Slippage':>10}  {'Total P&L':>12}  {'Delta vs base':>14}  "
          f"{'Trades kept':>12}")
    print("  " + "-" * 52)
    for d in degradation:
        print(f"  {d['slippage_bps']:>8} bps  "
              f"${d['total_pnl']:>10.2f}  "
              f"{d['pct_vs_base']:>+13.1f}%  "
              f"{d['trades_valid']:>12,}")

    print()
    print("  -- Latency Miss Table --")
    print(f"  {'Latency':>10}  {'Miss%':>7}  {'Expected misses':>16}"
          f"  {'P&L impact':>12}  {'Net P&L':>10}")
    print("  " + "-" * 60)
    for r in latency_table:
        print(f"  {r['latency_ms']:>7} ms  "
              f"{r['miss_probability']*100:>6.1f}%  "
              f"{r['expected_missed_trades']:>16.1f}  "
              f"${r['pnl_impact']:>10.2f}  "
              f"${r['net_pnl']:>9.2f}")
    print("=" * W)


def _save_report(
    params: dict,
    base_r: dict,
    mc: dict,
    degradation: list[dict],
    latency_table: list[dict],
    n_iters: int,
    max_slip: float,
    latency_ms: int,
    trade_amount: float,
) -> None:
    os.makedirs(_RESULTS_DIR, exist_ok=True)
    report = {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "params":        params,
        "config": {
            "n_iters":          n_iters,
            "max_slippage_bps": max_slip,
            "latency_ms":       latency_ms,
            "trade_amount":     trade_amount,
        },
        "base_backtest": {
            "total_trades":      base_r["total_trades"],
            "win_rate":          base_r["win_rate"],
            "total_pnl_dollars": base_r["total_pnl_dollars"],
            "sharpe_ratio":      base_r["sharpe_ratio"],
            "max_drawdown_pct":  base_r["max_drawdown_percent"],
        },
        "noise_simulation":   mc,
        "degradation_curve":  degradation,
        "latency_miss_table": latency_table,
    }
    with open(REPORT_PATH, "w") as fh:
        json.dump(report, fh, indent=2)
    print(f"\n  Report saved to: {REPORT_PATH}")


# ── Standalone CLI ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description="Execution noise stress test (run via: python backtest.py --stress-test)"
    )
    ap.add_argument("--iters",        type=int,   default=500)
    ap.add_argument("--max-slip",     type=float, default=20.0)
    ap.add_argument("--latency-ms",   type=int,   default=0)
    ap.add_argument("--amount",       type=float, default=5.0)
    ap.add_argument("--start-year",   type=int,   default=2020)
    args = ap.parse_args()
    run_stress_test(
        n_iters          = args.iters,
        max_slippage_bps = args.max_slip,
        latency_ms       = args.latency_ms,
        trade_amount     = args.amount,
        start_year       = args.start_year,
    )
