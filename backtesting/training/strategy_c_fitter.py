"""
Strategy C training pipeline — per-asset, sequential only.

Fits:
  1. HAR-RS-J coefficients (via har_fitter.py — reused)
  2. Drift-adjustment weight (mu_hat) from signed-jump OLS
  3. Per-moneyness calibrators: five IsotonicRegression / LogisticRegression
     calibrators (one per bucket) fit on raw N(d₂) vs. realized label.
  4. Regime × moneyness edge thresholds: placeholder only — NOT auto-tuned.

All artifacts are written under backtesting/output/models/ and referenced
in a sidecar config strategies/strategy_c/config/{asset}.fitted.yaml.
The original strategies/strategy_c/config/{asset}.yaml is NEVER modified.

BTC and ETH only. Do NOT call this for SOL or XRP.
"""
from __future__ import annotations
import logging
import math
import os
import pickle
import sys
from typing import Optional

import numpy as np
import pandas as pd
import yaml

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
_STRATEGIES_DIR = os.path.join(_PROJECT_ROOT, "strategies")
_STRATEGY_C_CONFIG_DIR = os.path.join(_STRATEGIES_DIR, "strategy_c", "config")

_MONEYNESS_BUCKETS = ["deep_itm", "itm", "atm", "otm", "deep_otm"]
_MIN_BUCKET_SAMPLES = 30


def _ensure_importable() -> None:
    if _STRATEGIES_DIR not in sys.path:
        sys.path.insert(0, _STRATEGIES_DIR)


def _load_yaml(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _detect_granularity(bars: pd.DataFrame) -> int:
    if len(bars) < 2:
        return 60
    diffs = bars["timestamp"].sort_values().diff().dropna()
    return max(1, int(round(diffs.dt.total_seconds().median())))


def _get_har_sigma(log_rets: np.ndarray, granularity_s: int) -> float:
    """Compute HAR-based annualized sigma from the most recent window of log returns."""
    from backtesting.training.har_fitter import _rv_components, _bars_in_window

    n_bars_4h = _bars_in_window(granularity_s, 240)
    window = log_rets[-n_bars_4h:] if len(log_rets) >= n_bars_4h else log_rets
    if len(window) < 2:
        return 0.5  # fallback annualized vol
    rv = _rv_components(window)["rv"]
    # rv is variance per granularity_s seconds; annualise to per-year
    periods_per_year = 365.25 * 24 * 3600 / granularity_s
    return float(math.sqrt(max(rv, 1e-12) * periods_per_year))


def fit_strategy_c(
    asset: str,
    underlying_bars: pd.DataFrame,
    ladder_df: pd.DataFrame,
    labels_df: pd.DataFrame,
    config: dict,
    output_dir: str = "backtesting/output/models",
) -> dict:
    """
    Fit all Strategy C artifacts for one asset.

    Args:
        asset:            "btc" or "eth"
        underlying_bars:  1-minute bar DataFrame with UTC timestamps
        ladder_df:        output of load_strike_ladder_history()
        labels_df:        output of build_strike_ladder_labels()
        config:           contents of strategies/strategy_c/config/{asset}.yaml
        output_dir:       directory for fitted artifacts

    Returns:
        dict with status and artifact paths
    """
    _ensure_importable()

    if asset.lower() not in ("btc", "eth"):
        raise ValueError(f"Strategy C only supports BTC and ETH; got '{asset}'")

    os.makedirs(output_dir, exist_ok=True)

    # ── 1. HAR-RS-J coefficients ───────────────────────────────────────────
    from backtesting.training.har_fitter import fit_har_rsj

    granularity_s = _detect_granularity(underlying_bars)
    log_rets = np.log(
        underlying_bars["close"].clip(lower=1e-10)
        / underlying_bars["open"].clip(lower=1e-10)
    ).values

    har_coeffs = fit_har_rsj(log_rets, granularity_seconds=granularity_s)
    logger.info("[%s] HAR-RS-J fitted: const=%.6f", asset.upper(), har_coeffs.get("const", 0))

    # ── 2. Build (p_raw, moneyness_bucket, signed_jump, label) rows ────────
    from strategy_c.probability.digital_call import binary_call_probability
    from strategy_c.features.moneyness import compute_moneyness_features

    sigma_ref = float(
        config.get("volatility_reference", {}).get("annualized", 0.5)
    )
    rfr = float(config.get("probability", {}).get("risk_free_rate", 0.0))

    # Build a timestamp → log_return lookup for jump signals
    bars_sorted = underlying_bars.sort_values("timestamp").reset_index(drop=True)
    bars_sorted["log_ret"] = np.log(
        bars_sorted["close"].clip(lower=1e-10)
        / bars_sorted["open"].clip(lower=1e-10)
    )

    # Merge labels with ladder to get ladder snapshots at event entry
    # Use the earliest snapshot per event as the "entry" snapshot
    earliest_snap = (
        ladder_df.sort_values("timestamp")
        .groupby("event_id")["timestamp"]
        .first()
        .rename("entry_time")
    )
    labels_with_entry = labels_df.join(earliest_snap, on="event_id")

    records = []
    for _, row in labels_with_entry.iterrows():
        eid = row["event_id"]
        strike = float(row["strike"])
        label = int(row["label"])
        event_close = row["event_close_time"]
        entry_time = row.get("entry_time")
        if pd.isna(entry_time):
            continue

        # Underlying spot at entry_time (backward fill)
        candidates = bars_sorted[bars_sorted["timestamp"] <= entry_time]
        if candidates.empty:
            continue
        spot = float(candidates["close"].iloc[-1])
        if spot <= 0:
            continue

        tte_s = (event_close - entry_time).total_seconds()
        if tte_s <= 0:
            continue

        # Use reference vol for integrated variance (calibrators correct residual bias)
        t_years = tte_s / (365.25 * 24 * 3600)
        iv = sigma_ref ** 2 * t_years

        p_raw = binary_call_probability(spot, strike, iv, tte_s, rfr)

        feat = compute_moneyness_features(spot, strike, sigma_ref, tte_s, config)
        bucket = feat["moneyness_bucket"]

        # Signed jump: sum of positive semi-variance in recent 15m window
        n_bars_15m = max(1, 15 * 60 // granularity_s)
        recent = candidates["log_ret"].values[-n_bars_15m:]
        signed_jump = float(np.sum(recent[recent > 0] ** 2) - np.sum(recent[recent < 0] ** 2))

        records.append({
            "p_raw": p_raw,
            "moneyness_bucket": bucket,
            "signed_jump": signed_jump,
            "label": label,
        })

    if not records:
        raise ValueError(
            f"[{asset}] No calibration training rows built — "
            "check that ladder_df and underlying_bars overlap in time."
        )

    cal_df = pd.DataFrame(records)
    logger.info(
        "[%s] %d calibration rows built across %d events",
        asset.upper(), len(cal_df),
        labels_df["event_id"].nunique(),
    )

    # ── 3. Drift adjustment weight (mu_hat) via OLS ────────────────────────
    x_mu = cal_df["signed_jump"].values.astype(float)
    y_mu = cal_df["label"].astype(float).values - cal_df["p_raw"].values  # residual
    if len(x_mu) >= 10 and np.std(x_mu) > 1e-12:
        # Closed-form single-feature OLS: beta = (x·y) / (x·x)
        xx = float(np.dot(x_mu, x_mu))
        mu_weight = float(np.dot(x_mu, y_mu) / xx) if xx > 0.0 else 0.0
    else:
        mu_weight = 0.0
    logger.info("[%s] drift_adjustment_weight = %.6f", asset.upper(), mu_weight)

    # ── 4. Per-moneyness calibrators ───────────────────────────────────────
    from sklearn.isotonic import IsotonicRegression
    from sklearn.linear_model import LogisticRegression

    cal_cfg = config.get("calibration", {})
    per_bucket_type = cal_cfg.get("per_bucket", {})

    calibrator_paths: dict[str, Optional[str]] = {}

    for bucket in _MONEYNESS_BUCKETS:
        bucket_rows = cal_df[cal_df["moneyness_bucket"] == bucket]
        if len(bucket_rows) < _MIN_BUCKET_SAMPLES:
            logger.warning(
                "[%s] Bucket '%s' has only %d rows (< %d); skipping calibration for this bucket.",
                asset.upper(), bucket, len(bucket_rows), _MIN_BUCKET_SAMPLES,
            )
            calibrator_paths[bucket] = None
            continue

        p_raw_b = bucket_rows["p_raw"].values
        y_b = bucket_rows["label"].values.astype(int)

        cal_type = per_bucket_type.get(bucket, "isotonic")
        if cal_type == "isotonic":
            calibrator = IsotonicRegression(out_of_bounds="clip")
            calibrator.fit(p_raw_b, y_b)
        else:
            calibrator = LogisticRegression(max_iter=200)
            calibrator.fit(p_raw_b.reshape(-1, 1), y_b)

        fname = f"{asset.lower()}_calibrator_{bucket}.pkl"
        fpath = os.path.join(output_dir, fname)
        with open(fpath, "wb") as f:
            pickle.dump(calibrator, f)

        calibrator_paths[bucket] = fpath
        logger.info("[%s] Calibrator for '%s' saved to %s", asset.upper(), bucket, fpath)

    # ── 5. Write sidecar fitted.yaml ───────────────────────────────────────
    fitted_config = {
        "har_rs_j": {
            "coefficients": har_coeffs,
        },
        "probability": {
            "drift_adjustment_weight": mu_weight,
        },
        "calibration": {
            "artifact_paths": calibrator_paths,
        },
        "meta": {
            "n_calibration_rows": int(len(cal_df)),
            "n_events": int(labels_df["event_id"].nunique()),
            "granularity_seconds": int(granularity_s),
        },
    }

    fitted_path = os.path.join(_STRATEGY_C_CONFIG_DIR, f"{asset.lower()}.fitted.yaml")
    with open(fitted_path, "w", encoding="utf-8") as f:
        yaml.dump(fitted_config, f, default_flow_style=False, allow_unicode=True)

    logger.info("[%s] Fitted config written to %s", asset.upper(), fitted_path)

    return {
        "status": "ok",
        "fitted_config_path": fitted_path,
        "calibrator_paths": calibrator_paths,
        "n_calibration_rows": int(len(cal_df)),
        "drift_adjustment_weight": mu_weight,
    }


def load_fitted_strategy_c(asset: str) -> dict:
    """
    Load the fitted sidecar config for Strategy C.
    Returns empty dict if not yet fitted.
    """
    path = os.path.join(_STRATEGY_C_CONFIG_DIR, f"{asset.lower()}.fitted.yaml")
    if not os.path.exists(path):
        logger.warning("[%s] No fitted Strategy C config found at %s", asset.upper(), path)
        return {}
    return _load_yaml(path)
