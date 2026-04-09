#!/usr/bin/env python3
"""
backtesting/walk_forward.py — Walk-Forward Validation (WFV) engine.

Rolls an optimisation window across the TRAIN partition only.
The OOS holdout defined in data/split_config.json is never touched.

How it works
------------
  1. Load data/split_config.json → train boundaries.
  2. Load the CSV filtered to the train partition.
  3. Divide train windows into N equal sequential chunks.
  4. For each chunk:
       a. In-window train  = first 80%  → run mini Monte Carlo → best params
       b. Forward period   = last  20%  → run backtest with best params
       c. Record metrics for both halves; flag PASS / FAIL.
  5. Calculate WFV Efficiency Ratio:
       avg(forward total P&L across windows) / avg(in-window total P&L across windows)
     ≥ 0.70  →  PASS   (params generalise well)
     0.50–0.70 →  MARGINAL
     < 0.50  →  WARN OVERFIT
  6. Save full results to results/wfv_report.json.

Usage
-----
    # Standalone:
    python backtesting/walk_forward.py
    python backtesting/walk_forward.py --windows 8 --mc-sims 50 --amount 5

    # Via backtest.py:
    python backtest.py --walk-forward
    python backtest.py --walk-forward --wf-windows 8 --wf-mc-sims 50
"""

import argparse
import json
import math
import os
import random
import sys
import time
from datetime import datetime, timezone

# ── Path setup — makes `import backtest` work from any cwd ───────────────────
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT     = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from backtest import load_data, run_backtest, _ALL_COMBOS  # noqa: E402

# ── Paths ─────────────────────────────────────────────────────────────────────
_SPLIT_CFG_PATH = os.path.join(_ROOT, "data", "split_config.json")
_RESULTS_DIR    = os.path.join(_ROOT, "results")
WFV_REPORT_PATH = os.path.join(_RESULTS_DIR, "wfv_report.json")


# ── Internal helpers ──────────────────────────────────────────────────────────

def _load_split_cfg() -> dict | None:
    if not os.path.exists(_SPLIT_CFG_PATH):
        return None
    with open(_SPLIT_CFG_PATH) as fh:
        return json.load(fh)


def _slice(windows, price_lookup, idx_lo: int, idx_hi: int):
    """Positional slice of (windows DataFrame, price_lookup Series)."""
    w  = windows.iloc[idx_lo:idx_hi]
    pl = price_lookup[price_lookup.index.isin(w.index)]
    return w, pl


def _date_range(windows) -> tuple[str, str]:
    """Return (start_date, end_date) strings for a windows DataFrame."""
    fmt = lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    return fmt(windows.index[0]), fmt(windows.index[-1])


def _metrics(r: dict) -> dict:
    """Flatten a run_backtest() result to the key metrics we care about."""
    if not r:
        return dict(total_trades=0, win_rate=0.0, total_pnl=0.0,
                    pnl_per_trade=0.0, sharpe=0.0, max_drawdown=0.0)
    t   = r.get("total_trades", 0)
    pnl = r.get("total_pnl_dollars", 0.0)
    return {
        "total_trades":  t,
        "win_rate":      round(r.get("win_rate", 0.0) * 100, 2),
        "total_pnl":     round(pnl, 4),
        "pnl_per_trade": round(pnl / t, 6) if t else 0.0,
        "sharpe":        round(r.get("sharpe_ratio", 0.0), 4),
        "max_drawdown":  round(r.get("max_drawdown_percent", 0.0), 2),
    }


def _mini_mc(windows, price_lookup, n_sims: int,
             trade_amount: float, seed: int = 0) -> tuple[dict | None, dict]:
    """
    Run up to n_sims Monte Carlo samples from PARAM_SPACE on the given slice.

    Returns
    -------
    (best_params_dict | None, in_window_metrics_dict)
    """
    rng    = random.Random(seed)
    combos = list(_ALL_COMBOS)
    rng.shuffle(combos)
    combos = combos[:min(n_sims, len(combos))]

    best_params  = None
    best_sharpe  = -math.inf
    best_result  = None

    for min_ev, min_confidence in combos:
        r = run_backtest(
            min_ev         = min_ev,
            trade_amount   = trade_amount,
            min_confidence = min_confidence,
            verbose        = False,
            _windows       = windows,
            _price_lookup  = price_lookup,
        )
        if not r:
            continue
        s = r.get("sharpe_ratio", -math.inf)
        if s > best_sharpe:
            best_sharpe = s
            best_result = r
            best_params = {
                "min_ev":         min_ev,
                "min_confidence": min_confidence,
            }

    return best_params, _metrics(best_result)


# ── Main engine ───────────────────────────────────────────────────────────────

def run_walk_forward(
    n_windows: int          = 8,
    in_window_train_pct: float = 0.80,
    n_mc_sims: int          = 50,
    start_year: int         = 2020,
    trade_amount: float     = 5.0,
) -> None:
    """
    Run Walk-Forward Validation over the TRAIN partition.

    Parameters
    ----------
    n_windows           Number of sequential rolling windows (default 8).
    in_window_train_pct Fraction of each window used for optimisation (default 0.80).
    n_mc_sims           Monte Carlo simulations per window optimisation (default 50).
    start_year          Earliest year of CSV data to load.
    trade_amount        Dollars per trade in each backtest.
    """
    # ── Require split config ─────────────────────────────────────────────────
    split_cfg = _load_split_cfg()
    if split_cfg is None:
        print("[WFV] ERROR: data/split_config.json not found.")
        print("      Run first:  python data/splitter.py")
        sys.exit(1)

    fwd_pct = 1.0 - in_window_train_pct
    W = 72
    print("\n" + "=" * W)
    print("  WALK-FORWARD VALIDATION")
    print("=" * W)
    print(f"  Windows         : {n_windows}")
    print(f"  In-win train    : {in_window_train_pct:.0%}  |  forward: {fwd_pct:.0%}")
    print(f"  MC sims/window  : {n_mc_sims}  (from {len(_ALL_COMBOS)} unique combos)")
    print(f"  Train data      : {split_cfg['train_start_date']} → "
          f"{split_cfg['train_end_date']}  ({split_cfg['train_windows']:,} windows)")
    print(f"  OOS holdout     : {split_cfg['oos_start_date']} → "
          f"{split_cfg['oos_end_date']}  [LOCKED — not used]")
    print()

    # ── Load train data ──────────────────────────────────────────────────────
    print("  Loading train partition ...")
    all_w, all_pl = load_data(start_year, verbose=True, mode="train")
    n_total = len(all_w)

    min_needed = n_windows * 20          # at least 20 windows per chunk
    if n_total < min_needed:
        print(f"  [error] Only {n_total:,} train windows — need ≥{min_needed:,} "
              f"for {n_windows} WFV windows.")
        sys.exit(1)

    chunk_size    = n_total // n_windows
    iw_train_size = int(chunk_size * in_window_train_pct)
    iw_test_size  = chunk_size - iw_train_size

    print(f"\n  Chunk size     : {chunk_size:,} windows  "
          f"→  {iw_train_size:,} train + {iw_test_size:,} forward per window")
    print()

    window_results: list[dict] = []
    t_total = time.time()

    for w in range(n_windows):
        chunk_lo   = w * chunk_size
        train_lo   = chunk_lo
        train_hi   = chunk_lo + iw_train_size
        forward_lo = train_hi
        forward_hi = chunk_lo + chunk_size

        iw_w,  iw_pl  = _slice(all_w, all_pl, train_lo,   train_hi)
        fwd_w, fwd_pl = _slice(all_w, all_pl, forward_lo, forward_hi)

        if len(iw_w) == 0 or len(fwd_w) == 0:
            print(f"  Window {w+1}/{n_windows}: empty slice — skipping.")
            continue

        iw_d0, iw_d1   = _date_range(iw_w)
        fwd_d0, fwd_d1 = _date_range(fwd_w)

        print(f"  ── Window {w+1}/{n_windows} "
              + "─" * (W - 14 - len(str(w+1)) - len(str(n_windows))))
        print(f"     In-win train : {iw_d0} → {iw_d1}  ({len(iw_w):,} windows)")
        print(f"     Forward test : {fwd_d0} → {fwd_d1}  ({len(fwd_w):,} windows)")

        t0 = time.time()
        print(f"     Optimising ({n_mc_sims} MC sims) ...", end="", flush=True)

        best_params, iw_metrics = _mini_mc(iw_w, iw_pl, n_mc_sims,
                                            trade_amount, seed=w)

        elapsed = time.time() - t0
        print(f" {elapsed:.1f}s")

        if best_params is None:
            print("     [skip] MC returned no valid results for this window.\n")
            continue

        print(f"     Best params  : ev={best_params['min_ev']:.0%}  "
              f"conf={best_params['min_confidence']}")

        # ── Run forward period with best params ───────────────────────────────
        fwd_r = run_backtest(
            min_ev         = best_params["min_ev"],
            trade_amount   = trade_amount,
            min_confidence = best_params["min_confidence"],
            verbose        = False,
            _windows       = fwd_w,
            _price_lookup  = fwd_pl,
        )
        fwd_metrics = _metrics(fwd_r)

        flag = "PASS" if fwd_metrics["total_pnl"] >= 0 else "FAIL"

        print(f"     In-win  : "
              f"trades={iw_metrics['total_trades']:,}  "
              f"wr={iw_metrics['win_rate']:.1f}%  "
              f"pnl=${iw_metrics['total_pnl']:.2f}  "
              f"sharpe={iw_metrics['sharpe']:.3f}")
        print(f"     Forward : "
              f"trades={fwd_metrics['total_trades']:,}  "
              f"wr={fwd_metrics['win_rate']:.1f}%  "
              f"pnl=${fwd_metrics['total_pnl']:.2f}  "
              f"sharpe={fwd_metrics['sharpe']:.3f}  "
              f"[{flag}]")
        print()

        window_results.append({
            "window":         w + 1,
            "iw_train_range": f"{iw_d0} → {iw_d1}",
            "forward_range":  f"{fwd_d0} → {fwd_d1}",
            "iw_train_size":  int(len(iw_w)),
            "forward_size":   int(len(fwd_w)),
            "best_params":    best_params,
            "in_window":      iw_metrics,
            "forward":        fwd_metrics,
            "pass_fail":      flag,
        })

    elapsed_total = time.time() - t_total

    if not window_results:
        print("[WFV] No windows completed. Check data and split config.")
        return

    # ── WFV Efficiency Ratio ─────────────────────────────────────────────────
    # Specified as: avg(forward total PnL) / avg(in-window total PnL)
    avg_iw_pnl  = sum(r["in_window"]["total_pnl"]  for r in window_results) / len(window_results)
    avg_fwd_pnl = sum(r["forward"]["total_pnl"]    for r in window_results) / len(window_results)
    efficiency  = round(avg_fwd_pnl / avg_iw_pnl, 4) if avg_iw_pnl > 0 else 0.0

    # Per-trade version (removes window-size bias) — shown alongside
    valid_pt = [r for r in window_results if r["in_window"]["pnl_per_trade"] > 0]
    if valid_pt:
        avg_iw_ppt  = sum(r["in_window"]["pnl_per_trade"]  for r in valid_pt) / len(valid_pt)
        avg_fwd_ppt = sum(r["forward"]["pnl_per_trade"]    for r in valid_pt) / len(valid_pt)
        eff_pt      = round(avg_fwd_ppt / avg_iw_ppt, 4) if avg_iw_ppt > 0 else 0.0
    else:
        avg_iw_ppt = avg_fwd_ppt = eff_pt = 0.0

    passes    = sum(1 for r in window_results if r["pass_fail"] == "PASS")
    pass_rate = round(passes / len(window_results) * 100, 1)

    _print_summary(window_results, efficiency, eff_pt,
                   avg_iw_pnl, avg_fwd_pnl, pass_rate, elapsed_total)
    _save_report(window_results, efficiency, eff_pt, pass_rate,
                 split_cfg, n_windows, n_mc_sims, in_window_train_pct, trade_amount)


# ── Output helpers ────────────────────────────────────────────────────────────

def _print_summary(results, efficiency, eff_pt,
                   avg_iw_pnl, avg_fwd_pnl, pass_rate, elapsed) -> None:
    W = 72
    print("=" * W)
    print("  WALK-FORWARD RESULTS — per window")
    print("=" * W)
    print(f"  {'Win':>3}  {'Forward Period':>23}  "
          f"{'WR':>6}  {'Fwd P&L':>9}  {'Sharpe':>7}  {'MaxDD':>6}  {'Flag':>5}")
    print("-" * W)
    for r in results:
        m = r["forward"]
        print(f"  {r['window']:>3}  {r['forward_range']:>23}  "
              f"{m['win_rate']:>5.1f}%  "
              f"${m['total_pnl']:>8.2f}  "
              f"{m['sharpe']:>7.3f}  "
              f"{m['max_drawdown']:>5.1f}%  "
              f"{r['pass_fail']:>5}")
    print("=" * W)

    passes = sum(1 for r in results if r["pass_fail"] == "PASS")
    print(f"\n  Completed   : {len(results)} windows  ({elapsed:.0f}s total)")
    print(f"  Pass / Fail : {passes} / {len(results) - passes}  ({pass_rate:.0f}% pass rate)")
    print(f"\n  Avg in-win  P&L : ${avg_iw_pnl:>10.4f}  (per window)")
    print(f"  Avg forward P&L : ${avg_fwd_pnl:>10.4f}  (per window)")
    print()

    # Verdict line
    verdict_str = (
        "PASS"           if efficiency >= 0.70 else
        "MARGINAL"       if efficiency >= 0.50 else
        "WARN OVERFIT"
    )
    print(f"  WFV Efficiency Ratio   : {efficiency:.4f}   [{verdict_str}]")
    print(f"  Per-trade Efficiency   : {eff_pt:.4f}   (bias-corrected)")
    print()

    if efficiency < 0.50:
        print("  *** OVERFITTING WARNING ***")
        print("  WFV efficiency ratio < 0.50.")
        print("  Optimised params do not generalise to forward periods.")
        print("  Consider: more training data, wider param ranges, fewer params.")
    elif efficiency < 0.70:
        print("  ~ Marginal generalisation (0.50-0.70). Monitor live performance.")
    else:
        print("  PASS: Strategy generalises well to unseen forward periods.")
    print()


def _save_report(results, efficiency, eff_pt, pass_rate,
                 split_cfg, n_windows, n_mc_sims, iw_train_pct, trade_amount) -> None:
    os.makedirs(_RESULTS_DIR, exist_ok=True)

    verdict = (
        "PASS"         if efficiency >= 0.70 else
        "MARGINAL"     if efficiency >= 0.50 else
        "WARN_OVERFIT"
    )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "n_windows":             n_windows,
            "in_window_train_pct":   iw_train_pct,
            "forward_pct":           round(1.0 - iw_train_pct, 2),
            "n_mc_sims_per_window":  n_mc_sims,
            "trade_amount_dollars":  trade_amount,
            "train_period":  f"{split_cfg['train_start_date']} → {split_cfg['train_end_date']}",
            "oos_locked":    f"{split_cfg['oos_start_date']} → {split_cfg['oos_end_date']}",
        },
        "summary": {
            "windows_completed":          len(results),
            "windows_passed":             sum(1 for r in results if r["pass_fail"] == "PASS"),
            "windows_failed":             sum(1 for r in results if r["pass_fail"] == "FAIL"),
            "pass_rate_pct":              pass_rate,
            "wfv_efficiency_ratio":       efficiency,
            "wfv_efficiency_per_trade":   eff_pt,
            "verdict":                    verdict,
        },
        "windows": results,
    }

    with open(WFV_REPORT_PATH, "w") as fh:
        json.dump(report, fh, indent=2)
    print(f"  Report saved : {WFV_REPORT_PATH}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Walk-Forward Validation — Kalshi BTC 15-minute strategy"
    )
    ap.add_argument("--windows",    type=int,   default=8,
                    help="Number of rolling WFV windows (default: 8)")
    ap.add_argument("--mc-sims",    type=int,   default=50,
                    help="Monte Carlo simulations per window (default: 50)")
    ap.add_argument("--amount",     type=float, default=5.0,
                    help="Trade amount in dollars (default: 5)")
    ap.add_argument("--start-year", type=int,   default=2020,
                    help="First data year to include (default: 2020)")
    args = ap.parse_args()

    run_walk_forward(
        n_windows    = args.windows,
        n_mc_sims    = args.mc_sims,
        trade_amount = args.amount,
        start_year   = args.start_year,
    )
