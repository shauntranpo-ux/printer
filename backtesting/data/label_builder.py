"""
Label builder for Kalshi 15-minute binary markets.

y = 1 if reference_price_close > reference_price_open (underlying CLOSE > OPEN at t+15min)
y = 0 otherwise

Missing data in a window: window is DROPPED, not imputed.
"""
from __future__ import annotations
import pandas as pd
import numpy as np
from typing import Optional

WINDOW_MINUTES = 15


def build_labels(
    bars: pd.DataFrame,
    reference_source: str = "spot",
    window_minutes: int = WINDOW_MINUTES,
    drop_incomplete: bool = True,
) -> pd.DataFrame:
    """
    Build binary labels from OHLCV bars.

    Args:
        bars: DataFrame with columns [timestamp, open, high, low, close, volume].
              Timestamp must be UTC-aware.
        reference_source: "spot" or "perp" — informational, caller ensures correct bars.
        window_minutes: length of each binary window (default 15).
        drop_incomplete: if True, drop windows with fewer than expected bars.

    Returns:
        DataFrame with columns: timestamp (UTC), label (0/1),
        reference_price_open, reference_price_close, log_return
    """
    if bars.empty:
        return pd.DataFrame(columns=[
            "timestamp", "label", "reference_price_open",
            "reference_price_close", "log_return"
        ])

    bars = bars.copy().sort_values("timestamp")
    _validate_bars(bars)

    bars = bars.set_index("timestamp")
    freq = f"{window_minutes}min"

    open_prices  = bars["open"].resample(freq, label="left", closed="left").first()
    close_prices = bars["close"].resample(freq, label="left", closed="left").last()
    bar_counts   = bars["close"].resample(freq, label="left", closed="left").count()

    expected_bars = window_minutes * 60 // 10  # 10-second bars

    result = pd.DataFrame({
        "timestamp":             open_prices.index,
        "reference_price_open":  open_prices.values,
        "reference_price_close": close_prices.values,
        "bar_count":             bar_counts.values,
    })

    if drop_incomplete:
        result = result[result["bar_count"] == expected_bars].copy()

    result = result[result["reference_price_open"] > 0].copy()
    result = result[result["reference_price_close"] > 0].copy()

    result["log_return"] = np.log(
        result["reference_price_close"] / result["reference_price_open"]
    )
    result["label"] = (result["reference_price_close"] > result["reference_price_open"]).astype(int)

    return result[["timestamp", "label", "reference_price_open",
                   "reference_price_close", "log_return"]].reset_index(drop=True)


def _validate_bars(bars: pd.DataFrame) -> None:
    required = {"timestamp", "open", "close"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(f"bars missing required columns: {missing}")
    if bars["timestamp"].dt.tz is None:
        raise ValueError("bars['timestamp'] must be UTC-aware. Call _enforce_utc first.")


def align_labels_to_signals(
    labels: pd.DataFrame,
    signal_times: pd.DatetimeIndex,
) -> pd.DataFrame:
    """
    Left-join labels onto signal_times so each signal window has its label.
    Signals without a label in the window are dropped.
    """
    labels = labels.copy().set_index("timestamp")
    result = labels.reindex(signal_times).dropna(subset=["label"])
    result["label"] = result["label"].astype(int)
    return result.reset_index().rename(columns={"index": "timestamp"})
