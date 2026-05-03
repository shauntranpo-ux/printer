from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

import numpy as np
import pandas as pd

from backtesting.metrics.trading import sharpe_ratio


@dataclass
class PermResult:
    real_sharpe: float
    null_median: float
    null_p95: float
    p_value: float       # one-tailed: fraction where null >= real
    verdict: str


def block_shuffle(arr: np.ndarray, block_size: int = 10, seed: int = 0) -> np.ndarray:
    """
    Shuffle arr in contiguous blocks of block_size, preserving all values.
    Block order is randomised; values within each block are unchanged.
    """
    rng = np.random.default_rng(seed)
    n = len(arr)
    blocks = [arr[i:i + block_size] for i in range(0, n, block_size)]
    rng.shuffle(blocks)
    shuffled = np.concatenate(blocks)
    return shuffled[:n]


def _max_drawdown(pnls: np.ndarray) -> float:
    """Maximum drawdown from cumulative P&L series (negative value; less negative = better)."""
    cum = np.cumsum(pnls)
    running_max = np.maximum.accumulate(cum)
    return float(np.min(cum - running_max))


def full_shuffle_test(pnls: np.ndarray, n_iter: int = 10_000, seed: int = 0) -> PermResult:
    """Full permutation test: randomly reorder P&Ls, test if real max-drawdown is better than null."""
    rng = np.random.default_rng(seed)
    real_stat = _max_drawdown(pnls)
    # One-tailed: is real drawdown less negative (better) than null?
    null_stats = np.array([_max_drawdown(rng.permutation(pnls)) for _ in range(n_iter)])
    p_val = float(np.mean(null_stats <= real_stat))
    verdict = 'PASS' if p_val < 0.05 else ('CONDITIONAL' if p_val < 0.10 else 'FAIL')
    return PermResult(
        real_sharpe=sharpe_ratio(pnls),  # keep for reporting; not the test statistic
        null_median=float(np.median(null_stats)),
        null_p95=float(np.percentile(null_stats, 95)),
        p_value=p_val,
        verdict=verdict,
    )


def block_shuffle_test(
    pnls: np.ndarray,
    n_iter: int = 10_000,
    block_size: int = 10,
    seed: int = 0,
) -> PermResult:
    """Block shuffle test: shuffle 10-trade blocks, test drawdown (preserves within-block autocorrelation)."""
    rng = np.random.default_rng(seed)
    seeds = rng.integers(0, 2**31, n_iter)
    real_stat = _max_drawdown(pnls)
    null_stats = np.array([
        _max_drawdown(block_shuffle(pnls, block_size=block_size, seed=int(s)))
        for s in seeds
    ])
    p_val = float(np.mean(null_stats <= real_stat))
    verdict = 'PASS' if p_val < 0.05 else ('CONDITIONAL' if p_val < 0.10 else 'FAIL')
    return PermResult(
        real_sharpe=sharpe_ratio(pnls),
        null_median=float(np.median(null_stats)),
        null_p95=float(np.percentile(null_stats, 95)),
        p_value=p_val,
        verdict=verdict,
    )


def min_trades_needed(win_rate: float, alpha: float = 0.05) -> int:
    """
    Approximate minimum trades for a permutation test to reach p < alpha.
    Normal approximation: n >= (Z_alpha / (win_rate - 0.5))^2 * 0.25
    """
    from scipy.stats import norm
    if win_rate <= 0.5:
        return 10_000
    z = norm.ppf(1 - alpha)
    edge = win_rate - 0.5
    return max(30, int(np.ceil((z / edge) ** 2 * 0.25)))


def run_layer4(trade_log: pd.DataFrame, n_iter: int = 10_000) -> Dict[str, Any]:
    """
    Layer 4: Trade-level permutation test.
    trade_log must have column 'pnl'.
    Primary verdict uses block shuffle (more conservative).
    """
    pnls = trade_log['pnl'].values
    win_rate = float(np.mean(pnls > 0))
    min_trades = min_trades_needed(win_rate)
    sufficient = len(pnls) >= min_trades

    full  = full_shuffle_test(pnls, n_iter=n_iter)
    block = block_shuffle_test(pnls, n_iter=n_iter)

    return {
        'n_trades':        len(pnls),
        'win_rate':        round(win_rate, 4),
        'real_sharpe':     round(full.real_sharpe, 4),
        'p_value_full':    round(full.p_value, 4),
        'p_value_block':   round(block.p_value, 4),
        'null_p95_block':  round(block.null_p95, 4),
        'verdict':         block.verdict if sufficient else 'INSUFFICIENT_DATA',
        'sufficient_data': sufficient,
        'min_trades':      min_trades,
    }
