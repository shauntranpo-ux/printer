"""
Calibration metrics for binary probability forecasters.

For binary markets, calibration matters more than raw accuracy.
A model outputting P(up)=0.70 should win 70% of the time.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Optional


def brier_score(y_true: np.ndarray, p_hat: np.ndarray) -> float:
    """Brier score = mean((p_hat - y_true)^2). Smaller is better. Perfect=0."""
    return float(np.mean((p_hat - y_true) ** 2))


def log_loss_score(y_true: np.ndarray, p_hat: np.ndarray, eps: float = 1e-7) -> float:
    """Binary log loss = -mean(y*log(p) + (1-y)*log(1-p))."""
    p = np.clip(p_hat, eps, 1.0 - eps)
    return float(-np.mean(y_true * np.log(p) + (1 - y_true) * np.log(1 - p)))


def expected_calibration_error(
    y_true: np.ndarray,
    p_hat: np.ndarray,
    n_bins: int = 10,
) -> float:
    """
    Expected Calibration Error (ECE).
    ECE = Σ_b (|B_b| / n) · |acc(B_b) - conf(B_b)|
    """
    bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
    n = len(y_true)
    ece = 0.0
    for i in range(n_bins):
        lo, hi = bin_edges[i], bin_edges[i + 1]
        if i < n_bins - 1:
            mask = (p_hat >= lo) & (p_hat < hi)
        else:
            mask = (p_hat >= lo) & (p_hat <= hi)
        if mask.sum() == 0:
            continue
        acc  = float(y_true[mask].mean())
        conf = float(p_hat[mask].mean())
        ece += (mask.sum() / n) * abs(acc - conf)
    return ece


def reliability_diagram_data(
    y_true: np.ndarray,
    p_hat: np.ndarray,
    n_bins: int = 10,
) -> pd.DataFrame:
    """Per-bin data for a reliability diagram."""
    bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
    rows = []
    for i in range(n_bins):
        lo, hi = bin_edges[i], bin_edges[i + 1]
        mask = (p_hat >= lo) & (p_hat < hi) if i < n_bins - 1 else (p_hat >= lo) & (p_hat <= hi)
        count = int(mask.sum())
        rows.append({
            "bin_lower":         round(lo, 4),
            "bin_upper":         round(hi, 4),
            "bin_center":        round((lo + hi) / 2, 4),
            "mean_predicted":    float(p_hat[mask].mean()) if count > 0 else float("nan"),
            "fraction_positive": float(y_true[mask].mean()) if count > 0 else float("nan"),
            "count":             count,
        })
    return pd.DataFrame(rows)


def calibration_summary(
    y_true: np.ndarray,
    p_hat: np.ndarray,
    n_bins: int = 10,
    regime: Optional[str] = None,
) -> dict:
    return {
        "regime":   regime or "all",
        "brier":    brier_score(y_true, p_hat),
        "log_loss": log_loss_score(y_true, p_hat),
        "ece":      expected_calibration_error(y_true, p_hat, n_bins),
        "n":        int(len(y_true)),
    }
