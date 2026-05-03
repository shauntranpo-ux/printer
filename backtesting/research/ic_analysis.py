from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List
import numpy as np
from scipy import stats


@dataclass
class ICResult:
    ic: float
    icir: float
    t_stat: float
    ic_decay: List[float]   # IC at lags [1, 2, 4, 8]
    n_obs: int
    verdict: str            # PASS | CONDITIONAL | FAIL


def compute_ic(predicted: np.ndarray, outcomes: np.ndarray) -> float:
    """
    Information Coefficient: correlation between p_yes predictions and binary outcomes.
    Uses point-biserial correlation (Pearson r with binary dependent variable),
    which is the statistically appropriate choice when outcomes are {0, 1}.
    Point-biserial is mathematically equivalent to Spearman for binary outcomes
    at large N; at small N it is more sensitive.
    """
    if len(predicted) < 5:
        return 0.0
    r, _ = stats.pointbiserialr(outcomes, predicted)
    return float(r) if not np.isnan(r) else 0.0


def compute_icir(ic_series: np.ndarray) -> float:
    """ICIR = mean(IC) / std(IC). Returns 0 if std is zero."""
    if len(ic_series) < 2:
        return 0.0
    std = np.std(ic_series)
    return float(np.mean(ic_series) / std) if std > 0 else 0.0


def compute_ic_tstat(ic: float, n: int) -> float:
    """t-stat = IC * sqrt(N). Threshold > 2.0 indicates significance."""
    return float(ic * np.sqrt(n))


def compute_rolling_ic(
    predicted: np.ndarray,
    outcomes: np.ndarray,
    window: int = 30,
) -> np.ndarray:
    """Rolling IC computed over windows of size `window`."""
    if window < 1 or len(predicted) < window:
        return np.array([])
    ics = [
        compute_ic(predicted[i - window:i], outcomes[i - window:i])
        for i in range(window, len(predicted) + 1)
    ]
    return np.array(ics)


def evaluate_signal(
    predicted: np.ndarray,
    outcomes: np.ndarray,
    outcome_by_lag: Dict[int, np.ndarray],
    rolling_window: int = 30,
) -> ICResult:
    """Full IC evaluation for one signal against binary outcomes."""
    ic = compute_ic(predicted, outcomes)
    rolling_ics = compute_rolling_ic(predicted, outcomes, window=max(1, min(rolling_window, len(predicted) // 3)))
    icir = compute_icir(rolling_ics) if len(rolling_ics) > 0 else 0.0
    t_stat = compute_ic_tstat(ic, len(predicted))
    ic_decay = []
    for lag in [1, 2, 4, 8]:
        arr = outcome_by_lag.get(lag)
        if arr is None or len(arr) == 0:
            ic_decay.append(0.0)
        else:
            ic_decay.append(compute_ic(predicted[:len(arr)], arr))

    if t_stat > 2.0 and icir > 0.30:
        verdict = "PASS"
    elif t_stat > 1.5 or icir > 0.20:
        verdict = "CONDITIONAL"
    else:
        verdict = "FAIL"

    return ICResult(ic=ic, icir=icir, t_stat=t_stat, ic_decay=ic_decay,
                    n_obs=len(predicted), verdict=verdict)
