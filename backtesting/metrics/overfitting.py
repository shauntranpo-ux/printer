"""
Overfitting detection metrics.

1. Deflated Sharpe Ratio (DSR) — Bailey & Lopez de Prado (2014)
2. Probability of Backtest Overfitting (PBO) — Bailey et al. (2017)
3. Probabilistic Sharpe Ratio (PSR) — Lopez de Prado & Bailey (2012)
"""
from __future__ import annotations
import math
import numpy as np
from scipy.stats import norm


def deflated_sharpe_ratio(
    sharpe_obs: float,
    n_trials: int,
    n_obs: int,
    skew: float = 0.0,
    kurt: float = 3.0,
) -> tuple[float, float]:
    """
    Deflated Sharpe Ratio (DSR).

    Returns (dsr, p_value).
    Reference: Bailey & Lopez de Prado (2014), Eq. (8).
    """
    if n_obs < 2 or n_trials < 1:
        return float("nan"), float("nan")

    euler_mascheroni = 0.5772156649
    sr_star = (
        (1.0 - euler_mascheroni) * norm.ppf(1.0 - 1.0 / n_trials)
        + euler_mascheroni * norm.ppf(1.0 - 1.0 / (n_trials * math.e))
    )

    denom = 1.0 - skew * sharpe_obs + (kurt - 1.0) / 4.0 * sharpe_obs ** 2
    if denom <= 0:
        denom = 1e-9
    z = (sharpe_obs - sr_star) * np.sqrt(n_obs - 1) / np.sqrt(denom)
    dsr = float(norm.cdf(z))
    p_value = float(1.0 - dsr)
    return dsr, p_value


def probabilistic_sharpe_ratio(
    sharpe_obs: float,
    sharpe_benchmark: float,
    n_obs: int,
    skew: float = 0.0,
    kurt: float = 3.0,
) -> float:
    """
    Probabilistic Sharpe Ratio (PSR).
    P(SR > SR_benchmark | observed SR, moments).
    Reference: Lopez de Prado & Bailey (2012), Eq. (2).
    """
    if n_obs < 2:
        return float("nan")
    denom = 1.0 - skew * sharpe_obs + (kurt - 1.0) / 4.0 * sharpe_obs ** 2
    if denom <= 0:
        denom = 1e-9
    z = (sharpe_obs - sharpe_benchmark) * np.sqrt(n_obs - 1) / np.sqrt(denom)
    return float(norm.cdf(z))


def probability_of_backtest_overfitting(
    oos_sharpes: np.ndarray,
    is_sharpes: np.ndarray,
) -> float:
    """
    Probability of Backtest Overfitting (PBO).

    Estimates the probability that the in-sample best config
    performs below median OOS.

    Returns PBO in [0, 1]. Values > 0.5 indicate likely overfitting.
    """
    if len(oos_sharpes) != len(is_sharpes) or len(oos_sharpes) == 0:
        return float("nan")

    n = len(oos_sharpes)
    is_best_idx = int(np.argmax(is_sharpes))
    oos_best = oos_sharpes[is_best_idx]
    oos_median = float(np.median(oos_sharpes))

    sorted_oos = np.sort(oos_sharpes)
    omega = np.searchsorted(sorted_oos, oos_best) / n
    omega = np.clip(omega, 1e-4, 1.0 - 1e-4)
    log_odds = np.log(omega / (1.0 - omega))
    pbo_logistic = float(1.0 / (1.0 + np.exp(log_odds)))
    return pbo_logistic


def overfitting_summary(
    oos_sharpes: np.ndarray,
    is_sharpes: np.ndarray,
    n_trials: int,
    sharpe_benchmark: float = 0.0,
) -> dict:
    """Compute all three overfitting metrics."""
    if len(oos_sharpes) == 0:
        return {"dsr": float("nan"), "dsr_pvalue": float("nan"),
                "pbo": float("nan"), "psr": float("nan")}

    sharpe_obs = float(np.mean(oos_sharpes))
    n_obs = len(oos_sharpes)
    std = float(np.std(oos_sharpes))
    if std > 0:
        skew = float(np.mean((oos_sharpes - sharpe_obs) ** 3) / std ** 3)
        kurt = float(np.mean((oos_sharpes - sharpe_obs) ** 4) / std ** 4)
    else:
        skew, kurt = 0.0, 3.0

    dsr, dsr_pvalue = deflated_sharpe_ratio(sharpe_obs, n_trials, n_obs, skew, kurt)
    psr = probabilistic_sharpe_ratio(sharpe_obs, sharpe_benchmark, n_obs, skew, kurt)
    pbo = probability_of_backtest_overfitting(oos_sharpes, is_sharpes)

    return {
        "sharpe_mean_oos": sharpe_obs,
        "n_paths":         n_obs,
        "dsr":             dsr,
        "dsr_pvalue":      dsr_pvalue,
        "psr":             psr,
        "pbo":             pbo,
    }
