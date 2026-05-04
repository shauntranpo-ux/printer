"""
Session clock for the X3 XRP strategy.

X3 thesis (see strategy plan, Part 2.5): when XRP is decoupled from BTC
(correlation < 0.35) AND the trading-session boundary is opening up Asia
or EU flow, prior-session price direction tends to *continue* over the
next 8-15 minutes rather than mean-revert. Korean/Japanese exchanges
(Upbit, Bithumb) follow US-session direction with a 30-90 s lag, and
news-catalysed continuation hit-rate runs 62-68%.

Sources:
- https://stoye.economics.cornell.edu/docs/Easley_ssrn-4814346.pdf
  (APAC XRP VPIN ~ 0.52 vs all-day ~ 0.45)
- https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4080253
  (Wen et al. 2022, intraday momentum/reversal cross-section)
- https://cryptorank.io/news/feed/72310-xrp-bnb-altcoins-losing-correlation-bitcoin
  (XRP <-> BTC correlation fell from ~80% to ~40% in 2025)

The decoupling windows below are slightly wider than the literal
session-open minute to absorb global timezone variability and DST.
"""

from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional


# Window definitions (UTC hour ranges, half-open intervals):
#   Asia open spike:  08:00-10:00 UTC
#   EU peak overlap:  14:00-16:00 UTC
DECOUPLING_WINDOWS_UTC = (
    (8, 10),
    (14, 16),
)


def is_decoupling_window(now: Optional[float] = None) -> tuple[bool, str]:
    """
    Return (is_active, label) for the X3 decoupling window check.

    Args:
        now: unix seconds; None uses current time.
    """
    import time
    ts = now if now is not None else time.time()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    hour = dt.hour
    for start, end in DECOUPLING_WINDOWS_UTC:
        if start <= hour < end:
            label = "asia_open" if start == 8 else "eu_peak"
            return True, label
    return False, ""


def prior_session_return(prices: list, lookback_seconds: int = 3600) -> Optional[float]:
    """
    Return the simple price return over the trailing `lookback_seconds`
    of the supplied (ts, price) series, used as a proxy for prior-session
    direction.

    NOTE: The X3 plan calls for a 4-hour US-session return; the bot's
    `prices_60m` deque only retains 60 min of ticks, so we use a 1-hour
    proxy. This trades signal sharpness for zero data-pipeline changes.
    Document this in the strategy docstring when the signal is used.
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

    Used as a soft boost for the X3 continuation signal.
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

