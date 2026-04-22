"""
Adapter wrapping the existing backtesting/stress_test.py.

# TODO: Reconcile field names with actual stress_test.py output.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import pandas as pd


SCHEMA_COLUMNS = [
    "iter_id", "slippage_bps", "latency_ms",
    "total_pnl", "win_rate", "sharpe", "n_trades",
]


def _normalize_mc_report(report: dict) -> pd.DataFrame:
    """
    Convert stress_test.py JSON output to standard per-iteration schema.
    # TODO: Adjust field names when actual stress_test.py output is inspected.
    """
    rows = []
    for i, it in enumerate(report.get("iterations", [])):
        rows.append({
            "iter_id":     i,
            "slippage_bps": it.get("slippage_bps", None),
            "latency_ms":   it.get("latency_ms", None),
            "total_pnl":    it.get("total_pnl", None),
            "win_rate":     it.get("win_rate", None),
            "sharpe":       None,  # stress_test.py doesn't compute Sharpe directly
            "n_trades":     it.get("n_trades", None),
        })
    return pd.DataFrame(rows, columns=SCHEMA_COLUMNS)


def run_monte_carlo(config: dict, asset: str) -> pd.DataFrame:
    """
    Run stress_test.py as a subprocess and return normalized output.
    # TODO: Reconcile once actual integration is confirmed.
    """
    st_path = os.path.join("backtesting", "stress_test.py")
    if not os.path.exists(st_path):
        raise FileNotFoundError(f"stress_test.py not found at {st_path}")

    mc_cfg   = config.get("validation", {}).get("monte_carlo", {})
    iters    = mc_cfg.get("iterations", 200)
    max_slip = mc_cfg.get("max_slippage_bps", 20)

    result = subprocess.run(
        [sys.executable, st_path,
         "--st-iters", str(iters), "--st-max-slippage", str(max_slip)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"stress_test.py failed:\n{result.stderr}")

    report_path = os.path.join("results", "stress_test_report.json")
    if not os.path.exists(report_path):
        return pd.DataFrame(columns=SCHEMA_COLUMNS)

    with open(report_path) as f:
        report = json.load(f)

    return _normalize_mc_report(report)
