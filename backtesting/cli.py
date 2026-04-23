"""
Backtesting CLI entry point.

Usage:
    python backtesting/cli.py <command> [options]

Commands:
    train       Run the training pipeline for one or more assets.
    validate    Run validation (CPCV, WFA, MC) for one or more assets.
    backtest    Run the backtest engine for one asset and strategy.
    report      Generate reports for one asset.
    all         Run train -> validate -> backtest -> report for one asset.
    dry-run     Run a quick end-to-end test on synthetic data (no real data needed).

Hard constraints:
    - Sequential only: never async, never threading, never multiprocessing
    - Always load global config from backtesting/configs/backtest.yaml
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from types import SimpleNamespace

# Ensure project root is on sys.path so `backtesting.*` is importable
# when the script is run directly (python backtesting/cli.py ...).
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


def _detect_granularity(bars: pd.DataFrame) -> int:
    """Detect bar granularity in seconds from median inter-bar interval."""
    if len(bars) < 2:
        return 10
    ts = pd.to_datetime(bars["timestamp"])
    diffs = ts.sort_values().diff().dropna()
    return max(1, int(round(diffs.dt.total_seconds().median())))


def cmd_train(args) -> None:
    """Run training pipeline."""
    config = _load_global_config()
    assets = [args.asset] if args.asset else None
    strategy = getattr(args, "strategy", "both")

    if strategy == "c":
        from backtesting.training.pipeline import run_strategy_c_training_pipeline
        summaries = run_strategy_c_training_pipeline(config, assets=assets)
    else:
        from backtesting.training.pipeline import run_training_pipeline
        summaries = run_training_pipeline(config, assets=assets)
    for asset, summary in summaries.items():
        logger.info(f"{asset.upper()}: {summary}")


def cmd_validate(args) -> None:
    """
    Run validation (CPCV, WFA, MC) for one or all assets.

    Outputs per-method JSON to backtesting/output/validation/{asset}/{strategy}/{method}.json
    """
    config_path = getattr(args, "config", None) or CONFIG_PATH
    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    all_assets = ["btc", "eth", "sol", "xrp"]
    assets_to_run = all_assets if args.asset == "all" else [args.asset]
    strategy_arg = getattr(args, "strategy", "both")

    if strategy_arg == "c":
        # Strategy C uses event-level CPCV only
        for asset in assets_to_run:
            if asset.lower() not in ("btc", "eth"):
                logger.warning("[%s] Strategy C only supports BTC and ETH; skipping.", asset.upper())
                continue
            _run_strategy_c_validation(config, asset)
        return

    strategies_to_run = ["a", "b"] if strategy_arg == "both" else [strategy_arg]
    methods_to_run = ["cpcv", "wfa", "mc"] if args.method == "all" else [args.method]

    for asset in assets_to_run:
        for strategy in strategies_to_run:
            for method in methods_to_run:
                _run_single_validation(config, asset, strategy, method)


def _run_strategy_c_validation(config: dict, asset: str) -> None:
    """Run Strategy C event-level CPCV and write JSON output."""
    output_dir = os.path.join("backtesting", "output", "validation", asset, "c")
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, "cpcv.json")

    from backtesting.data.loaders import load_bars, load_strike_ladder_history
    from backtesting.data.label_builder import build_strike_ladder_labels

    try:
        bars = load_bars(asset)
    except (FileNotFoundError, ValueError) as exc:
        logger.warning("[%s] Strategy C validation: skipping bars — %s", asset.upper(), exc)
        return

    try:
        ladder_df = load_strike_ladder_history(asset)
    except FileNotFoundError as exc:
        logger.warning("[%s] Strategy C validation: skipping ladder — %s", asset.upper(), exc)
        return

    labels_df = build_strike_ladder_labels(bars, ladder_df)
    if labels_df.empty:
        logger.warning("[%s] Strategy C validation: no labels built.", asset.upper())
        return

    strategy_c_cfg_path = os.path.join("strategies", "strategy_c", "config", f"{asset.lower()}.yaml")
    strategy_c_config: dict = {}
    if os.path.exists(strategy_c_cfg_path):
        with open(strategy_c_cfg_path, encoding="utf-8") as f:
            strategy_c_config = yaml.safe_load(f) or {}

    val_cfg = config.get("validation", {}).get("strategy_c", {}).get("cpcv", {})
    from backtesting.validation.strategy_c_cpcv_adapter import run_strategy_c_cpcv
    try:
        result_df = run_strategy_c_cpcv(
            ladder_df=ladder_df,
            labels_df=labels_df,
            underlying_bars=bars,
            strategy_c_config=strategy_c_config,
            n_groups=val_cfg.get("n_groups", 6),
            k_test_groups=val_cfg.get("k_test_groups", 2),
            embargo_hours=val_cfg.get("embargo_hours", 1),
        )
    except Exception as exc:
        logger.warning("[%s] Strategy C CPCV failed: %s", asset.upper(), exc)
        return

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result_df.to_dict("records"), f, indent=2, default=str)
    logger.info("[%s] Strategy C CPCV -> %s", asset.upper(), output_path)


def _run_single_validation(config: dict, asset: str, strategy: str, method: str) -> None:
    """Run one (asset, strategy, method) validation pass and write JSON output."""
    output_dir = os.path.join("backtesting", "output", "validation", asset, strategy)
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"{method}.json")

    if method == "cpcv":
        from backtesting.data.loaders import load_bars
        from backtesting.data.label_builder import build_labels
        from backtesting.training.har_fitter import build_har_features
        from backtesting.validation.cpcv import run_cpcv

        try:
            bars = load_bars(asset)
        except (FileNotFoundError, ValueError) as exc:
            logger.warning("[%s] Skipping CPCV — %s", asset.upper(), exc)
            return

        labels_df = build_labels(bars)
        if labels_df.empty:
            logger.warning("[%s] No labels built; skipping CPCV.", asset.upper())
            return

        log_rets = np.log(
            bars["close"].clip(lower=1e-10) / bars["open"].clip(lower=1e-10)
        ).values
        granularity = _detect_granularity(bars)
        feat_df = build_har_features(log_rets, granularity_seconds=granularity)
        if len(feat_df) < 50:
            logger.warning("[%s] Insufficient feature rows for CPCV.", asset.upper())
            return

        feature_cols = [c for c in feat_df.columns if c != "rv_target"]
        n = min(len(feat_df), len(labels_df))
        X = feat_df[feature_cols].values[-n:].astype(float)
        y = labels_df["label"].values[-n:]
        timestamps_idx = pd.DatetimeIndex(labels_df["timestamp"].values[-n:])

        strategy_cfg_path = os.path.join(
            "strategies", "strategy_a", "config", f"{asset.lower()}.yaml"
        )
        model_config: dict = {}
        if os.path.exists(strategy_cfg_path):
            with open(strategy_cfg_path, encoding="utf-8") as f:
                model_config = yaml.safe_load(f) or {}

        fees_cfg_path = os.path.join("strategies", "shared", "fees.yaml")
        fees_config: dict = {}
        if os.path.exists(fees_cfg_path):
            with open(fees_cfg_path, encoding="utf-8") as f:
                fees_config = yaml.safe_load(f) or {}

        val_cfg = config.get("validation", {}).get("cpcv", {})
        try:
            result_df = run_cpcv(
                timestamps=timestamps_idx,
                labels=y,
                features=X,
                feature_names=feature_cols,
                model_config=model_config,
                fees_config=fees_config,
                n_groups=val_cfg.get("n_groups", 6),
                k_test_groups=val_cfg.get("k_test_groups", 2),
                embargo_minutes=val_cfg.get("embargo_minutes", 30),
            )
        except Exception as exc:
            logger.warning("[%s] CPCV failed: %s", asset.upper(), exc)
            return

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result_df.to_dict("records"), f, indent=2, default=str)
        logger.info("[%s] CPCV complete -> %s", asset.upper(), output_path)

    elif method == "wfa":
        from backtesting.validation.wfa_adapter import run_wfa
        try:
            result_df = run_wfa(config, asset)
        except Exception as exc:
            logger.warning("[%s] WFA failed: %s", asset.upper(), exc)
            return
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result_df.to_dict("records"), f, indent=2, default=str)
        logger.info("[%s] WFA complete -> %s", asset.upper(), output_path)

    elif method == "mc":
        from backtesting.validation.monte_carlo_adapter import run_monte_carlo
        try:
            result_df = run_monte_carlo(config, asset)
        except Exception as exc:
            logger.warning("[%s] MC failed: %s", asset.upper(), exc)
            return
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result_df.to_dict("records"), f, indent=2, default=str)
        logger.info("[%s] MC complete -> %s", asset.upper(), output_path)


def cmd_backtest_c(args) -> pd.DataFrame:
    """Run Strategy C backtest simulation and return trade log."""
    config = _load_global_config()

    asset = args.asset.lower()
    if asset not in ("btc", "eth"):
        logger.error("Strategy C only supports btc and eth; got '%s'.", asset)
        return pd.DataFrame()

    from backtesting.data.loaders import load_bars, load_strike_ladder_history
    from backtesting.data.label_builder import build_strike_ladder_labels
    from backtesting.simulation.strategy_c_adapter import run_strategy_c_backtest
    from backtesting.training.strategy_c_fitter import load_fitted_strategy_c

    data_cfg = config.get("data", {})
    try:
        bars = load_bars(asset, start_date=data_cfg.get("start_date"), end_date=data_cfg.get("end_date"))
    except (FileNotFoundError, ValueError) as exc:
        logger.error("[%s] Cannot load bars: %s", asset.upper(), exc)
        return pd.DataFrame()

    try:
        ladder_df = load_strike_ladder_history(asset)
    except FileNotFoundError as exc:
        logger.error("[%s] Cannot load ladder: %s", asset.upper(), exc)
        return pd.DataFrame()

    labels_df = build_strike_ladder_labels(bars, ladder_df)
    if labels_df.empty:
        logger.warning("[%s] No Strategy C labels built.", asset.upper())
        return pd.DataFrame()

    strategy_c_cfg_path = os.path.join("strategies", "strategy_c", "config", f"{asset}.yaml")
    strategy_c_config: dict = {}
    if os.path.exists(strategy_c_cfg_path):
        with open(strategy_c_cfg_path, encoding="utf-8") as f:
            strategy_c_config = yaml.safe_load(f) or {}

    fitted_config = load_fitted_strategy_c(asset)

    sim_cfg = config.get("strategy_c", {}).get("simulation", {})
    run_c1 = sim_cfg.get("run_c1", True)
    run_c2 = sim_cfg.get("run_c2", True)
    max_pos = sim_cfg.get("max_positions_per_event", 2)

    trade_log = run_strategy_c_backtest(
        asset=asset,
        ladder_df=ladder_df,
        labels_df=labels_df,
        underlying_bars=bars,
        strategy_c_config=strategy_c_config,
        fitted_config=fitted_config,
        run_c1=run_c1,
        run_c2=run_c2,
        max_positions_per_event=max_pos,
    )

    logger.info("[%s] Strategy C: %d trades", asset.upper(), len(trade_log))
    trade_log_dir = os.path.join("backtesting", "output", "trade_logs")
    os.makedirs(trade_log_dir, exist_ok=True)
    out_path = os.path.join(trade_log_dir, f"{asset}_c.parquet")
    if not trade_log.empty:
        trade_log.to_parquet(out_path, index=False)
    return trade_log


def cmd_backtest(args) -> pd.DataFrame:
    """Run backtest engine and return trade log."""
    config = _load_global_config()
    output_dir = config.get("reports", {}).get("output_dir", "backtesting/output/reports")
    latency_ms = config.get("simulation", {}).get("latency_ms", 500.0)

    from backtesting.data.loaders import load_bars, load_kalshi_ticks
    from backtesting.data.label_builder import build_labels
    from backtesting.data.aligner import build_event_stream
    from backtesting.simulation.backtest_engine import run_backtest

    fees_cfg_path = os.path.join("strategies", "shared", "fees.yaml")
    fees_config: dict = {}
    if os.path.exists(fees_cfg_path):
        with open(fees_cfg_path, encoding="utf-8") as f:
            fees_config = yaml.safe_load(f) or {}

    model_a = None
    model_b = None
    model_config: dict = {}

    if args.strategy == "a":
        strategy_cfg_path = os.path.join(
            "strategies", "strategy_a", "config", f"{args.asset.lower()}.yaml"
        )
        if os.path.exists(strategy_cfg_path):
            with open(strategy_cfg_path, encoding="utf-8") as f:
                model_config = yaml.safe_load(f) or {}
        from backtesting.training.model_fitter import load_model
        try:
            model_a = load_model(args.asset, model_config, fees_config)
        except Exception as exc:
            logger.warning("[%s] Could not load Strategy A model: %s", args.asset.upper(), exc)

    elif args.strategy == "b":
        strategy_cfg_path = os.path.join(
            "strategies", "strategy_b", "config", f"{args.asset.lower()}.yaml"
        )
        if os.path.exists(strategy_cfg_path):
            with open(strategy_cfg_path, encoding="utf-8") as f:
                model_config = yaml.safe_load(f) or {}
        _strat_path = os.path.join(_PROJECT_ROOT, "strategies")
        if _strat_path not in sys.path:
            sys.path.insert(0, _strat_path)
        from strategy_b.contract_dislocation import ContractDislocationDetector
        model_b = ContractDislocationDetector(model_config)

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
        model_a=model_a,
        model_b=model_b,
        model_config=model_config,
        fees_config=fees_config,
        latency_ms=latency_ms,
    )
    logger.info(f"{args.asset.upper()}: {len(trade_log)} trades")
    trade_log_dir = os.path.join("backtesting", "output", "trade_logs")
    os.makedirs(trade_log_dir, exist_ok=True)
    trade_log.to_parquet(os.path.join(trade_log_dir, f"{args.asset}_{args.strategy}.parquet"), index=False)
    return trade_log


def cmd_report(args, trade_log: pd.DataFrame | None = None) -> None:
    """Generate per-asset report. trade_log may be passed in from cmd_backtest."""
    config = _load_global_config()
    output_dir = config.get("reports", {}).get("output_dir", "backtesting/output/reports")
    strategy = getattr(args, "strategy", "a")

    if strategy == "c":
        from backtesting.reports.report_builder import build_strategy_c_report, render_strategy_c_report
        if trade_log is None:
            tl_path = os.path.join("backtesting", "output", "trade_logs", f"{args.asset}_c.parquet")
            trade_log = pd.read_parquet(tl_path) if os.path.exists(tl_path) else pd.DataFrame()
        artifacts = build_strategy_c_report(args.asset, trade_log, output_dir=output_dir)
        render_strategy_c_report(args.asset, artifacts, output_dir=output_dir)
        logger.info(f"Strategy C report written to {output_dir}/{args.asset}/strategy_c_report.md")
        return

    from backtesting.reports.report_builder import build_asset_report, render_asset_report

    if trade_log is None:
        trade_log_path = os.path.join("backtesting", "output", "trade_logs", f"{args.asset}_{strategy}.parquet")
        trade_log = pd.read_parquet(trade_log_path) if os.path.exists(trade_log_path) else pd.DataFrame()

    y_true = trade_log["label"].values if "label" in trade_log.columns and not trade_log.empty else np.array([])
    p_hat  = trade_log["p_model"].values if "p_model" in trade_log.columns and not trade_log.empty else np.array([])

    artifacts = build_asset_report(
        args.asset, trade_log, y_true, p_hat, output_dir=output_dir
    )
    render_asset_report(args.asset, artifacts, output_dir=output_dir)
    logger.info(f"Report written to {output_dir}/{args.asset}/")


def cmd_all(args) -> None:
    """Run train -> validate -> backtest -> report sequentially."""
    strategy = getattr(args, "strategy", "a")
    logger.info(f"Running full pipeline for {args.asset.upper()} strategy_{strategy}")
    cmd_train(args)

    validate_args = SimpleNamespace(
        asset=args.asset,
        strategy=strategy,
        method="all",
        config=CONFIG_PATH,
    )
    cmd_validate(validate_args)

    if strategy == "c":
        trade_log = cmd_backtest_c(args)
    else:
        trade_log = cmd_backtest(args)
    cmd_report(args, trade_log=trade_log)


def cmd_dry_run(args) -> None:
    """
    End-to-end dry run using synthetic data. No real data files needed.

    Generates 7 days of synthetic 10-second BTC bars, builds labels,
    runs the backtest engine (no fitted model; predict_proba returns 0.5 -> no trades),
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
    n_bars = 7 * 24 * 360  # 7 days x 24h x 360 bars/h (10-second bars)
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
    p_train.add_argument(
        "--strategy", default="both", choices=["a", "b", "c", "both"],
        help="Strategy to train (default: both; 'c' trains Strategy C for BTC/ETH only)",
    )
    p_train.set_defaults(func=cmd_train)

    # validate
    p_val = sub.add_parser("validate", help="Run validation (CPCV, WFA, MC)")
    p_val.add_argument(
        "--asset", required=True,
        choices=["btc", "eth", "sol", "xrp", "all"],
        help="Asset to validate (use 'all' to iterate btc, eth, sol, xrp sequentially)",
    )
    p_val.add_argument(
        "--strategy", default="both", choices=["a", "b", "c", "both"],
        help="Strategy to validate (default: both; 'c' runs event-level CPCV for BTC/ETH)",
    )
    p_val.add_argument(
        "--method", default="all", choices=["cpcv", "wfa", "mc", "all"],
        help="Validation method for strategies a/b (default: all); ignored for strategy c",
    )
    p_val.add_argument(
        "--config", default=CONFIG_PATH,
        help=f"Path to backtest config YAML (default: {CONFIG_PATH})",
    )
    p_val.set_defaults(func=cmd_validate)

    # backtest
    p_bt = sub.add_parser("backtest", help="Run backtest engine")
    p_bt.add_argument("--asset", required=True)
    p_bt.add_argument("--strategy", default="a", choices=["a", "b", "c"])
    p_bt.set_defaults(func=lambda args: cmd_backtest_c(args) if args.strategy == "c" else cmd_backtest(args))

    # report
    p_rpt = sub.add_parser("report", help="Generate reports")
    p_rpt.add_argument("--asset", required=True)
    p_rpt.add_argument("--strategy", default="a", choices=["a", "b", "c"])
    p_rpt.set_defaults(func=lambda args: cmd_report(args))

    # all
    p_all = sub.add_parser("all", help="Run train -> validate -> backtest -> report")
    p_all.add_argument("--asset", required=True)
    p_all.add_argument("--strategy", default="a", choices=["a", "b", "c"])
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
