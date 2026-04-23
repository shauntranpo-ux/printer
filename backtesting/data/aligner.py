"""
Event stream aligner for the backtesting engine.

Produces a sorted, typed event stream. iter_windows yields events strictly
before each window timestamp — no look-ahead.
"""
from __future__ import annotations
import pandas as pd
from dataclasses import dataclass, field
from typing import Iterator, Literal

EventType = Literal["bar", "l2", "trade", "funding", "kalshi_tick", "label"]


@dataclass(order=True)
class Event:
    timestamp: pd.Timestamp
    event_type: EventType = field(compare=False)
    payload: dict = field(compare=False)


def _validate_utc(df: pd.DataFrame, name: str, ts_col: str = "timestamp") -> None:
    if df.empty:
        return
    if df[ts_col].dt.tz is None:
        raise ValueError(f"[{name}] timestamps must be UTC-aware. Found tz-naive.")
    if str(df[ts_col].dt.tz) != "UTC":
        raise ValueError(f"[{name}] timestamps must be UTC, found {df[ts_col].dt.tz}.")


def build_event_stream(
    bars: pd.DataFrame,
    labels: pd.DataFrame,
    l2_snapshots: pd.DataFrame | None = None,
    trades: pd.DataFrame | None = None,
    funding: pd.DataFrame | None = None,
    kalshi_ticks: pd.DataFrame | None = None,
) -> list[Event]:
    """
    Merge all input DataFrames into a single sorted event list.
    Every DataFrame must have a UTC-aware 'timestamp' column.
    """
    events: list[Event] = []

    for name, df, etype in [
        ("bars",         bars,         "bar"),
        ("labels",       labels,       "label"),
        ("l2_snapshots", l2_snapshots, "l2"),
        ("trades",       trades,       "trade"),
        ("funding",      funding,      "funding"),
        ("kalshi_ticks", kalshi_ticks, "kalshi_tick"),
    ]:
        if df is None or df.empty:
            continue
        _validate_utc(df, name)
        for row in df.itertuples(index=False):
            ts = row.timestamp if isinstance(row.timestamp, pd.Timestamp) else pd.Timestamp(row.timestamp)
            payload = {k: getattr(row, k) for k in df.columns if k != "timestamp"}
            events.append(Event(timestamp=ts, event_type=etype, payload=payload))

    events.sort(key=lambda e: e.timestamp)
    return events


_MAX_LOOKBACK_EVENTS = 1500  # 240 bars (4h @ 1m) + buffer for kalshi/trade events


def iter_windows(
    events: list[Event],
    label_timestamps: pd.DatetimeIndex,
) -> Iterator[tuple[pd.Timestamp, list[Event]]]:
    """
    Yield (window_open_time, events_strictly_before_window_open) for each label.
    The engine gets only events with timestamp < window_open to prevent look-ahead.
    """
    events_seen: list[Event] = []
    event_idx = 0
    n = len(events)

    for window_ts in sorted(label_timestamps):
        while event_idx < n and events[event_idx].timestamp < window_ts:
            events_seen.append(events[event_idx])
            event_idx += 1
            if len(events_seen) > _MAX_LOOKBACK_EVENTS * 2:
                events_seen = events_seen[-_MAX_LOOKBACK_EVENTS:]
        yield window_ts, events_seen[-_MAX_LOOKBACK_EVENTS:]
