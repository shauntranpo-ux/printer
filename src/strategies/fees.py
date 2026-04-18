"""
Kalshi's exact fee formula, confirmed from Kalshi fee schedule (Feb 2026).

Taker fee:  ceil(0.07 * contracts * price * (1 - price))  in dollars
Maker fee:  ceil(0.0175 * contracts * price * (1 - price)) in dollars

where price is in dollars (0.0 to 1.0), NOT cents.

Fees peak at price=0.5 and drop toward the tails.
"""

from __future__ import annotations

import math


def taker_fee(contracts: int, price_dollars: float) -> float:
    """
    Fee for taking liquidity (market order).

    Args:
        contracts: integer number of contracts
        price_dollars: contract price as fraction 0.0 to 1.0

    Returns:
        Fee in dollars (rounded up to nearest cent)
    """
    if contracts <= 0:
        return 0.0
    raw = 0.07 * contracts * price_dollars * (1.0 - price_dollars)
    # Round up to nearest cent
    return math.ceil(raw * 100) / 100.0


def maker_fee(contracts: int, price_dollars: float) -> float:
    """
    Fee for providing liquidity (resting limit order that gets filled).
    """
    if contracts <= 0:
        return 0.0
    raw = 0.0175 * contracts * price_dollars * (1.0 - price_dollars)
    return math.ceil(raw * 100) / 100.0


def fee_as_pct_of_stake(
    contracts: int,
    price_dollars: float,
    taker: bool = True,
) -> float:
    """
    Returns fee as a percentage of stake (contracts * price).
    Useful for quick EV sanity checks.
    """
    if contracts <= 0 or price_dollars <= 0:
        return 0.0
    stake = contracts * price_dollars
    fee = taker_fee(contracts, price_dollars) if taker else maker_fee(contracts, price_dollars)
    return fee / stake
