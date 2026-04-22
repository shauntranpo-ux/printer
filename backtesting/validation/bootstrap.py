"""
Politis-Romano stationary block bootstrap for confidence intervals.

Used for confidence intervals on Sharpe, win rate, and expectancy.
Default mean block length: 24 hours of 15-min trades = 96 periods.

Reference: Politis & Romano (1994) — "The Stationary Bootstrap."
"""
from __future__ import annotations
import numpy as np
from typing import Callable


def _geometric_block_lengths(n: int, mean_block_length: float, rng: np.random.Generator) -> list[int]:
    """Sample block lengths from geometric distribution with mean=mean_block_length."""
    p = 1.0 / mean_block_length
    blocks: list[int] = []
    total = 0
    while total < n:
        length = int(rng.geometric(p))
        blocks.append(min(length, n - total))
        total += blocks[-1]
    return blocks


def stationary_bootstrap_sample(
    data: np.ndarray,
    mean_block_length: float,
    rng: np.random.Generator,
) -> np.ndarray:
    """
    Draw one bootstrap sample of the same length as `data` using
    the Politis-Romano stationary block bootstrap.
    """
    n = len(data)
    block_lengths = _geometric_block_lengths(n, mean_block_length, rng)
    sample = np.empty(n, dtype=data.dtype)
    pos = 0
    for blen in block_lengths:
        start = int(rng.integers(0, n))
        for j in range(blen):
            sample[pos] = data[(start + j) % n]
            pos += 1
            if pos >= n:
                break
        if pos >= n:
            break
    return sample


def bootstrap_ci(
    data: np.ndarray,
    statistic: Callable[[np.ndarray], float],
    n_iterations: int = 1000,
    mean_block_hours: float = 24.0,
    periods_per_hour: float = 4.0,  # 15-min periods
    alpha: float = 0.05,
    seed: int = 42,
) -> tuple[float, float, float]:
    """
    Compute bootstrap confidence interval for a statistic.

    Returns:
        (point_estimate, lower_ci, upper_ci) at (1-alpha) confidence level.
    """
    mean_block_length = mean_block_hours * periods_per_hour
    rng = np.random.default_rng(seed)
    point = statistic(data)

    boot_stats = np.empty(n_iterations)
    for i in range(n_iterations):
        sample = stationary_bootstrap_sample(data, mean_block_length, rng)
        boot_stats[i] = statistic(sample)

    lower = float(np.percentile(boot_stats, 100 * alpha / 2))
    upper = float(np.percentile(boot_stats, 100 * (1 - alpha / 2)))
    return point, lower, upper
