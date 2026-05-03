"""
Runs each D3 sub-signal independently on historical 1-min bars,
returning p_yes predictions for IC analysis.

Signals requiring live Kalshi/cross-venue data fall back to p=0.5 (neutral).
IC for these signals will always be uninformative and expected to FAIL.
"""
from __future__ import annotations

import os
import sys
import numpy as np
import pandas as pd
from typing import Dict

_SRC = os.path.join(os.path.dirname(__file__), '..', '..', 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

SIGNAL_NAMES = [
    'supertrend_direction',
    'bs_probability',
    'momentum_delta',
    'exhaustion_fade',
    'ratio_divergence',
    'rolling_beta',
    'variance_ratio_signal',
    'volume_spike',
]


def _supertrend_predictions(bars: pd.DataFrame, atr_period: int = 14, multiplier: float = 5.0) -> np.ndarray:
    try:
        closes = bars['close'].values
        highs = bars['high'].values
        lows = bars['low'].values
        n = len(bars)

        tr = [highs[0] - lows[0]]
        for i in range(1, n):
            h, lo, pc = highs[i], lows[i], closes[i - 1]
            tr.append(max(h - lo, abs(h - pc), abs(lo - pc)))

        atr_vals = [None] * n
        for i in range(atr_period - 1, n):
            atr_vals[i] = sum(tr[i - atr_period + 1: i + 1]) / atr_period

        start = atr_period - 1
        hl2 = [(highs[i] + lows[i]) / 2.0 for i in range(n)]

        upper_final = [None] * n
        lower_final = [None] * n
        direction = [None] * n

        for i in range(start, n):
            av = atr_vals[i]
            if av is None:
                continue
            raw_upper = hl2[i] + multiplier * av
            raw_lower = hl2[i] - multiplier * av
            if i == start:
                upper_final[i] = raw_upper
                lower_final[i] = raw_lower
                direction[i] = 1 if closes[i] >= hl2[i] else -1
                continue
            pu, pl, pd_ = upper_final[i - 1], lower_final[i - 1], direction[i - 1]
            upper_final[i] = min(raw_upper, pu) if pu is not None and closes[i - 1] <= pu else raw_upper
            lower_final[i] = max(raw_lower, pl) if pl is not None and closes[i - 1] >= pl else raw_lower
            if pd_ == -1 and closes[i] > upper_final[i]:
                direction[i] = 1
            elif pd_ == 1 and closes[i] < lower_final[i]:
                direction[i] = -1
            else:
                direction[i] = pd_

        preds = np.full(n, 0.5)
        for i, d in enumerate(direction):
            if d == 1:
                preds[i] = 0.70
            elif d == -1:
                preds[i] = 0.30
        return preds
    except Exception:
        return np.full(len(bars), 0.5)


def _bs_predictions(bars: pd.DataFrame, strike: float, seconds_left: float = 900.0) -> np.ndarray:
    try:
        from strategies.signals.black_scholes import compute_bs_p_yes
        prices = bars['close'].values
        log_ret = np.diff(np.log(np.maximum(prices, 1e-8)))
        vol_1m = float(np.std(log_ret)) if len(log_ret) > 5 else 0.01
        result = np.full(len(prices), 0.5)
        for i, p in enumerate(prices):
            v = compute_bs_p_yes(current_price=p, strike=strike, realized_vol_1min=vol_1m, seconds_left=seconds_left)
            if v is not None:
                result[i] = v
        return result
    except Exception:
        return np.full(len(bars), 0.5)


def _momentum_predictions(bars: pd.DataFrame, lookback: int = 4) -> np.ndarray:
    closes = bars['close'].values
    preds = np.full(len(closes), 0.5)
    for i in range(lookback, len(closes)):
        delta = closes[i] - closes[i - lookback]
        preds[i] = 0.65 if delta > 0 else 0.35
    return preds


def extract_all_signals(
    bars: pd.DataFrame,
    strike: float,
    asset: str,
) -> Dict[str, np.ndarray]:
    """
    Returns {signal_name: np.ndarray of p_yes} for all SIGNAL_NAMES.
    Each array has the same length as bars. Values clipped to [0, 1].
    Signals requiring live data fall back to 0.5 (neutral / no-information).
    """
    n = len(bars)
    results: Dict[str, np.ndarray] = {}

    results['supertrend_direction'] = np.clip(_supertrend_predictions(bars), 0.0, 1.0)
    results['bs_probability']       = np.clip(_bs_predictions(bars, strike), 0.0, 1.0)
    results['momentum_delta']       = np.clip(_momentum_predictions(bars), 0.0, 1.0)

    for name in ['exhaustion_fade', 'ratio_divergence', 'rolling_beta', 'variance_ratio_signal', 'volume_spike']:
        results[name] = np.full(n, 0.5)

    return results
