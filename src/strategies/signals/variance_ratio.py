"""
Variance ratio test (Lo and MacKinlay 1988).

Tests whether returns follow a random walk. For a time series of returns
with q-period aggregation:

    VR(q) = Var(r_q) / (q * Var(r_1))

where r_q is the q-period return and r_1 is the 1-period return.

- VR > 1: positive autocorrelation (trending / momentum)
- VR < 1: negative autocorrelation (mean-reversion)
- VR = 1: random walk (no predictability)

For 1-min crypto returns over 60-minute windows, VR(5) is a standard choice
that captures whether short-term momentum is alive or dead.
"""

from __future__ import annotations
from typing import Optional


def variance_ratio(returns: list, q: int = 5) -> Optional[float]:
    """
    Args:
        returns: list of single-period returns (e.g. 1-min log returns)
        q: aggregation window (e.g. 5 for VR(5))

    Returns:
        VR(q), or None if inputs insufficient

    Requires at least q*10 observations for a meaningful estimate.
    """
    n = len(returns)
    if n < q * 10:
        return None

    # Variance of single-period returns
    mean_1 = sum(returns) / n
    var_1 = sum((r - mean_1) ** 2 for r in returns) / (n - 1)
    if var_1 <= 0:
        return None

    # q-period returns (non-overlapping for simpler independence assumptions)
    q_returns = []
    for i in range(0, n - q + 1, q):
        q_returns.append(sum(returns[i:i + q]))
    if len(q_returns) < 5:
        return None

    mean_q = sum(q_returns) / len(q_returns)
    var_q = sum((r - mean_q) ** 2 for r in q_returns) / (len(q_returns) - 1)

    return var_q / (q * var_1)


def variance_ratio_to_regime(vr: Optional[float]) -> str:
    """
    Classify regime: 'momentum', 'reversion', or 'neutral'.
    """
    if vr is None:
        return "neutral"
    if vr > 1.1:
        return "momentum"
    if vr < 0.9:
        return "reversion"
    return "neutral"
