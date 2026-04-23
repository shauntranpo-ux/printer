"""
Label builder for Kalshi 15-minute binary markets.

y = 1 if reference_price_close > reference_price_open (underlying CLOSE > OPEN at t+15min)
y = 0 otherwise

Missing data in a window: window is DROPPED, not imputed.
Granularity is auto-detected from the bars' median inter-bar interval.
"""
from __future__ import annotations
import logging
import pandas as pd
import numpy as np
from typing import Optional

logger = logging.getLogger(__name__)

WINDOW_MINUTES = 15


def _detect_bar_granularity(bars: pd.DataFrame, ts_col: str = "timestamp") -> int:
    """Detect bar granularity in seconds from the median inter-bar interval."""
    if len(bars) < 2:
        return 10  # default: assume 10-second bars if we can't detect
    ts = pd.to_datetime(bars[ts_col])
    diffs = ts.sort_values().diff().dropna()
    median_s = diffs.dt.total_seconds().median()
    return max(1, int(round(median_s)))


def build_labels(
    bars: pd.DataFrame,
    reference_source: str = "spot",
    window_minutes: int = WINDOW_MINUTES,
    drop_incomplete: bool = True,
    bars_per_window: Optional[int] = None,
) -> pd.DataFrame:
    """
    Build binary labels from OHLCV bars.

    Args:
        bars: DataFrame with columns [timestamp, open, high, low, close, volume].
              Timestamp must be UTC-aware.
        reference_source: "spot" or "perp" — informational, caller ensures correct bars.
        window_minutes: length of each binary window (default 15).
        drop_incomplete: if True, drop windows with fewer than the tolerance floor.
        bars_per_window: expected bars per window. If None, auto-detected from bar granularity.
                         Auto-detection: median inter-bar interval → window_minutes * 60 // granularity.

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

    # Determine expected bars per window
    if bars_per_window is None:
        granularity_s = _detect_bar_granularity(bars)
        bars_per_window = window_minutes * 60 // granularity_s

    # Tolerance floor: allow 1 missing bar per window (e.g. occasional missing minute)
    min_bars = max(1, bars_per_window - 1)

    bars = bars.set_index("timestamp")
    freq = f"{window_minutes}min"

    open_prices  = bars["open"].resample(freq, label="left", closed="left").first()
    close_prices = bars["close"].resample(freq, label="left", closed="left").last()
    bar_counts   = bars["close"].resample(freq, label="left", closed="left").count()

    result = pd.DataFrame({
        "timestamp":             open_prices.index,
        "reference_price_open":  open_prices.values,
        "reference_price_close": close_prices.values,
        "bar_count":             bar_counts.values,
    })

    if drop_incomplete:
        at_floor = result[
            (result["bar_count"] >= min_bars) & (result["bar_count"] < bars_per_window)
        ]
        if not at_floor.empty:
            logger.warning(
                "%d window(s) at tolerance floor (%d bars; expected %d). "
                "Keeping with warning — check for missing data.",
                len(at_floor), min_bars, bars_per_window,
            )
        result = result[result["bar_count"] >= min_bars].copy()

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
