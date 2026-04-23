"""
Component ablation study.

For each asset, disable each strategy signal in turn and re-run a
single-period backtest. Compare per-component P&L impact.

Ablation method: set the magnitude constant to 0 before instantiating
the strategy. This disables a signal contribution without changing
its code path.

Output: results/ablation_report.json

Usage:
    python scripts\\backtest_ablation.py --start 2024-03-01 --end 2024-09-01
"""

from __future__ import annotations
import argparse
import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# (asset, signal_name, module_path, magnitude_const_name)
ABLATION_TARGETS = [
    ("ETH",  "beta_adj",       "strategies.eth_strategy",  "BETA_ADJ_MAX"),
    ("ETH",  "regime_adj",     "strategies.eth_strategy",  "REGIME_ADJ"),
    ("ETH",  "ratio_adj",      "strategies.eth_strategy",  "RATIO_ADJ_MAX"),
    ("ETH",  "velocity_adj",   "strategies.eth_strategy",  "VELOCITY_ADJ"),
    ("SOL",  "beta_adj",       "strategies.sol_strategy",  "BETA_ADJ_MAX"),
    ("SOL",  "momentum_bias",  "strategies.sol_strategy",  "MOMENTUM_BIAS"),
    ("SOL",  "regime_adj",     "strategies.sol_strategy",  "REGIME_ADJ"),
    ("SOL",  "velocity_adj",   "strategies.sol_strategy",  "VELOCITY_ADJ"),
    ("SOL",  "exhaustion",     "strategies.sol_strategy",  "EXHAUSTION_ADJ"),
    ("XRP",  "beta_adj_cap",   "strategies.xrp_strategy",  "BETA_ADJ_MAX_CAP"),
    ("XRP",  "regime_adj",     "strategies.xrp_strategy",  "REGIME_ADJ"),
    ("XRP",  "news_mode",      "strategies.xrp_strategy",  "NEWS_MODE_CONTINUATION_ADJ"),
    ("XRP",  "ratio_adj",      "strategies.xrp_strategy",  "RATIO_ADJ_MAX"),
    ("XRP",  "velocity_adj",   "strategies.xrp_strategy",  "VELOCITY_ADJ"),


]


def _run_slice(asset: str, start: datetime, end: datetime, seed: int) -> list:
    from scripts.backtest_walk_forward import make_strategy, load_prices, load_btc_prices, slice_events
    from strategies.backtest.runner import run_backtest
    df = load_prices(asset)
    btc_df = load_btc_prices()
    events = slice_events(asset, df, start.timestamp(), end.timestamp(), seed)
    strat = make_strategy(asset, calibrator=None)
    return run_backtest(strat, events, stake_dollars=5.0, btc_prices_df=btc_df)


def ablate_and_run(asset: str, module_path: str, const_name: str,
                   start: datetime, end: datetime, seed: int) -> dict:
    from scripts.backtest_walk_forward import compute_metrics
    module = importlib.import_module(module_path)
    original = getattr(module, const_name)
    setattr(module, const_name, 0.0)
    try:
        trades = _run_slice(asset, start, end, seed)
        return compute_metrics(trades, total_windows=max(1, len(trades)))
    finally:
        setattr(module, const_name, original)


def run_baseline(asset: str, start: datetime, end: datetime, seed: int) -> dict:
    from scripts.backtest_walk_forward import compute_metrics
    trades = _run_slice(asset, start, end, seed)
    return compute_metrics(trades, total_windows=max(1, len(trades)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2024-03-01")
    parser.add_argument("--end",   default="2024-09-01")
    parser.add_argument("--seed",  type=int, default=42)
    args = parser.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {"start": args.start, "end": args.end, "seed": args.seed},
        "per_asset": {},
    }

    assets = sorted(set(a for a, _, _, _ in ABLATION_TARGETS))
    for asset in assets:
        print(f"\n== {asset} ==")
        try:
            baseline = run_baseline(asset, start, end, args.seed)
            per_signal = {}
            for (a, sig, mod_path, const_name) in ABLATION_TARGETS:
                if a != asset:
                    continue
                print(f"  ablating {sig}...")
                try:
                    m = ablate_and_run(asset, mod_path, const_name, start, end, args.seed)
                    delta_pnl = baseline["total_pnl_dollars"] - m["total_pnl_dollars"]
                    delta_win = baseline["win_rate"] - m["win_rate"]
                    per_signal[sig] = {
                        "ablated_metrics": m,
                        "delta_pnl":       round(delta_pnl, 2),
                        "delta_win_rate":  round(delta_win, 4),
                    }
                    print(f"    delta_pnl={delta_pnl:+.2f} delta_win={delta_win:+.4f}")
                except Exception as e:
                    per_signal[sig] = {"error": str(e)}
            results["per_asset"][asset] = {"baseline": baseline, "ablations": per_signal}
        except Exception as e:
            import traceback
            results["per_asset"][asset] = {"error": str(e), "traceback": traceback.format_exc()}

    Path("results").mkdir(exist_ok=True)
    with open("results/ablation_report.json", "w") as f:
        json.dump(results, f, indent=2, default=str)

    print("\n== ABLATION SUMMARY (delta_pnl: positive = signal helps) ==")
    for asset, rep in results["per_asset"].items():
        if "error" in rep:
            print(f"{asset}: ERROR")
            continue
        print(f"\n{asset}:")
        for sig, data in rep["ablations"].items():
            if "error" in data:
                print(f"  {sig:22s} ERROR")
            else:
                print(f"  {sig:22s} delta_pnl={data['delta_pnl']:+7.2f}  "
                      f"delta_win={data['delta_win_rate']:+.4f}")

    print("\nFull report: results/ablation_report.json")


if __name__ == "__main__":
    main()

