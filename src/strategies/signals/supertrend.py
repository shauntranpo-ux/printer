"""
Supertrend ATR-based trend direction indicator.

Returns 1 (uptrend / trade YES) or -1 (downtrend / trade NO) from synthetic
1-minute OHLCV bars built from the per-asset (timestamp, price) tick deque.

Pure function: identical tick data always produces identical output.
"""

from __future__ import annotations

from collections import deque
from typing import Optional


def _build_1m_ohlcv(prices_deque) -> list[tuple[float, float, float, float]]:
    """
    Convert (timestamp, price) ticks to 1-minute (open, high, low, close) bars.
    Returns bars sorted oldest-first. Each bar covers a 60-second bucket.
    """
    if not prices_deque:
        return []

    buckets: dict[int, list[float]] = {}
    for ts, p in prices_deque:
        key = int(ts) // 60
        if key not in buckets:
            buckets[key] = []
        buckets[key].append(p)

    bars = []
    for key in sorted(buckets.keys()):
        px = buckets[key]
        bars.append((px[0], max(px), min(px), px[-1]))
    return bars


def supertrend_direction(
    prices_deque: deque,
    atr_period: int = 10,
    atr_multiplier: float = 3.0,
    min_bars: Optional[int] = None,
) -> Optional[int]:
    """
    Compute Supertrend direction from tick price data.

    Returns 1 (uptrend → trade YES) or -1 (downtrend → trade NO).
    Returns None when fewer than (atr_period + 2) usable bars are available.

    Algorithm:
      1. Build 1-minute OHLCV bars from (timestamp, price) deque ticks.
      2. Compute True Range per bar: max(H-L, |H-prevC|, |L-prevC|).
      3. ATR = simple moving average of TR over atr_period bars.
      4. Upper band = HL2 + multiplier*ATR, lower band = HL2 - multiplier*ATR.
      5. Bands only tighten (upper can only drop, lower can only rise) while
         price stays on the same side.
      6. Trend flips when close crosses through the band for the current direction.
    """
    if min_bars is None:
        min_bars = atr_period + 2

    bars = _build_1m_ohlcv(prices_deque)
    n = len(bars)
    if n < min_bars:
        return None

    highs  = [b[1] for b in bars]
    lows   = [b[2] for b in bars]
    closes = [b[3] for b in bars]

    # True range; first bar has no previous close so use high-low only
    tr = [highs[0] - lows[0]]
    for i in range(1, n):
        h, lo, pc = highs[i], lows[i], closes[i - 1]
        tr.append(max(h - lo, abs(h - pc), abs(lo - pc)))

    # ATR via simple MA; None until enough bars accumulated
    atr: list[Optional[float]] = [None] * n
    for i in range(atr_period - 1, n):
        atr[i] = sum(tr[i - atr_period + 1: i + 1]) / atr_period

    start = atr_period - 1  # first bar with valid ATR

    hl2 = [(highs[i] + lows[i]) / 2.0 for i in range(n)]

    upper_final: list[Optional[float]] = [None] * n
    lower_final: list[Optional[float]] = [None] * n
    direction:   list[Optional[int]]   = [None] * n

    for i in range(start, n):
        av = atr[i]
        if av is None:
            continue

        raw_upper = hl2[i] + atr_multiplier * av
        raw_lower = hl2[i] - atr_multiplier * av

        if i == start:
            upper_final[i] = raw_upper
            lower_final[i] = raw_lower
            direction[i]   = 1 if closes[i] >= hl2[i] else -1
            continue

        pu = upper_final[i - 1]
        pl = lower_final[i - 1]
        pd = direction[i - 1]

        # Upper band: step down only when previous close was below it
        if pu is not None:
            upper_final[i] = (
                min(raw_upper, pu) if closes[i - 1] <= pu else raw_upper
            )
        else:
            upper_final[i] = raw_upper

        # Lower band: step up only when previous close was above it
        if pl is not None:
            lower_final[i] = (
                max(raw_lower, pl) if closes[i - 1] >= pl else raw_lower
            )
        else:
            lower_final[i] = raw_lower

        # Flip logic: flip only on close, not on wick
        if pd == -1 and closes[i] > upper_final[i]:
            direction[i] = 1
        elif pd == 1 and closes[i] < lower_final[i]:
            direction[i] = -1
        else:
            direction[i] = pd

    # Return the most recent computed direction
    for i in range(n - 1, start - 1, -1):
        if direction[i] is not None:
            return direction[i]
    return None
