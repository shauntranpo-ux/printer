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


def build_strike_ladder_labels(
    underlying_df: pd.DataFrame,
    ladder_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Build binary labels for Kalshi hourly strike-ladder contracts (Strategy C).

    For each (event_id, strike), label = 1 if underlying close >= strike at
    event_close_time, else 0.

    Args:
        underlying_df: OHLCV bars DataFrame with UTC-aware 'timestamp' column.
        ladder_df:     Output of load_strike_ladder_history() — one row per
                       (event_id, strike, snapshot_timestamp).

    Returns:
        DataFrame with columns:
            event_id, strike, event_close_time (UTC),
            label (0/1), reference_price_close
        One row per (event_id, strike). Events where the underlying close
        price is unavailable are dropped entirely (all ~40 rows).
    """
    if underlying_df.empty or ladder_df.empty:
        return pd.DataFrame(columns=[
            "event_id", "strike", "event_close_time", "label", "reference_price_close"
        ])

    _validate_bars(underlying_df)

    bars = underlying_df.copy().sort_values("timestamp").set_index("timestamp")

    # One row per (event_id, event_close_time)
    events = (
        ladder_df[["event_id", "event_close_time"]]
        .drop_duplicates("event_id")
        .sort_values("event_close_time")
        .reset_index(drop=True)
    )

    # For each event, look up the underlying close at event_close_time
    # using backward-fill at the nearest bar at or before close_time.
    close_prices: dict[str, float] = {}
    dropped_events: list[str] = []

    last_bar_ts = bars.index.max()
    for _, ev_row in events.iterrows():
        eid = ev_row["event_id"]
        close_time = ev_row["event_close_time"]
        # Require the event close to be within the observed bar range;
        # otherwise we'd fabricate a "close" from stale data.
        if close_time > last_bar_ts:
            logger.warning(
                "Event %s: close_time %s beyond last bar %s — dropping event.",
                eid, close_time, last_bar_ts,
            )
            dropped_events.append(eid)
            continue
        # Find the latest bar at or before close_time
        candidates = bars.loc[bars.index <= close_time, "close"]
        if candidates.empty:
            logger.warning(
                "Event %s: no underlying bar at or before close_time %s — dropping event.",
                eid, close_time,
            )
            dropped_events.append(eid)
            continue
        close_prices[eid] = float(candidates.iloc[-1])

    if dropped_events:
        logger.warning("Dropped %d event(s) with missing underlying close.", len(dropped_events))

    # Build one row per (event_id, strike) for events that have a close price
    # Take the unique (event_id, strike) pairs from the ladder
    per_strike = (
        ladder_df[["event_id", "event_close_time", "strike"]]
        .drop_duplicates(subset=["event_id", "strike"])
        .copy()
    )

    # Filter to events with a valid close price
    per_strike = per_strike[per_strike["event_id"].isin(close_prices)].copy()

    if per_strike.empty:
        logger.warning("No valid labeled rows after filtering dropped events.")
        return pd.DataFrame(columns=[
            "event_id", "strike", "event_close_time", "label", "reference_price_close"
        ])

    per_strike["reference_price_close"] = per_strike["event_id"].map(close_prices)
    per_strike["label"] = (
        per_strike["reference_price_close"] >= per_strike["strike"]
    ).astype(int)

    return per_strike[
        ["event_id", "strike", "event_close_time", "label", "reference_price_close"]
    ].reset_index(drop=True)


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
