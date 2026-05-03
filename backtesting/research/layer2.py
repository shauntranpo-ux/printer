from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List

import numpy as np
import pandas as pd

from backtesting.metrics.trading import sharpe_ratio


@dataclass
class NullResult:
    real_sharpe: float
    null_sharpes: np.ndarray
    p_value: float        # one-tailed: fraction where null >= real
    null_p95: float
    verdict: str


def flip_side_pnl(pnls: np.ndarray) -> np.ndarray:
    """Invert all P&Ls (simulating the opposite side on each trade)."""
    return -pnls


def run_null_simulation(
    trade_pnls: np.ndarray,
    n_iter: int = 1000,
    seed: int = 0,
) -> NullResult:
    """
    'Flip-side' null: randomly flip each trade's P&L sign with 50% probability
    n_iter times. P&L sign flip simulates trading the opposite side.
    p-value = fraction of null sharpes >= real sharpe (one-tailed).
    """
    rng = np.random.default_rng(seed)
    real_sharpe = sharpe_ratio(trade_pnls)

    null_sharpes = np.empty(n_iter)
    for i in range(n_iter):
        signs = rng.choice([-1.0, 1.0], size=len(trade_pnls))
        null_pnls = trade_pnls * signs
        null_sharpes[i] = sharpe_ratio(null_pnls)

    p_value = float(np.mean(null_sharpes >= real_sharpe))

    return NullResult(
        real_sharpe=real_sharpe,
        null_sharpes=null_sharpes,
        p_value=p_value,
        null_p95=float(np.percentile(null_sharpes, 95)),
        verdict=layer2_verdict(p_value),
    )


def layer2_verdict(p_value: float) -> str:
    if p_value < 0.05:
        return 'PASS'
    if p_value < 0.10:
        return 'CONDITIONAL'
    return 'FAIL'


def audit_lookahead(asset: str) -> List[str]:
    """
    Static checks for known lookahead risks. Returns list of finding strings.
    Empty list = no issues found.
    """
    findings = []
    root = os.path.join(os.path.dirname(__file__), '..', '..', 'backtesting', 'output', 'models')

    cal_path = os.path.join(root, f'{asset.lower()}_calibrated_model.pkl')
    if os.path.exists(cal_path):
        findings.append(
            f'[WARN] {cal_path} exists — verify calibrator was fit on training fold only, '
            'not full dataset, within WFA windows.'
        )

    fb_path = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'strategies', 'feature_builder.py')
    if os.path.exists(fb_path):
        with open(fb_path) as f:
            src = f.read()
        if 'shift(-' in src or 'future' in src.lower():
            findings.append('[WARN] feature_builder.py may reference future bars (found shift(- or "future").')

    return findings


def run_layer2(trade_log: pd.DataFrame, asset: str, n_iter: int = 1000) -> dict:
    """
    Layer 2: Strategy simulation + null hypothesis.

    trade_log must have column 'pnl' (dollars per dollar staked).
    """
    pnls = trade_log['pnl'].values
    audit = audit_lookahead(asset)
    null  = run_null_simulation(pnls, n_iter=n_iter)

    return {
        'real_sharpe':      null.real_sharpe,
        'null_p95':         null.null_p95,
        'p_value':          null.p_value,
        'verdict':          null.verdict,
        'lookahead_issues': audit,
        'n_trades':         len(pnls),
    }
