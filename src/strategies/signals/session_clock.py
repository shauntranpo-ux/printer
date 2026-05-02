"""Market-context helpers for the X3 XRP strategy."""

from __future__ import annotations
from typing import Optional


def prior_session_return(prices: list, lookback_seconds: int = 3600) -> Optional[float]:
    """
    Return the simple price return over the trailing `lookback_seconds`
    of the supplied (ts, price) series, used as a proxy for prior-session
    direction.

    NOTE: The X3 plan calls for a 4-hour US-session return; the bot's
    `prices_60m` deque only retains 60 min of ticks, so we use a 1-hour
    proxy. This trades signal sharpness for zero data-pipeline changes.
    """
    if not prices or len(prices) < 30:
        return None
    end_ts, end_price = prices[-1]
    cutoff = end_ts - lookback_seconds
    start_price: Optional[float] = None
    for ts, p in prices:
        if ts >= cutoff:
            start_price = p
            break
    if start_price is None or start_price <= 0:
        return None
    return (end_price - start_price) / start_price


def is_event_day(event_calendar, now: Optional[float] = None,
                 window_hours: int = 24) -> bool:
    """
    True if any calendar event sits within +/- `window_hours` of `now`,
    even if not within the per-event hard-skip window.
    """
    import time
    if not getattr(event_calendar, "_events", None):
        return False
    current = now if now is not None else time.time()
    window_sec = window_hours * 3600
    for e in event_calendar._events:
        if abs(current - e["timestamp"]) <= window_sec:
            return True
    return False
