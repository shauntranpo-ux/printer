"""
B3 — BTC time-of-day-conditioned order-book-imbalance signal.

Plan source: BTC depth at 10 bps peaks ~ 11:00 UTC (Asia + EU overlap)
and troughs ~ 21:00 UTC.  Order-book imbalance signals are predictive
only when depth is high.  Time-of-day-filtered intraday trend systems
report Sharpe ~ 1.6 (Concretum, 2018-2025).

Sources:
- https://blog.amberdata.io/the-rhythm-of-liquidity-temporal-patterns-in-market-depth
- https://concretumgroup.com/seasonality-in-bitcoin-intraday-trend-trading/
- https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4080253

Honest scope: the bot does not currently consume L2 depth at 10 bps.
This module uses the Kalshi orderbook itself (yes/no bid-ask) as a
binary OBI proxy.  It captures directional pressure on the contract
book, which is correlated with — but not identical to — spot OBI.
"""

from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional


# Time-of-day bands (UTC hours, half-open intervals)
PEAK_BANDS = (
    (10, 12),   # Asia + EU overlap
    (22, 24),   # Asia open / late-US
)
TROUGH_BAND = (18, 21)   # Late-EU dead zone

# Binance-style funding reset times (UTC). Avoid trading +/- 5 min around them.
FUNDING_RESET_HOURS = (0, 8, 16)
FUNDING_RESET_GUARD_MIN = 5


def current_btc_diurnal_band(now: Optional[float] = None) -> str:
    """Return 'peak', 'trough', or 'neutral'."""
    import time
    ts = now if now is not None else time.time()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    hour = dt.hour
    for start, end in PEAK_BANDS:
        if start <= hour < end:
            return "peak"
    if TROUGH_BAND[0] <= hour < TROUGH_BAND[1]:
        return "trough"
    return "neutral"


def is_funding_reset_window(now: Optional[float] = None) -> bool:
    """True within +/- FUNDING_RESET_GUARD_MIN of any Binance funding reset."""
    import time
    ts = now if now is not None else time.time()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    minute_of_day = dt.hour * 60 + dt.minute
    for h in FUNDING_RESET_HOURS:
        center = h * 60
        if center == 0:
            # Wrap-around: 23:55-23:59 + 00:00-00:05
            if minute_of_day >= (24 * 60 - FUNDING_RESET_GUARD_MIN):
                return True
            if minute_of_day <= FUNDING_RESET_GUARD_MIN:
                return True
        elif abs(minute_of_day - center) <= FUNDING_RESET_GUARD_MIN:
            return True
    return False


def kalshi_book_obi(
    yes_bid_c: float,
    no_bid_c: float,
    yes_ask_c: float,
    no_ask_c: float,
) -> Optional[float]:
    """
    Binary-book directional pressure proxy for OBI.

    The Kalshi orderbook is itself a directional book: YES is the
    "above-strike" leg.  When YES bid sits well above the NO bid, market
    participants are paying up to be long the up-side.  We measure that
    asymmetry on a [-1, 1] scale where positive = up-side pressure.

    Mid-price normalisation removes the 100c sum constraint (yes + no
    asks ~ 100 + spread) so the metric is purely directional.

    Returns None if any quote is missing/zero or the book is crossed.
    """
    yb, nb, ya, na = yes_bid_c, no_bid_c, yes_ask_c, no_ask_c
    if min(yb, nb, ya, na) <= 0:
        return None
    if ya <= yb or na <= nb:
        return None

    yes_mid = (yb + ya) / 2.0
    no_mid = (nb + na) / 2.0
    total = yes_mid + no_mid
    if total <= 0:
        return None
    return (yes_mid - no_mid) / total


def b3_obi_adjustment(
    obi: Optional[float],
    band: str,
    funding_reset: bool,
    obi_threshold: float = 0.04,
    adj_magnitude: float = 0.04,
) -> tuple[float, dict]:
    """
    Map (OBI, time-of-day band, funding-reset flag) into a p_yes nudge.

    Rules:
    - Trough or funding-reset window → adj = 0 (the strategy itself should
      skip; this is a safety net if it doesn't).
    - OBI missing or |OBI| below threshold → adj = 0.
    - Peak band → full adj_magnitude in OBI sign.
    - Neutral band → half adj_magnitude (degraded liquidity).
    """
    info = {
        "diurnal_band": band,
        "funding_reset": funding_reset,
        "obi": obi,
        "obi_used": False,
    }
    if funding_reset or band == "trough":
        return 0.0, info
    if obi is None or abs(obi) < obi_threshold:
        return 0.0, info

    multiplier = 1.0 if band == "peak" else 0.5
    adj = (adj_magnitude * multiplier) if obi > 0 else -(adj_magnitude * multiplier)
    info["obi_used"] = True
    info["obi_multiplier"] = multiplier
    return adj, info

