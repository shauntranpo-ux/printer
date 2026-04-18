"""
Simulated Kalshi orderbook for backtesting.

Kalshi's real orderbook reflects:
  1. Theoretical fair value based on spot price + realized vol
  2. Market-maker inventory skew
  3. Retail directional flow
  4. Spread widening around events

We can't perfectly reproduce (2)-(4) from historical spot data alone.
We model the orderbook as fair_value +/- noise, calibrated so simulated
spreads match the empirical spreads observed in the live data snapshot.

Conservative approach: widen spreads in the simulator vs reality.
This biases backtest results DOWN — a strategy that's profitable in
the simulator is likely to be at least as profitable live.
"""

from __future__ import annotations
import math
import random
from typing import NamedTuple, Optional

from strategies.baseline import brownian_bridge_prob_above


class SimulatedOrderbook(NamedTuple):
    yes_ask: float   # cents (0-100)
    yes_bid: float
    no_ask: float
    no_bid: float


# Empirical bid/ask spread parameters per asset. Conservative (wider than
# real Kalshi spreads) to bias backtest results DOWN.
DEFAULT_SPREAD_CENTS = {
    "BTC":  2.0,
    "ETH":  2.0,
    "SOL":  3.0,
    "XRP":  3.0,
    "DOGE": 3.0,
}

DEFAULT_NOISE_CENTS = {
    "BTC":  1.0,
    "ETH":  1.0,
    "SOL":  1.5,
    "XRP":  2.0,
    "DOGE": 2.0,
}


def simulate_orderbook(
    current_price: float,
    strike: float,
    seconds_left: float,
    realized_vol_1min: float,
    asset: str = "BTC",
    rng: Optional[random.Random] = None,
    seed: Optional[int] = None,
) -> SimulatedOrderbook:
    """
    Produce a simulated Kalshi orderbook consistent with the underlying
    fair value.
    """
    if rng is None:
        rng = random.Random(seed) if seed is not None else random.Random()

    p_above = brownian_bridge_prob_above(
        current_price, strike, seconds_left, realized_vol_1min
    )

    fair_yes_cents = p_above * 100.0

    noise_std = DEFAULT_NOISE_CENTS.get(asset, 2.0)
    fair_yes_cents_noisy = fair_yes_cents + rng.gauss(0, noise_std)
    fair_yes_cents_noisy = max(2.0, min(98.0, fair_yes_cents_noisy))

    half_spread = DEFAULT_SPREAD_CENTS.get(asset, 2.5) / 2.0
    yes_bid = max(1.0, fair_yes_cents_noisy - half_spread)
    yes_ask = min(99.0, fair_yes_cents_noisy + half_spread)

    no_ask = max(1.0, min(99.0, (100.0 - yes_bid) + rng.gauss(0, noise_std * 0.3)))
    no_bid = max(1.0, min(99.0, (100.0 - yes_ask) - abs(rng.gauss(0, noise_std * 0.3))))

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
