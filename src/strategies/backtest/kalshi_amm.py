"""
Simulated Kalshi orderbook for backtesting.

Deterministic pricing: yes_ask = p_above * 100 + half_spread
No Gaussian noise — edge must come from signals, not noise artifacts.
Spread is conservative (wider than real Kalshi) to bias results DOWN.
"""

from __future__ import annotations
import math
from typing import NamedTuple, Optional

from strategies.baseline import brownian_bridge_prob_above


class SimulatedOrderbook(NamedTuple):
    yes_ask: float   # cents (0-100)
    yes_bid: float
    no_ask: float
    no_bid: float


# Half-spread per asset in cents. Conservative (wider than live Kalshi data).
# Real Kalshi spread on active 15-min markets is ~2c; we use 3c total = 1.5c half.
HALF_SPREAD_CENTS = {
    "BTC":  1.5,
    "ETH":  1.5,
    "SOL":  2.0,
    "XRP":  2.0,
    "DOGE": 2.0,
}


def simulate_orderbook(
    current_price: float,
    strike: float,
    seconds_left: float,
    realized_vol_1min: float,
    asset: str = "BTC",
    rng=None,       # ignored — kept for API compatibility
    seed=None,      # ignored — deterministic pricing
) -> SimulatedOrderbook:
    """
    Produce a deterministic simulated Kalshi orderbook.

    yes_ask = p_above * 100 + half_spread
    no_ask  = (1 - p_above) * 100 + half_spread
    Both clamped to [2, 98].

    No Gaussian noise. Edge must come entirely from signals,
    not from noise artifacts in the orderbook simulator.
    """
    p_above = brownian_bridge_prob_above(
        current_price, strike, seconds_left, realized_vol_1min
    )
    p_above = max(0.01, min(0.99, p_above))

    half_spread = HALF_SPREAD_CENTS.get(asset, 1.5)

    yes_ask = p_above * 100.0 + half_spread
    yes_bid = p_above * 100.0 - half_spread
    no_ask  = (1.0 - p_above) * 100.0 + half_spread
    no_bid  = (1.0 - p_above) * 100.0 - half_spread

    yes_ask = max(2.0, min(98.0, yes_ask))
    yes_bid = max(1.0, min(97.0, yes_bid))
    no_ask  = max(2.0, min(98.0, no_ask))
    no_bid  = max(1.0, min(97.0, no_bid))

    # Enforce bid < ask
    if yes_ask <= yes_bid:
        yes_ask = yes_bid + 0.5
    if no_ask <= no_bid:
        no_ask = no_bid + 0.5

    return SimulatedOrderbook(
        yes_ask=round(yes_ask, 2),
        yes_bid=round(yes_bid, 2),
        no_ask=round(no_ask, 2),
        no_bid=round(no_bid, 2),
    )
