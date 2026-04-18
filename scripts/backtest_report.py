"""
Generate docs/backtest_report.md summarizing all backtest results.

Reads:
  results/wfv_report_v2.json
  results/holdout_report.json
  results/ablation_report.json
  results/stress_report.json

Usage:
    python scripts\\backtest_report.py
"""

from __future__ import annotations
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def load(path):
    p = Path(path)
    if not p.exists():
        return None
    with open(p) as f:
        return json.load(f)


def fmt(m):
    if not m or "error" in m:
        return f"ERROR: {m.get('error') if m else 'missing'}"
    return (f"trades={m.get('total_trades', 0):4d}  win={m.get('win_rate', 0):.3f}  "
            f"pnl=${m.get('total_pnl_dollars', 0):+.2f}  "
            f"avg=${m.get('avg_pnl_per_trade', 0):+.4f}  "
            f"sharpe={m.get('trade_level_sharpe', 0):.2f}  "
            f"brier={m.get('brier_score', 0):.3f}")


def main():
    wfv = load("results/wfv_report_v2.json")
    holdout = load("results/holdout_report.json")
    ablation = load("results/ablation_report.json")
    stress = load("results/stress_report.json")

    lines = []
    lines.append("# Backtest Report")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append("")
    lines.append("## Methodology")
    lines.append("")
    lines.append("- Data: 1-min OHLCV from Binance, 2020-present")
    lines.append("- Windows: 15-min binary contracts, strike = round-to-increment of window-open price")
    lines.append("- Fills: entry at simulated orderbook ask, conservative spread/noise model")
    lines.append("- Fees: exact Kalshi formula (ceil(0.07 * C * P * (1-P)) taker)")
    lines.append("- Calibration: isotonic regression fit on train, locked for test")
    lines.append("- Purge: 1-day gap between train and test to prevent window-boundary leakage")
    lines.append("")

    if wfv:
        lines.append("## Walk-forward results")
        lines.append("")
        cfg = wfv["config"]
        lines.append(f"- train days: {cfg['train_days']}  test days: {cfg['test_days']}  purge: {cfg['purge_days']}")
        lines.append(f"- period: {cfg['start']} to {cfg['end']}")
        lines.append("")
        for asset, rep in wfv["per_asset"].items():
            if "error" in rep:
                lines.append(f"- **{asset}**: ERROR: {rep['error']}")
            else:
                lines.append(f"- **{asset}**: {fmt(rep['overall_test'])}")
        lines.append("")

    if holdout:
        lines.append("## Out-of-sample holdout (the honest number)")
        lines.append("")
        lines.append(f"Holdout window: {holdout['config']['holdout_start']} to {holdout['config']['holdout_end']}")
        lines.append("")
        for asset, rep in holdout["per_asset"].items():
            if "error" in rep:
                lines.append(f"- **{asset}**: ERROR: {rep['error']}")
            else:
                m = rep["metrics"]
                lines.append(f"- **{asset}**: {fmt(m)}")
                lines.append(f"  - calibrated: {rep.get('calibrated', False)}  (train trades: {rep.get('train_trades', 0)})")
                lines.append(f"  - max_drawdown: ${m.get('max_drawdown_dollars', 0):.2f}")
        lines.append("")

    if ablation:
        lines.append("## Component ablation")
        lines.append("")
        lines.append("Positive `delta_pnl` means the signal contributes to P&L.")
        lines.append("")
        for asset, rep in ablation["per_asset"].items():
            lines.append(f"### {asset}")
            lines.append("")
            lines.append(f"Baseline: {fmt(rep.get('baseline', {}))}")
            lines.append("")
            lines.append("| signal | delta_pnl | delta_win_rate |")
            lines.append("|--------|-----------|----------------|")
            for sig, data in rep.get("ablations", {}).items():
                if "error" in data:
                    lines.append(f"| {sig} | ERROR | — |")
                else:
                    lines.append(f"| {sig} | {data['delta_pnl']:+.2f} | {data['delta_win_rate']:+.4f} |")
            lines.append("")

    if stress:
        lines.append("## Regime stress tests")
        lines.append("")
        for regime, per_asset in stress["per_regime"].items():
            lines.append(f"### {regime}")
            lines.append("")
            for asset, m in per_asset.items():
                if isinstance(m, dict) and m.get("status"):
                    lines.append(f"- **{asset}**: {m['status']}")
                elif isinstance(m, dict) and "error" in m:
                    lines.append(f"- **{asset}**: ERROR")
                else:
                    lines.append(f"- **{asset}**: {fmt(m)}")
            lines.append("")

    lines.append("## Go-live interpretation")
    lines.append("")
    lines.append("Before going live on any asset:")
    lines.append("")
    lines.append("- Holdout `avg_pnl_per_trade > 0`")
    lines.append("- Holdout `trade_level_sharpe > 0.5`")
    lines.append("- Holdout `max_drawdown` is survivable for your bankroll")
    lines.append("- Calibration was fittable (train_trades >= 50)")
    lines.append("- Ablation: no signal has materially negative delta_pnl")
    lines.append("- Stress: no catastrophic losses in any regime")
    lines.append("")
    lines.append("If ANY condition fails for an asset, do not go live on that asset.")
    lines.append("")

    Path("docs").mkdir(exist_ok=True)
    with open("docs/backtest_report.md", "w") as f:
        f.write("\n".join(lines))

    print("Wrote docs/backtest_report.md")


if __name__ == "__main__":
    main()
