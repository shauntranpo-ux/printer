from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Literal, Optional


@dataclass
class MarketFeatures:
    """
    All inputs a strategy might need to make a decision.
    Not every strategy uses every field. Strategies access only what they need.
    """
    # Identity
    asset: str                      # "BTC", "ETH", "SOL", "XRP", "DOGE"
    ticker: str                     # Kalshi market ticker
    timestamp: float                # unix seconds

    # Price state
    current_price: float            # current asset price in USD
    strike: float                   # contract strike price
    btc_price: float                # current BTC price (for cross-market features)

    # Time
    seconds_left: float             # seconds until window close
    elapsed_seconds: float          # seconds since window open

    # Kalshi orderbook
    yes_ask: float                  # best YES ask in cents (0-100)
    no_ask: float                   # best NO ask in cents (0-100)
    yes_bid: float                  # best YES bid in cents
    no_bid: float                   # best NO bid in cents
    spread_yes: float               # yes_ask - yes_bid in cents
    spread_no: float                # no_ask - no_bid in cents

    # Recent price history (per-asset, not just BTC)
    # Each deque stores (timestamp, price) tuples
    prices_1m: deque = field(default_factory=lambda: deque(maxlen=60))
    prices_5m: deque = field(default_factory=lambda: deque(maxlen=300))
    prices_60m: deque = field(default_factory=lambda: deque(maxlen=3600))

    # Contract velocity tracking
    kalshi_price_history: deque = field(default_factory=lambda: deque(maxlen=60))

    # Realized volatility (computed from prices_60m)
    realized_vol_1min: Optional[float] = None  # std of 1-min returns over last 60 min

    def window_fraction_remaining(self) -> float:
        """Tau = fraction of 15-min window remaining. Range (0, 1]."""
        total = self.seconds_left + self.elapsed_seconds
        if total <= 0:
            return 0.0
        return self.seconds_left / total


@dataclass
class Decision:
    """What every strategy returns from decide()."""
    action: Literal["trade", "skip"]
    side: Optional[Literal["yes", "no"]]    # None if action == "skip"
    p_model: float                          # 0.0 to 1.0, strategy's probability
                                            #   that YES contract wins at settlement
    reason: str                             # human-readable for logging
    contributing_signals: dict = field(default_factory=dict)
                                            # signal name -> value, for debugging
    expected_value: Optional[float] = None  # set by EVCalculator later
