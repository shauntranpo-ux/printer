"""
Backtesting CLI entry point.

Usage:
    python backtesting/cli.py <command> [options]

Commands:
    train       Run the training pipeline for one or more assets.
    backtest    Run the backtest engine for one asset and strategy.
    report      Generate reports for one asset.
    all         Run train → backtest → report for one asset.
    dry-run     Run a quick end-to-end test on synthetic data (no real data needed).

Hard constraints:
    - Sequential only: never async, never threading, never multiprocessing
    - Always load global config from backtesting/configs/backtest.yaml
"""
from __future__ import annotations

import argparse
import os
import sys

# Ensure project root is on sys.path so `backtesting.*` is importable
# when the script is run directly (python backtesting/cli.py …).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import yaml
import logging
import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "configs", "backtest.yaml")


def _load_global_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def cmd_train(args) -> None:
    """Run training pipeline."""
    config = _load_global_config()
    assets = [args.asset] if args.asset else None
    from backtesting.training.pipeline import run_training_pipeline
    summaries = run_training_pipeline(config, assets=assets)
    for asset, summary in summaries.items():
        logger.info(f"{asset.upper()}: {summary}")


def cmd_backtest(args) -> pd.DataFrame:
    """Run backtest engine and return trade log."""
    config = _load_global_config()
    output_dir = config.get("reports", {}).get("output_dir", "backtesting/output/reports")
    latency_ms = config.get("simulation", {}).get("latency_ms", 500.0)

    from backtesting.data.loaders import load_bars, load_kalshi_ticks
    from backtesting.data.label_builder import build_labels
    from backtesting.data.aligner import build_event_stream
    from backtesting.simulation.backtest_engine import run_backtest

    bars = load_bars(args.asset)
    labels = build_labels(bars)

    kalshi_ticks_df = None
    try:
        kalshi_ticks_df = load_kalshi_ticks(args.asset)
    except Exception:
        pass

    events = build_event_stream(bars, labels, kalshi_ticks=kalshi_ticks_df)

    trade_log = run_backtest(
        events, labels, args.asset,
        strategy=f"strategy_{args.strategy}",
        latency_ms=latency_ms,
    )
    logger.info(f"{args.asset.upper()}: {len(trade_log)} trades")
    return trade_log


def cmd_report(args, trade_log: pd.DataFrame | None = None) -> None:
    """Generate per-asset report. trade_log may be passed in from cmd_backtest."""
    config = _load_global_config()
    output_dir = config.get("reports", {}).get("output_dir", "backtesting/output/reports")

    from backtesting.reports.report_builder import build_asset_report, render_asset_report

    if trade_log is None:
        trade_log = pd.DataFrame()

    y_true = trade_log["label"].values if "label" in trade_log.columns and not trade_log.empty else np.array([])
    p_hat  = trade_log["p_model"].values if "p_model" in trade_log.columns and not trade_log.empty else np.array([])

    artifacts = build_asset_report(
        args.asset, trade_log, y_true, p_hat, output_dir=output_dir
    )
    render_asset_report(args.asset, artifacts, output_dir=output_dir)
    logger.info(f"Report written to {output_dir}/{args.asset}/")


def cmd_all(args) -> None:
    """Run train → backtest → report sequentially."""
    logger.info(f"Running full pipeline for {args.asset.upper()} strategy_{args.strategy}")
    cmd_train(args)
    trade_log = cmd_backtest(args)
    cmd_report(args, trade_log=trade_log)


def cmd_dry_run(args) -> None:
    """
    End-to-end dry run using synthetic data. No real data files needed.

    Generates 7 days of synthetic 10-second BTC bars, builds labels,
    runs the backtest engine (no fitted model; predict_proba returns 0.5 → no trades),
    and generates a report. Verifies the report artifact exists.

    This is the acceptance test for the entire backtesting pipeline.
    """
    logger.info(f"Starting dry run: asset={args.asset} strategy={args.strategy}")
    config = _load_global_config()
    output_dir = config.get("reports", {}).get("output_dir", "backtesting/output/reports")
    latency_ms = config.get("simulation", {}).get("latency_ms", 500.0)

    from backtesting.data.label_builder import build_labels
    from backtesting.data.aligner import build_event_stream
    from backtesting.simulation.backtest_engine import run_backtest
    from backtesting.reports.report_builder import build_asset_report, render_asset_report

    # Generate 7 days of synthetic 10-second bars
    n_bars = 7 * 24 * 360  # 7 days × 24h × 360 bars/h (10-second bars)
    rng = np.random.default_rng(42)

    start = pd.Timestamp("2024-01-01", tz="UTC")
    ts = pd.date_range(start, periods=n_bars, freq="10s", tz="UTC")

    price = 45000.0
    prices = [price]
    for _ in range(n_bars - 1):
        price *= np.exp(rng.normal(0, 0.0001))
        prices.append(price)
    prices = np.array(prices)

    bars = pd.DataFrame({
        "timestamp": ts,
        "open":      prices,
        "high":      prices * (1 + np.abs(rng.normal(0, 0.0002, n_bars))),
        "low":       prices * (1 - np.abs(rng.normal(0, 0.0002, n_bars))),
        "close":     prices * np.exp(rng.normal(0, 0.00005, n_bars)),
        "volume":    rng.exponential(1.0, n_bars),
    })

    labels = build_labels(bars)
    logger.info(f"Synthetic bars: {len(bars)}, labels: {len(labels)}")

    events = build_event_stream(bars, labels)

    trade_log = run_backtest(
        events, labels, args.asset,
        strategy=f"strategy_{args.strategy}",
        latency_ms=latency_ms,
    )
    logger.info(f"Trades: {len(trade_log)}")

    y_true = trade_log["label"].values if not trade_log.empty else np.array([])
    p_hat  = trade_log["p_model"].values if not trade_log.empty else np.array([])

    artifacts = build_asset_report(
        args.asset, trade_log, y_true, p_hat, output_dir=output_dir
    )
    render_asset_report(args.asset, artifacts, output_dir=output_dir)

    # Verify report artifact exists
    report_path = os.path.join(output_dir, args.asset, "report.md")
    assert os.path.exists(report_path), f"Report not found: {report_path}"

    # Print summary
    logger.info(f"Dry run PASSED. Report: {report_path}")
    logger.info(f"Artifacts: {list(artifacts.keys())}")

    # Print artifact sizes
    for name, path in artifacts.items():
        size = os.path.getsize(path) if os.path.exists(path) else 0
        logger.info(f"  {name}: {path} ({size} bytes)")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Kalshi 15-min crypto strategy backtesting CLI"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # train
    p_train = sub.add_parser("train", help="Run training pipeline")
    p_train.add_argument("--asset", default=None, help="Asset to train (default: all)")
    p_train.set_defaults(func=cmd_train)

    # backtest
    p_bt = sub.add_parser("backtest", help="Run backtest engine")
    p_bt.add_argument("--asset", required=True)
    p_bt.add_argument("--strategy", default="a", choices=["a", "b"])
    p_bt.set_defaults(func=lambda args: cmd_backtest(args))

    # report
    p_rpt = sub.add_parser("report", help="Generate reports")
    p_rpt.add_argument("--asset", required=True)
    p_rpt.add_argument("--strategy", default="a", choices=["a", "b"])
    p_rpt.set_defaults(func=lambda args: cmd_report(args))

    # all
    p_all = sub.add_parser("all", help="Run train → backtest → report")
    p_all.add_argument("--asset", required=True)
    p_all.add_argument("--strategy", default="a", choices=["a", "b"])
    p_all.set_defaults(func=cmd_all)

    # dry-run
    p_dry = sub.add_parser("dry-run", help="End-to-end dry run with synthetic data")
    p_dry.add_argument("--asset", default="btc")
    p_dry.add_argument("--strategy", default="a", choices=["a", "b"])
    p_dry.set_defaults(func=cmd_dry_run)

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
