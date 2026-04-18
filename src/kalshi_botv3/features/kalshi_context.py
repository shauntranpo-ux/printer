"""Kalshi-specific context features.

All pure — no I/O, no network.
"""

from __future__ import annotations

from kalshi_botv3.kalshi.models import Orderbook


def kalshi_implied_prob(yes_price_cents: int) -> float:
    """Implied probability from a YES price in cents (1-99 -> 0.01-0.99)."""
    return yes_price_cents / 100.0


def kalshi_spread_cents(orderbook: Orderbook) -> int | None:
    """Effective spread in cents: (100 - best_no_bid) - best_yes_bid.

    In a binary market, the implied ask for YES = 100 - best NO bid.
    Returns None if either side is empty or spread is negative.
    """
    if not orderbook.yes or not orderbook.no:
        return None
    best_yes_bid = max(level.price for level in orderbook.yes)
    best_no_bid = max(level.price for level in orderbook.no)
    spread = (100 - best_no_bid) - best_yes_bid
    return int(spread) if spread >= 0 else None
