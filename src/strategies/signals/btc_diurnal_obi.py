"""B3 — Kalshi binary-book OBI proxy."""

from __future__ import annotations
from typing import Optional


def kalshi_book_obi(
    yes_bid_c: float,
    no_bid_c: float,
    yes_ask_c: float,
    no_ask_c: float,
) -> Optional[float]:
    """
    Directional pressure proxy for OBI on [-1, 1].

    Positive = up-side pressure (YES priced higher than NO).
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
