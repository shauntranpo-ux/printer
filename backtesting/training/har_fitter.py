"""
HAR-RS-J coefficient fitter for the backtesting training pipeline.

Fits HAR-RS-J coefficients using OLS on historical log returns.
Writes results to a sidecar file: strategies/strategy_a/config/{asset}.fitted.yaml
Does NOT modify the original config under strategies/.

Reference: Patton & Sheppard (2015) — separate RV+/RV- for crypto.
"""
from __future__ import annotations
import os
import numpy as np
import pandas as pd
import yaml
from typing import Optional


_FITTED_CONFIG_DIR = os.path.join("strategies", "strategy_a", "config")
TIMESCALES_MINUTES = [15, 60, 240]


def _bipower_variation(log_returns: np.ndarray) -> float:
    """BV = (π/2) · Σ|r[1:]||r[:-1]|"""
    if len(log_returns) < 2:
        return 0.0
    return float((np.pi / 2.0) * np.sum(np.abs(log_returns[1:]) * np.abs(log_returns[:-1])))


def _rv_components(log_returns: np.ndarray) -> dict[str, float]:
    """Compute RV, RV+, RV-, BV, jump, signed_jump."""
    rv = float(np.sum(log_returns ** 2))
    rv_pos = float(np.sum(log_returns[log_returns > 0] ** 2))
    rv_neg = float(np.sum(log_returns[log_returns < 0] ** 2))
    bv = _bipower_variation(log_returns)
    jump = max(rv - bv, 0.0)
    signed_jump = rv_pos - rv_neg
    return {
        "rv": rv, "rv_pos": rv_pos, "rv_neg": rv_neg,
        "bv": bv, "jump": jump, "signed_jump": signed_jump,
    }


def _bars_in_window(granularity_seconds: int, window_minutes: int) -> int:
    return window_minutes * 60 // granularity_seconds


def build_har_features(
    log_returns: np.ndarray,
    granularity_seconds: int = 10,
    timescales_minutes: list[int] = TIMESCALES_MINUTES,
) -> pd.DataFrame:
    """
    Compute HAR-RS-J feature matrix from log returns.

    Each row is one 15-min window. Columns:
        rv_{t}_pos, rv_{t}_neg for each timescale t, jump_15m, rv_target
    """
    rows = []
    step = _bars_in_window(granularity_seconds, timescales_minutes[0])  # 15-min step
    max_lookback = _bars_in_window(granularity_seconds, max(timescales_minutes))

    for i in range(max_lookback, len(log_returns) - step + 1, step):
        window_log_rets = log_returns[i:i + step]
        target_rv = float(np.sum(window_log_rets ** 2))

        row: dict[str, float] = {"rv_target": target_rv}
        for t_min in timescales_minutes:
            n_bars = _bars_in_window(granularity_seconds, t_min)
            hist_rets = log_returns[i - n_bars:i]
            comps = _rv_components(hist_rets)
            # Use human-readable aliases so column names match fit_har_rsj return keys:
            #   15 → rv_15m, 60 → rv_1h, 240 → rv_4h
            _alias = {15: "rv_15m", 60: "rv_1h", 240: "rv_4h"}
            label = _alias.get(t_min, f"rv_{t_min}m")
            row[f"{label}_pos"] = comps["rv_pos"]
            row[f"{label}_neg"] = comps["rv_neg"]
        short_n = _bars_in_window(granularity_seconds, timescales_minutes[0])
        short_rets = log_returns[i - short_n:i]
        row["jump_15m"] = _rv_components(short_rets)["jump"]
        rows.append(row)

    return pd.DataFrame(rows)


def fit_har_rsj(
    log_returns: np.ndarray,
    granularity_seconds: int = 10,
    timescales_minutes: list[int] = TIMESCALES_MINUTES,
) -> dict[str, float | None]:
    """
    Fit HAR-RS-J coefficients via OLS.

    Returns dict with keys: const, rv_15m_pos, rv_15m_neg, rv_1h_pos, rv_1h_neg,
        rv_4h_pos, rv_4h_neg, jump
    Returns all-None if insufficient data (< 30 feature rows).
    """
    feat_df = build_har_features(log_returns, granularity_seconds, timescales_minutes)
    if len(feat_df) < 30:
        return {
            "const": None, "rv_15m_pos": None, "rv_15m_neg": None,
            "rv_1h_pos": None, "rv_1h_neg": None,
            "rv_4h_pos": None, "rv_4h_neg": None, "jump": None,
        }

    y = feat_df["rv_target"].values
    feature_cols = [c for c in feat_df.columns if c != "rv_target"]
    X_raw = feat_df[feature_cols].values
    X = np.column_stack([np.ones(len(X_raw)), X_raw])

    # OLS: β = (X'X)^-1 X'y
    # TODO: swap to WLS or Huber regression by replacing this block
    try:
        coeffs, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
    except np.linalg.LinAlgError:
        return {k: None for k in ["const", "rv_15m_pos", "rv_15m_neg",
                                   "rv_1h_pos", "rv_1h_neg", "rv_4h_pos",
                                   "rv_4h_neg", "jump"]}

    coeff_map: dict[str, float] = {"const": float(coeffs[0])}
    for name, val in zip(feature_cols, coeffs[1:]):
        coeff_map[name] = float(val)

    return {
        "const":      coeff_map.get("const"),
        "rv_15m_pos": coeff_map.get("rv_15m_pos"),
        "rv_15m_neg": coeff_map.get("rv_15m_neg"),
        "rv_1h_pos":  coeff_map.get("rv_1h_pos"),
        "rv_1h_neg":  coeff_map.get("rv_1h_neg"),
        "rv_4h_pos":  coeff_map.get("rv_4h_pos"),
        "rv_4h_neg":  coeff_map.get("rv_4h_neg"),
        "jump":       coeff_map.get("jump_15m"),
    }


def write_fitted_config(
    asset: str,
    coefficients: dict[str, float | None],
    extra_meta: dict | None = None,
    output_dir: str = _FITTED_CONFIG_DIR,
    suffix: str = ".fitted.yaml",
) -> str:
    """
    Write fitted HAR-RS-J coefficients to a sidecar YAML file.
    Does NOT touch the original strategies/strategy_a/config/{asset}.yaml.
    Returns the path written.
    """
    out_path = os.path.join(output_dir, f"{asset.lower()}{suffix}")
    data: dict = {
        "asset": asset.upper(),
        "fitted_by": "backtesting.training.har_fitter",
        "har_rs_j": {
            "coefficients": {
                k: (float(v) if v is not None else None)
                for k, v in coefficients.items()
            }
        },
    }
    if extra_meta:
        data["meta"] = extra_meta
    os.makedirs(output_dir, exist_ok=True)
    with open(out_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False)
    return out_path
