from __future__ import annotations

import numpy as np
import pandas as pd
from typing import Any, Dict

from backtesting.research.ic_analysis import evaluate_signal
from backtesting.research.label_builder import build_binary_labels, build_lagged_labels
from backtesting.research.signal_extractor import extract_all_signals, SIGNAL_NAMES


def layer1_verdict(n_failing: int, n_total: int) -> str:
    frac = n_failing / n_total if n_total > 0 else 1.0
    if frac >= 0.5:
        return 'FAIL'
    if frac >= 0.25:
        return 'CONDITIONAL'
    return 'PASS'


def run_layer1(bars: pd.DataFrame, strike: float, asset: str) -> Dict[str, Any]:
    """
    Layer 1: Signal Validation.

    For each sub-signal, compute IC / ICIR / t-stat / IC decay against
    binary directional outcomes (close[T+15] > strike).

    Returns:
        {
            'signals': {signal_name: {ic, icir, t_stat, ic_decay, n_obs, verdict}},
            'verdict': PASS | CONDITIONAL | FAIL,
            'n_failing': int,
            'n_signals': int,
        }
    """
    outcomes_15 = build_binary_labels(bars, strike=strike, horizon_bars=15)
    lag_outcomes = build_lagged_labels(bars, strike=strike, lags=[1, 2, 4, 8])
    signal_preds = extract_all_signals(bars, strike=strike, asset=asset)

    signal_results = {}
    n_failing = 0

    for name in SIGNAL_NAMES:
        preds = signal_preds[name]
        # Align lengths: labels are shorter by horizon_bars
        n = min(len(preds), len(outcomes_15))
        aligned_pred = preds[:n]
        aligned_out  = outcomes_15[:n]
        aligned_lag  = {lag: arr[:n] for lag, arr in lag_outcomes.items()}

        ic_result = evaluate_signal(aligned_pred, aligned_out, aligned_lag)

        signal_results[name] = {
            'ic':       ic_result.ic,
            'icir':     ic_result.icir,
            't_stat':   ic_result.t_stat,
            'ic_decay': ic_result.ic_decay,
            'n_obs':    ic_result.n_obs,
            'verdict':  ic_result.verdict,
        }
        if ic_result.verdict == 'FAIL':
            n_failing += 1

    return {
        'signals':   signal_results,
        'verdict':   layer1_verdict(n_failing, len(SIGNAL_NAMES)),
        'n_failing': n_failing,
        'n_signals': len(SIGNAL_NAMES),
    }
