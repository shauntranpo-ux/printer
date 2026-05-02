"""
ETH strategy replay backtest — uses the live FifteenMinStrategy (D3 hybrid).

Instantiates FifteenMinStrategy(asset="ETH") and calls decide() on historical
1m data, producing results that reflect what the bot actually does live.

Outputs:
  backtesting/output/eth_replay_full.csv      — per-trade rows for all min_ev
  backtesting/output/eth_replay_baseline.csv  — baseline-only mode (signals zeroed)
  backtesting/output/ev_sweep_eth_replay_full.csv    — directional accuracy sweep
  backtesting/output/ev_sweep_eth_replay_baseline.csv
  backtesting/output/eth_calibration_rows.csv — (raw_p_yes, outcome) for calibrator refit

Usage:
  py scripts/run_eth_replay.py
  py scripts/run_eth_replay.py --start 2024-04-01 --end 2026-01-01
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import warnings; warnings.filterwarnings("ignore")
import argparse, math, time as _time
from collections import deque as _deque

import numpy as np
import pandas as pd

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backtesting", "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

LOAD_START = "2024-04-01"
LOAD_END   = "2026-04-01"
STEP       = 15          # 15m window
ENTRY_MIN  = 5           # entry at bar 5 → 10 min left
ETH_STRIKE_INC = 25.0   # Kalshi ETH strike increment
EV_VALUES  = [round(x / 100, 2) for x in range(4, 26)]  # 0.04 … 0.25


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _binary_price(cur: float, strike: float, rv: float, mins: float) -> float:
    """Black-Scholes binary call: P(price > strike at expiry)."""
    sigma = rv * math.sqrt(max(mins, 1e-6))
    if sigma < 1e-8 or strike <= 0 or cur <= 0:
        return 0.5
    return _norm_cdf(math.log(cur / strike) / sigma)


def _kalshi_fee(fill_cents: float) -> float:
    p = fill_cents / 100.0
    return math.ceil(0.07 * p * (1.0 - p) * 100) / 100


def _load_1m(asset: str, start: str, end: str) -> pd.DataFrame:
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", f"{asset}_1m.csv")
    print(f"  Loading {asset} 1m data …", end=" ", flush=True)
    df = pd.read_csv(path, usecols=["open_time", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["open_time"], utc=True)
    df = df[(df["ts"] >= start) & (df["ts"] < end)].reset_index(drop=True)
    df["ts_unix"] = (df["ts"] - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds()
    print(f"{len(df):,} rows ({df['ts'].iloc[0].date()} to {df['ts'].iloc[-1].date()})")
    return df


def _run_replay(eth: pd.DataFrame, btc: pd.DataFrame, baseline_only: bool) -> pd.DataFrame:
    from strategies.fifteen_min_strategy import FifteenMinStrategy
    from strategies.skip_layer import SkipConfig
    from strategies.features import MarketFeatures
    from strategies.baseline import brownian_bridge_prob_above

    label = "BASELINE-ONLY" if baseline_only else "FULL"
    print(f"\n  Running {label} mode …")

    skip_cfg = SkipConfig(
        min_entry_price_cents=5.0,
        max_entry_price_cents=95.0,
        cold_start_samples=60,
        vol_ratio_threshold=99.0,   # no vol gate — we gate with EV only
    )
    strat = FifteenMinStrategy(
        asset="ETH",
        skip_config=skip_cfg,
        min_ev=0.001,   # accept everything; we sweep min_ev in post-processing
        stake_dollars=25.0,
    )

    # Build BTC lookup by minute (ts_unix → close)
    btc_idx = btc.set_index("ts_unix")["close"]

    eth = eth.sort_values("ts_unix").reset_index(drop=True)
    n = len(eth)

    records = []
    skipped_cold = 0

    for start_idx in range(0, n - STEP + 1, STEP):
        window = eth.iloc[start_idx : start_idx + STEP]
        if len(window) < STEP:
            break

        entry_bar = window.iloc[ENTRY_MIN]
        exit_bar  = window.iloc[-1]
        entry_ts  = float(entry_bar["ts_unix"])
        entry_price = float(entry_bar["close"])
        exit_price  = float(exit_bar["close"])

        # Strike: nearest ETH_STRIKE_INC to the window-open price
        open_price = float(window.iloc[0]["close"])
        strike = round(open_price / ETH_STRIKE_INC) * ETH_STRIKE_INC
        if strike <= 0:
            continue

        mins_left = float(STEP - ENTRY_MIN)
        seconds_left = mins_left * 60.0

        # Historical price series: 60 bars ending at entry bar
        hist_start = max(0, start_idx + ENTRY_MIN - 60)
        hist_eth = eth.iloc[hist_start : start_idx + ENTRY_MIN]
        ts_arr  = hist_eth["ts_unix"].to_numpy(dtype=float)
        cl_arr  = hist_eth["close"].to_numpy(dtype=float)

        if len(cl_arr) < 10:
            skipped_cold += 1
            continue

        prices_60m  = list(zip(ts_arr, cl_arr))
        prices_1m   = prices_60m

        # BTC prices: align to same timestamp range
        btc_ts_lo = float(ts_arr[0]) - 60
        btc_slice_ts = btc_idx[(btc_idx.index >= btc_ts_lo) & (btc_idx.index <= entry_ts)]
        if len(btc_slice_ts) < 5:
            # Fall back to ETH price (correlation = 1, beta signal ~= 0 net)
            btc_prices_60m = [(t, entry_price * 100) for t in ts_arr]
        else:
            btc_prices_60m = list(zip(btc_slice_ts.index.tolist(), btc_slice_ts.tolist()))

        # Realized vol (1m log-returns std)
        if len(cl_arr) >= 2:
            rv_1min = float(np.std(np.log(np.maximum(cl_arr[1:], 1e-10) / np.maximum(cl_arr[:-1], 1e-10))))
        else:
            rv_1min = 0.002
        rv_1min = max(rv_1min, 0.0005)

        # Market price via binary option (same as run_ev_sweep.py)
        p_market = _binary_price(entry_price, strike, rv_1min, mins_left)
        yes_ask_c = round(p_market * 100, 1)
        no_ask_c  = round((1 - p_market) * 100, 1)
        yes_ask_c = max(5.0, min(95.0, yes_ask_c))
        no_ask_c  = max(5.0, min(95.0, no_ask_c))

        # Synthetic Kalshi price history (40 ticks at current market price)
        kalshi_hist = [(entry_ts - (40 - i) * 10, yes_ask_c) for i in range(40)]

        features = MarketFeatures(
            asset="ETH",
            ticker="KXETHD-BT",
            timestamp=entry_ts,
            current_price=entry_price,
            strike=strike,
            btc_price=float(btc_slice_ts.iloc[-1]) if len(btc_slice_ts) >= 1 else entry_price * 100,
            seconds_left=seconds_left,
            elapsed_seconds=float(ENTRY_MIN * 60),
            yes_ask=yes_ask_c,
            no_ask=no_ask_c,
            yes_bid=max(0.0, yes_ask_c - 1.0),
            no_bid=max(0.0, no_ask_c - 1.0),
            spread_yes=1.0,
            spread_no=1.0,
            realized_vol_1min=rv_1min,
        )
        features.prices_60m.extend(prices_60m)
        features.prices_1m.extend(prices_1m)
        features.btc_prices_60m.extend(btc_prices_60m)
        features.kalshi_price_history.extend(kalshi_hist)

        # ── Compute p_model ────────────────────────────────────────────────
        baseline_p = brownian_bridge_prob_above(
            entry_price, strike, seconds_left, rv_1min
        )

        if baseline_only:
            raw_p_yes = baseline_p
            calibrated_p_yes = baseline_p
            signals = {"baseline_p_above": baseline_p}
        else:
            _decision = strat.decide(features)
            _cs = _decision.contributing_signals or {}
            raw_p_yes = float(_cs.get("raw_p_yes", baseline_p))
            calibrated_p_yes = float(_cs.get("calibrated_p_yes", raw_p_yes))
            signals = _cs

        # Outcome
        outcome = 1 if exit_price > strike else 0

        records.append({
            "entry_ts":      entry_ts,
            "entry_price":   round(entry_price, 4),
            "strike":        strike,
            "exit_price":    round(exit_price, 4),
            "baseline_p":    round(baseline_p, 4),
            "raw_p_yes":     round(raw_p_yes, 4),
            "calibrated_p":  round(calibrated_p_yes, 4),
            "yes_ask_c":     yes_ask_c,
            "no_ask_c":      no_ask_c,
            "p_market":      round(p_market, 4),
            "rv_1min":       round(rv_1min, 6),
            "mins_left":     mins_left,
            "outcome":       outcome,       # 1 = closed above strike
            "total_signal":  round(
                abs(signals.get("beta_adj", 0)) +
                abs(signals.get("regime_adj", 0)) +
                abs(signals.get("ratio_adj", 0)) +
                abs(signals.get("velocity_adj", 0)), 4
            ),
        })

    if skipped_cold > 0:
        print(f"    Skipped {skipped_cold:,} windows (insufficient history).")

    df = pd.DataFrame(records)
    print(f"    {len(df):,} windows evaluated.")
    return df


def _apply_signal_sweep(df: pd.DataFrame) -> pd.DataFrame:
    """
    Signal accuracy sweep: for each signal-strength threshold, measure how often
    the model's directional call (p_yes > 0.5 = predict YES/above, < 0.5 = NO/below)
    matches the actual outcome.

    This sidesteps AMM pricing (we can't replicate Kalshi prices in backtest).
    What we CAN measure: does the signal-adjusted p_model correctly predict direction
    better than the baseline alone?

    Thresholds: |p_model - 0.5| >= threshold (confidence in the model's call).
    """
    thresholds = [round(x / 100, 2) for x in range(1, 45, 2)]  # 0.01 to 0.43
    rows = []
    for thr in thresholds:
        sub = df[abs(df["calibrated_p"] - 0.5) >= thr].copy()
        if len(sub) < 20:
            break
        sub["model_yes"] = sub["calibrated_p"] >= 0.5
        sub["correct"] = sub["model_yes"] == (sub["outcome"] == 1)
        rows.append({
            "threshold":    thr,
            "n":            len(sub),
            "accuracy":     round(sub["correct"].mean(), 4),
            "pct_yes_call": round(sub["model_yes"].mean(), 4),
        })
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=LOAD_START)
    ap.add_argument("--end",   default=LOAD_END)
    args = ap.parse_args()

    print(f"\n=== ETH Replay Backtest ({args.start} to {args.end}) ===\n")

    eth = _load_1m("ETH", args.start, args.end)
    btc = _load_1m("BTC", args.start, args.end)

    # Fix 1 — baseline audit
    df_base = _run_replay(eth, btc, baseline_only=True)
    df_base.to_csv(os.path.join(OUTPUT_DIR, "eth_replay_baseline.csv"), index=False)
    sweep_base = _apply_signal_sweep(df_base)
    sweep_base.to_csv(os.path.join(OUTPUT_DIR, "ev_sweep_eth_replay_baseline.csv"), index=False)

    # Full signals
    df_full = _run_replay(eth, btc, baseline_only=False)
    df_full.to_csv(os.path.join(OUTPUT_DIR, "eth_replay_full.csv"), index=False)
    sweep_full = _apply_signal_sweep(df_full)
    sweep_full.to_csv(os.path.join(OUTPUT_DIR, "ev_sweep_eth_replay_full.csv"), index=False)

    # Fix 2 — calibration rows (raw_p_yes, outcome) from full-signal run
    cal_rows = df_full[["raw_p_yes", "outcome"]].rename(columns={"outcome": "label"})
    cal_rows.to_csv(os.path.join(OUTPUT_DIR, "eth_calibration_rows.csv"), index=False)
    print(f"\n  Calibration rows saved: {len(cal_rows):,} rows.")

    # Side-by-side comparison
    print("\n=== Fix 1: Baseline vs Full Signals (directional accuracy) ===")
    print()
    print(f"  threshold = |p_model - 0.5|; accuracy = fraction the model called direction correctly")
    print()
    print(f"{'thresh':>7} | {'n_base':>7} {'acc_base':>9} | {'n_full':>7} {'acc_full':>9} | delta")
    print("-" * 55)
    key_thr = [0.01, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30]
    for thr in key_thr:
        b = sweep_base[sweep_base["threshold"] == thr]
        f = sweep_full[sweep_full["threshold"] == thr]
        bn  = int(b["n"].iloc[0]) if len(b) else 0
        bacc = f"{b['accuracy'].iloc[0]:.1%}" if len(b) else "-"
        fn  = int(f["n"].iloc[0]) if len(f) else 0
        facc = f"{f['accuracy'].iloc[0]:.1%}" if len(f) else "-"
        if len(b) and len(f):
            delta = f"+{f['accuracy'].iloc[0] - b['accuracy'].iloc[0]:.1%}"
        else:
            delta = "-"
        print(f"{thr:>7.2f} | {bn:>7} {bacc:>9} | {fn:>7} {facc:>9} | {delta}")


if __name__ == "__main__":
    main()
