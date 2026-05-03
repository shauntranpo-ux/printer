from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Dict, List, Optional

# Nearest valid strike spacing per asset (dollars)
STRIKE_SPACING: Dict[str, float] = {
    'BTC': 100.0,
    'ETH': 5.0,
    'SOL': 0.5,
    'XRP': 0.005,
    'DOGE': 0.001,
}


def nearest_strike(price: float, asset: str) -> float:
    """Round price to nearest valid Kalshi strike for the given asset."""
    spacing = STRIKE_SPACING.get(asset, 1.0)
    return round(round(price / spacing) * spacing, 8)


def build_binary_labels(
    bars: pd.DataFrame,
    strike: float,
    horizon_bars: int = 15,
) -> np.ndarray:
    """
    For bar i, outcome = 1 if bars.close[i + horizon_bars] > strike else 0.
    Returns array of length len(bars) - horizon_bars.
    """
    closes = bars['close'].values
    return (closes[horizon_bars:] > strike).astype(np.int8)


def build_lagged_labels(
    bars: pd.DataFrame,
    strike: float,
    lags: Optional[List[int]] = None,
) -> Dict[int, np.ndarray]:
    """
    Build binary labels at multiple lags for IC decay analysis.
    Returns {lag: array of length (len(bars) - lag)}.
    """
    if lags is None:
        lags = [1, 2, 4, 8]
    return {lag: build_binary_labels(bars, strike, horizon_bars=lag) for lag in lags}
