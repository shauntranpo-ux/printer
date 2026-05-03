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


def _sign_flip(pnls: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Randomly flip each trade's P&L sign — the elementary null permutation."""
    signs = rng.choice(np.array([-1.0, 1.0]), size=len(pnls))
    return pnls * signs


def _block_sign_flip(pnls: np.ndarray, block_size: int, seed: int) -> np.ndarray:
    """
    Sign-flip entire blocks of trades together (preserves within-block autocorrelation).
    Each block of `block_size` consecutive trades gets the same random sign applied.
    """
    rng = np.random.default_rng(seed)
    n = len(pnls)
    out = pnls.copy()
    for i in range(0, n, block_size):
        sign = rng.choice(np.array([-1.0, 1.0]))
        out[i:i + block_size] *= sign
    return out


def full_shuffle_test(pnls: np.ndarray, n_iter: int = 10_000, seed: int = 0) -> PermResult:
    """
    Full sign-flip permutation test: randomly flip each trade's P&L sign each
    iteration.  This generates the null distribution of Sharpe ratios for a
    zero-edge strategy and tests whether the real Sharpe is in the upper tail.
    """
    rng = np.random.default_rng(seed)
    real_sr = sharpe_ratio(pnls)
    null_srs = np.array([sharpe_ratio(_sign_flip(pnls, rng)) for _ in range(n_iter)])
    p_val = float(np.mean(null_srs >= real_sr))
    verdict = 'PASS' if p_val < 0.05 else ('CONDITIONAL' if p_val < 0.10 else 'FAIL')
    return PermResult(
        real_sharpe=real_sr,
        null_median=float(np.median(null_srs)),
        null_p95=float(np.percentile(null_srs, 95)),
        p_value=p_val,
        verdict=verdict,
    )


def block_shuffle_test(
    pnls: np.ndarray,
    n_iter: int = 10_000,
    block_size: int = 10,
    seed: int = 0,
) -> PermResult:
    """
    Block sign-flip permutation test: flip the sign of 10-trade blocks together.
    More conservative than full sign-flip because it respects within-block
    autocorrelation structure, making it harder to reject the null.
    Seeds are pre-generated so each iteration uses a distinct, reproducible seed.
    """
    rng = np.random.default_rng(seed)
    seeds = rng.integers(0, 2**31, n_iter)
    real_sr = sharpe_ratio(pnls)
    null_srs = np.array([
        sharpe_ratio(_block_sign_flip(pnls, block_size=block_size, seed=int(s)))
        for s in seeds
    ])
    p_val = float(np.mean(null_srs >= real_sr))
    verdict = 'PASS' if p_val < 0.05 else ('CONDITIONAL' if p_val < 0.10 else 'FAIL')
    return PermResult(
        real_sharpe=real_sr,
        null_median=float(np.median(null_srs)),
        null_p95=float(np.percentile(null_srs, 95)),
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
