"""
Adapter wrapping the existing backtesting/walk_forward.py.

The existing walk_forward.py reads data/split_config.json and calls backtest.py.
This adapter wraps it and normalizes output to a per-fold metrics DataFrame.

# TODO: Reconcile field names with actual walk_forward.py output once
# data/split_config.json and backtest.py are available.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import pandas as pd


SCHEMA_COLUMNS = [
    "fold_id", "fold_type", "n_train", "n_test",
    "train_start", "train_end", "test_start", "test_end",
    "sharpe", "win_rate", "total_pnl", "n_trades",
]


def _normalize_wfv_report(report: dict) -> pd.DataFrame:
    """
    Convert walk_forward.py JSON output to standard per-fold schema.
    # TODO: Adjust field names to match actual walk_forward.py output.
    Expected keys from reading backtesting/walk_forward.py source:
        windows[].window_id, forward_pnl, forward_win_rate (if present)
    """
    rows = []
    for w in report.get("windows", []):
        rows.append({
            "fold_id":    w.get("window_id", 0),
            "fold_type":  "wfa",
            "n_train":    w.get("train_n", None),
            "n_test":     w.get("forward_n", None),
            "train_start": None,
            "train_end":   None,
            "test_start":  None,
            "test_end":    None,
            "sharpe":     None,
            "win_rate":   w.get("forward_win_rate", None),
            "total_pnl":  w.get("forward_pnl", None),
            "n_trades":   w.get("forward_n_trades", None),
        })
    return pd.DataFrame(rows, columns=SCHEMA_COLUMNS)


def run_wfa(config: dict, asset: str) -> pd.DataFrame:
    """
    Run the existing walk-forward validation for an asset.
    Returns per-fold metrics DataFrame in standard schema.
    # TODO: Reconcile once actual integration is confirmed.
    """
    wfv_path = os.path.join("backtesting", "walk_forward.py")
    if not os.path.exists(wfv_path):
        raise FileNotFoundError(f"walk_forward.py not found at {wfv_path}")

    wfv_cfg  = config.get("validation", {}).get("wfa", {})
    windows  = wfv_cfg.get("windows", 6)
    mc_sims  = wfv_cfg.get("mc_sims", 50)

    result = subprocess.run(
        [sys.executable, wfv_path, "--windows", str(windows), "--mc-sims", str(mc_sims)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"walk_forward.py failed:\n{result.stderr}")

    report_path = os.path.join("results", "wfv_report.json")
    if not os.path.exists(report_path):
        return pd.DataFrame(columns=SCHEMA_COLUMNS)

    with open(report_path) as f:
        report = json.load(f)

    return _normalize_wfv_report(report)
