"""Pure OHLCV feature functions.

All functions:
  - Accept a pandas DataFrame with columns: open, high, low, close, volume
    (DatetimeIndex, UTC, 1-minute bars, ascending time order).
  - Return None if insufficient data — never raise.
  - Have no side effects, no clocks, no I/O.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def return_over(df: pd.DataFrame, minutes: int) -> float | None:
    """Simple return from `minutes` bars ago to the latest close."""
    if minutes < 1 or len(df) < minutes + 1:
        return None
    prev = float(df["close"].iloc[-(minutes + 1)])
    curr = float(df["close"].iloc[-1])
    if prev == 0:
        return None
    return curr / prev - 1.0


def realized_vol(df: pd.DataFrame, window_min: int) -> float | None:
    """Std-dev of 1-minute log returns over the last `window_min` bars."""
    if window_min < 2 or len(df) < window_min + 1:
        return None
    closes = df["close"].iloc[-(window_min + 1):].to_numpy(dtype=float)
    log_rets = np.diff(np.log(closes))
    if len(log_rets) < 2 or np.any(closes <= 0):
        return None
    return float(np.std(log_rets, ddof=1))


def rsi(df: pd.DataFrame, period: int = 14) -> float | None:
    """RSI (simple-average variant) of the last `period` closes."""
    if period < 2 or len(df) < period + 1:
        return None
    deltas = np.diff(df["close"].iloc[-(period + 1):].to_numpy(dtype=float))
    gains = deltas.clip(min=0)
    losses = (-deltas).clip(min=0)
    avg_gain = gains.mean()
    avg_loss = losses.mean()
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - 100.0 / (1.0 + rs))


def atr(df: pd.DataFrame, period: int = 14) -> float | None:
    """Average True Range over the last `period` bars."""
    if period < 1 or len(df) < period + 1:
        return None
    highs = df["high"].iloc[-period:].to_numpy(dtype=float)
    lows = df["low"].iloc[-period:].to_numpy(dtype=float)
    prev_closes = df["close"].iloc[-(period + 1) : -1].to_numpy(dtype=float)
    tr = np.maximum(
        highs - lows,
        np.maximum(np.abs(highs - prev_closes), np.abs(lows - prev_closes)),
    )
    return float(tr.mean())


def atr_percentile(
    df: pd.DataFrame,
    atr_value: float,
    lookback_bars: int = 240,
    period: int = 14,
) -> float | None:
    """Percentile of `atr_value` in the distribution of recent ATRs.

    Requires at least `lookback_bars + period + 1` rows.
    """
    required = lookback_bars + period + 1
    if len(df) < required:
        return None

    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)

    # True range for all bars (n-1 values)
    tr_all = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1])),
    )

    # Rolling mean ATR of length `period` over the last `lookback_bars` positions
    n = len(tr_all)
    start = n - lookback_bars - period + 1
    if start < 0:
        return None

    atr_vals = np.array(
        [tr_all[i : i + period].mean() for i in range(start, start + lookback_bars)]
    )
    return float((atr_vals <= atr_value).mean())


def vwap_deviation(df: pd.DataFrame, window_min: int) -> float | None:
    """(close - VWAP) / VWAP for the last `window_min` bars."""
    if window_min < 1 or len(df) < window_min:
        return None
    sub = df.iloc[-window_min:]
    typical = (
        sub["high"].to_numpy(dtype=float)
        + sub["low"].to_numpy(dtype=float)
        + sub["close"].to_numpy(dtype=float)
    ) / 3.0
    volumes = sub["volume"].to_numpy(dtype=float)
    total_vol = volumes.sum()
    if total_vol == 0:
        return None
    vwap = float((typical * volumes).sum() / total_vol)
    if vwap == 0:
        return None
    current_close = float(sub["close"].iloc[-1])
    return (current_close - vwap) / vwap
