"""
Runs each live voter from compute_15m_signal independently on historical 1-min bars,
returning p_yes predictions for IC analysis.
"""
from __future__ import annotations

import logging
import math
import os
import sys
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

_log = logging.getLogger(__name__)

_SRC = os.path.join(os.path.dirname(__file__), '..', '..', 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

SIGNAL_NAMES = [
    'v1_bs_prob',
    'v2_mtf_momentum',
    'v3_rsi',
    'v4_bollinger',
    'v5_mtf_magnitude',
]

# Mirrored from fifteen_min_signal.py — update both if thresholds change
_MTF_THRESHOLDS  = {'BTC': 0.0005, 'ETH': 0.0005, 'SOL': 0.0005, 'XRP': 0.0005}
_RSI_THRESHOLDS  = {'BTC': 5.0,    'ETH': 8.0,     'SOL': 10.0,   'XRP': 8.0}
_BOLL_THRESHOLDS = {'BTC': 0.75,   'ETH': 0.50,    'SOL': 0.50,   'XRP': 0.35}
_MTF_DEFAULT     = 0.0005
_RSI_DEFAULT     = 8.0
_BOLL_DEFAULT    = 0.50


# ── helpers copied verbatim from fifteen_min_signal.py ─────────────────────────

def _rsi(prices: List[float], period: int = 14) -> Optional[float]:
    if len(prices) < period + 2:
        return None
    changes = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains  = [max(0.0, c) for c in changes]
    losses = [max(0.0, -c) for c in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _boll_zscore(prices: List[float], period: int = 20) -> Optional[float]:
    if len(prices) < period:
        return None
    recent = prices[-period:]
    mean_p = sum(recent) / len(recent)
    var_p  = sum((p - mean_p) ** 2 for p in recent) / (len(recent) - 1)
    std_p  = math.sqrt(var_p) if var_p > 0 else 0.0
    if std_p <= 0:
        return None
    return (prices[-1] - mean_p) / std_p


def _multi_tf_mom(prices: List[float]) -> Optional[float]:
    if len(prices) < 31:
        return None
    cur = prices[-1]
    if cur <= 0:
        return None
    r5  = (cur - prices[-6])  / prices[-6]  if prices[-6]  > 0 else 0.0
    r15 = (cur - prices[-16]) / prices[-16] if prices[-16] > 0 else 0.0
    r30 = (cur - prices[-31]) / prices[-31] if prices[-31] > 0 else 0.0
    return (r5 + r15 + r30) / 3.0


# ── per-voter extractors ───────────────────────────────────────────────────────

def _v1_predictions(bars: pd.DataFrame, strike: float, seconds_left: float = 900.0) -> np.ndarray:
    """V1: BS p_yes directly (continuous)."""
    try:
        from strategies.signals.black_scholes import compute_bs_p_yes
        prices = bars['close'].values
        log_ret = np.diff(np.log(np.maximum(prices, 1e-8)))
        vol_1m = float(np.std(log_ret)) if len(log_ret) > 5 else 0.01
        result = np.full(len(prices), 0.5)
        for i, p in enumerate(prices):
            v = compute_bs_p_yes(
                current_price=p, strike=strike,
                realized_vol_1min=vol_1m, seconds_left=seconds_left,
            )
            if v is not None:
                result[i] = v
        return result
    except Exception as exc:
        _log.warning("_v1_predictions failed: %s", exc)
        return np.full(len(bars), 0.5)


def _v2_predictions(bars: pd.DataFrame, asset: str) -> np.ndarray:
    """V2: MTF momentum inverted — negative momentum → 0.65 (YES)."""
    T = _MTF_THRESHOLDS.get(asset.upper(), _MTF_DEFAULT)
    prices = bars['close'].tolist()
    n = len(prices)
    preds = np.full(n, 0.5)
    for i in range(30, n):
        mtf = _multi_tf_mom(prices[:i + 1])
        if mtf is None:
            continue
        if mtf < -T:
            preds[i] = 0.65
        elif mtf > T:
            preds[i] = 0.35
    return preds


def _v3_predictions(bars: pd.DataFrame, asset: str) -> np.ndarray:
    """V3: RSI deviation inverted — oversold (rsi_dev < -T) → 0.65 (YES)."""
    T = _RSI_THRESHOLDS.get(asset.upper(), _RSI_DEFAULT)
    prices = bars['close'].tolist()
    n = len(prices)
    preds = np.full(n, 0.5)
    for i in range(15, n):
        rsi = _rsi(prices[:i + 1])
        if rsi is None:
            continue
        rsi_dev = rsi - 50.0
        if rsi_dev < -T:
            preds[i] = 0.65
        elif rsi_dev > T:
            preds[i] = 0.35
    return preds


def _v4_predictions(bars: pd.DataFrame, asset: str) -> np.ndarray:
    """V4: Bollinger z-score inverted — below lower band (z < -T) → 0.65 (YES)."""
    T = _BOLL_THRESHOLDS.get(asset.upper(), _BOLL_DEFAULT)
    prices = bars['close'].tolist()
    n = len(prices)
    preds = np.full(n, 0.5)
    for i in range(19, n):
        boll = _boll_zscore(prices[:i + 1])
        if boll is None:
            continue
        if boll < -T:
            preds[i] = 0.65
        elif boll > T:
            preds[i] = 0.35
    return preds


def _v5_predictions(bars: pd.DataFrame, asset: str) -> np.ndarray:
    """V5: MTF magnitude soft confirmation — uses T/2 threshold, inverted."""
    T = _MTF_THRESHOLDS.get(asset.upper(), _MTF_DEFAULT) / 2.0
    prices = bars['close'].tolist()
    n = len(prices)
    preds = np.full(n, 0.5)
    for i in range(30, n):
        mtf = _multi_tf_mom(prices[:i + 1])
        if mtf is None:
            continue
        if mtf < -T:
            preds[i] = 0.65
        elif mtf > T:
            preds[i] = 0.35
    return preds


def extract_all_signals(
    bars: pd.DataFrame,
    strike: float,
    asset: str,
) -> Dict[str, np.ndarray]:
    """
    Returns {signal_name: np.ndarray of p_yes} for all SIGNAL_NAMES.
    Each array has the same length as bars. Values clipped to [0, 1].
    """
    return {
        'v1_bs_prob':       np.clip(_v1_predictions(bars, strike), 0.0, 1.0),
        'v2_mtf_momentum':  np.clip(_v2_predictions(bars, asset), 0.0, 1.0),
        'v3_rsi':           np.clip(_v3_predictions(bars, asset), 0.0, 1.0),
        'v4_bollinger':     np.clip(_v4_predictions(bars, asset), 0.0, 1.0),
        'v5_mtf_magnitude': np.clip(_v5_predictions(bars, asset), 0.0, 1.0),
    }
