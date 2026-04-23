"""
Training pipeline orchestration.

Runs sequentially: BTC first (ETH/SOL/XRP cross-asset features depend on it),
then ETH, SOL, XRP. Never runs in parallel.

Entry point:
    from backtesting.training.pipeline import run_training_pipeline
    run_training_pipeline(global_config)
"""
from __future__ import annotations
import logging
import os
import sys
import numpy as np
import yaml
from typing import Optional

logger = logging.getLogger(__name__)
_ASSET_ORDER = ["btc", "eth", "sol", "xrp"]  # BTC must come first


def _detect_granularity(bars) -> int:
    """Detect bar granularity in seconds from median inter-bar interval."""
    if len(bars) < 2:
        return 10
    import pandas as pd
    ts = pd.to_datetime(bars["timestamp"])
    diffs = ts.sort_values().diff().dropna()
    return max(1, int(round(diffs.dt.total_seconds().median())))


def _load_yaml(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _build_feature_matrix(
    bars,
    asset: str,
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """
    Build (X, y, feature_names) from raw bar data.

    Features: HAR-RS-J variance components for now.
    # TODO: integrate order_flow, time_of_day, cross_asset, funding features
    # once data loaders for those sources are confirmed.
    """
    from backtesting.training.har_fitter import build_har_features
    from backtesting.data.label_builder import build_labels

    log_rets = np.log(
        bars["close"].clip(lower=1e-10) / bars["open"].clip(lower=1e-10)
    ).values

    granularity = _detect_granularity(bars)
    feat_df = build_har_features(log_rets, granularity_seconds=granularity)
    if feat_df.empty or len(feat_df) < 50:
        raise ValueError(f"[{asset}] Insufficient feature rows: {len(feat_df)}")

    labels_df = build_labels(bars)
    if labels_df.empty:
        raise ValueError(f"[{asset}] No labels built from bars.")

    n = min(len(feat_df), len(labels_df))
    feat_df = feat_df.iloc[-n:].reset_index(drop=True)
    labels_arr = labels_df["label"].values[-n:]

    feature_cols = [c for c in feat_df.columns if c != "rv_target"]
    X = feat_df[feature_cols].values.astype(float)
    y = labels_arr.astype(int)
    return X, y, feature_cols


def run_training_pipeline(
    global_config: dict,
    assets: Optional[list[str]] = None,
    per_asset_config_dir: str = "backtesting/configs/per_asset",
    fees_config_path: str = "strategies/shared/fees.yaml",
) -> dict[str, dict]:
    """
    Train models for all assets sequentially. Returns a dict of fit summaries.

    Args:
        global_config: contents of backtesting/configs/backtest.yaml
        assets: list of asset names to train; default is all 4 in BTC-first order
    """
    # Ensure strategies/ is importable
    strategies_path = os.path.abspath("strategies")
    if strategies_path not in sys.path:
        sys.path.insert(0, strategies_path)

    if assets is None:
        assets = list(_ASSET_ORDER)
    else:
        assets = sorted(
            [a.lower() for a in assets],
            key=lambda a: _ASSET_ORDER.index(a) if a in _ASSET_ORDER else 99
        )

    fees_config = _load_yaml(fees_config_path)
    train_cfg   = global_config.get("training", {})
    output_dir  = train_cfg.get("output_dir", "backtesting/output/models")
    refit       = train_cfg.get("refit", True)

    summaries: dict[str, dict] = {}

    for asset in assets:
        logger.info(f"Training asset: {asset.upper()}")

        # Load per-asset config
        per_asset_cfg_path = os.path.join(per_asset_config_dir, f"{asset.lower()}.yaml")
        if not os.path.exists(per_asset_cfg_path):
            logger.warning(f"[{asset}] Per-asset config not found: {per_asset_cfg_path}")
            summaries[asset] = {"status": "skipped", "reason": f"missing {per_asset_cfg_path}"}
            continue

        # Load bars
        try:
            from backtesting.data.loaders import load_bars
            data_cfg = global_config.get("data", {})
            bars = load_bars(
                asset=asset,
                start_date=data_cfg.get("start_date"),
                end_date=data_cfg.get("end_date"),
                check_min_history=True,
            )
        except (FileNotFoundError, ValueError) as exc:
            logger.warning(f"[{asset}] Skipping training — data not available: {exc}")
            summaries[asset] = {"status": "skipped", "reason": str(exc)}
            continue

        # Build feature matrix
        try:
            X, y, feature_names = _build_feature_matrix(bars, asset)
        except ValueError as exc:
            logger.warning(f"[{asset}] Feature build failed: {exc}")
            summaries[asset] = {"status": "skipped", "reason": str(exc)}
            continue

        # Load strategy A model config
        strategy_cfg_path = os.path.join(
            "strategies", "strategy_a", "config", f"{asset.lower()}.yaml"
        )
        if not os.path.exists(strategy_cfg_path):
            logger.warning(f"[{asset}] Strategy config not found: {strategy_cfg_path}")
            summaries[asset] = {"status": "skipped", "reason": f"missing {strategy_cfg_path}"}
            continue
        model_config = _load_yaml(strategy_cfg_path)

        # Fit and save
        from backtesting.training.model_fitter import fit_and_save
        try:
            wp, cp = fit_and_save(
                X=X, y=y, feature_names=feature_names,
                asset=asset, model_config=model_config, fees_config=fees_config,
                output_dir=output_dir, refit=refit,
            )
        except Exception as exc:
            logger.error(f"[{asset}] Fit failed: {exc}")
            summaries[asset] = {"status": "error", "reason": str(exc)}
            continue

        # HAR sidecar config
        from backtesting.training.har_fitter import fit_har_rsj, write_fitted_config
        log_rets = np.log(
            bars["close"].clip(lower=1e-10) / bars["open"].clip(lower=1e-10)
        ).values
        granularity_s = _detect_granularity(bars)

        # Check order_flow.enabled from sidecar config; log if disabled
        fitted_cfg_path = os.path.join(
            "strategies", "strategy_a", "config", f"{asset.lower()}.fitted.yaml"
        )
        if os.path.exists(fitted_cfg_path):
            fitted_cfg = _load_yaml(fitted_cfg_path)
            if not fitted_cfg.get("order_flow", {}).get("enabled", True):
                logger.info(
                    "[%s] order_flow features disabled (order_flow.enabled=false in sidecar config); "
                    "running on reduced feature set (HAR-RS-J only).",
                    asset.upper(),
                )

        coefficients = fit_har_rsj(log_rets, granularity_seconds=granularity_s)
        fitted_path = write_fitted_config(
            asset=asset,
            coefficients=coefficients,
            extra_meta={"n_training_bars": int(len(log_rets)), "n_feature_rows": int(len(X))},
            suffix=train_cfg.get("fitted_config_suffix", ".fitted.yaml"),
        )

        summaries[asset] = {
            "status": "ok",
            "n_samples": int(len(X)),
            "feature_count": int(len(feature_names)),
            "weights_path": wp,
            "calibrator_path": cp,
            "fitted_config_path": fitted_path,
            "label_balance": float(y.mean()),
        }
        logger.info(f"[{asset}] Training complete.")

    return summaries


_STRATEGY_C_ASSETS = ["btc", "eth"]


def run_strategy_c_training_pipeline(
    global_config: dict,
    assets: Optional[list[str]] = None,
    per_asset_config_dir: str = "backtesting/configs/per_asset",
) -> dict[str, dict]:
    """
    Fit Strategy C artifacts (HAR-RS-J, calibrators, sidecar config) for BTC and ETH.

    Requires Kalshi hourly ladder data at:
        data/kalshi/hourly/{ASSET_UPPERCASE}/

    Returns dict of fit summaries keyed by asset name.
    """
    strategies_path = os.path.abspath("strategies")
    if strategies_path not in sys.path:
        sys.path.insert(0, strategies_path)

    if assets is None:
        assets = list(_STRATEGY_C_ASSETS)
    else:
        assets = [a.lower() for a in assets if a.lower() in _STRATEGY_C_ASSETS]

    train_cfg = global_config.get("training", {})
    output_dir = train_cfg.get("output_dir", "backtesting/output/models")

    summaries: dict[str, dict] = {}

    for asset in assets:
        logger.info("[%s] Starting Strategy C training.", asset.upper())

        strategy_c_cfg_path = os.path.join(
            "strategies", "strategy_c", "config", f"{asset.lower()}.yaml"
        )
        if not os.path.exists(strategy_c_cfg_path):
            logger.warning("[%s] Strategy C config not found: %s", asset.upper(), strategy_c_cfg_path)
            summaries[asset] = {"status": "skipped", "reason": f"missing {strategy_c_cfg_path}"}
            continue

        strategy_c_config = _load_yaml(strategy_c_cfg_path)

        # Load underlying bars
        try:
            from backtesting.data.loaders import load_bars
            data_cfg = global_config.get("data", {})
            bars = load_bars(
                asset=asset,
                start_date=data_cfg.get("start_date"),
                end_date=data_cfg.get("end_date"),
                check_min_history=True,
            )
        except (FileNotFoundError, ValueError) as exc:
            logger.warning("[%s] Strategy C: skipping — bars unavailable: %s", asset.upper(), exc)
            summaries[asset] = {"status": "skipped", "reason": str(exc)}
            continue

        # Load ladder history
        try:
            from backtesting.data.loaders import load_strike_ladder_history
            data_cfg = global_config.get("data", {})
            ladder_df = load_strike_ladder_history(
                asset=asset,
                start_date=data_cfg.get("start_date"),
                end_date=data_cfg.get("end_date"),
            )
        except FileNotFoundError as exc:
            logger.warning("[%s] Strategy C: skipping — ladder data unavailable: %s", asset.upper(), exc)
            summaries[asset] = {"status": "skipped", "reason": str(exc)}
            continue

        # Build strike-ladder labels
        try:
            from backtesting.data.label_builder import build_strike_ladder_labels
            labels_df = build_strike_ladder_labels(bars, ladder_df)
        except Exception as exc:
            logger.warning("[%s] Strategy C: label build failed: %s", asset.upper(), exc)
            summaries[asset] = {"status": "error", "reason": str(exc)}
            continue

        # Fit
        from backtesting.training.strategy_c_fitter import fit_strategy_c
        try:
            result = fit_strategy_c(
                asset=asset,
                underlying_bars=bars,
                ladder_df=ladder_df,
                labels_df=labels_df,
                config=strategy_c_config,
                output_dir=output_dir,
            )
        except Exception as exc:
            logger.error("[%s] Strategy C fit failed: %s", asset.upper(), exc)
            summaries[asset] = {"status": "error", "reason": str(exc)}
            continue

        summaries[asset] = result
        logger.info("[%s] Strategy C training complete.", asset.upper())

    return summaries
