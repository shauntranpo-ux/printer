"""
Builds MarketFeatures from the running bot's state.

Bridges the legacy bot.py module-level globals and the new dataclass-based
feature pipeline. The new strategies never import from bot.py except through
this adapter.
"""

from __future__ import annotations

import math
import time
from collections import deque

from strategies.features import MarketFeatures


def build_features_from_bot_state(
    asset: str,
    ticker: str,
    current_price: float,
    strike: float,
    btc_price: float,
    seconds_left: float,
    elapsed_seconds: float,
    yes_ask: float,
    no_ask: float,
    yes_bid: float,
    no_bid: float,
    prices_deque,
    contract_history=None,
) -> MarketFeatures:
    """
    Construct a MarketFeatures from whatever state the bot has available.

    prices_deque: the bot's per-asset price deque. We copy references, not
                  deep-copy, because the strategy reads but does not mutate.
    """
    spread_yes = max(0.0, yes_ask - yes_bid)
    spread_no = max(0.0, no_ask - no_bid)

    rv = _realized_vol_1min(prices_deque)

    now = time.time()
    prices_1m = deque(
        [(ts, p) for ts, p in prices_deque if ts >= now - 60],
        maxlen=60,
    )
    prices_5m = deque(
        [(ts, p) for ts, p in prices_deque if ts >= now - 300],
        maxlen=300,
    )
    prices_60m = deque(
        [(ts, p) for ts, p in prices_deque if ts >= now - 3600],
        maxlen=3600,
    )

    kalshi_hist = deque(maxlen=60)
    if contract_history is not None:
        for item in contract_history:
            kalshi_hist.append(item)

    return MarketFeatures(
        asset=asset,
        ticker=ticker,
        timestamp=now,
        current_price=current_price,
        strike=strike,
        btc_price=btc_price,
        seconds_left=seconds_left,
        elapsed_seconds=elapsed_seconds,
        yes_ask=yes_ask,
        no_ask=no_ask,
        yes_bid=yes_bid,
        no_bid=no_bid,
        spread_yes=spread_yes,
        spread_no=spread_no,
        prices_1m=prices_1m,
        prices_5m=prices_5m,
        prices_60m=prices_60m,
        kalshi_price_history=kalshi_hist,
        realized_vol_1min=rv,
    )


def _realized_vol_1min(prices_deque) -> float | None:
    """
    Std dev of 1-minute log returns over the last 60 minutes.
    Mirrors the legacy bot.btc_realized_vol() formula.
    """
    if not prices_deque or len(prices_deque) < 4:
        return None

    now = time.time()
    cutoff = now - 3600
    samples = [(ts, p) for ts, p in prices_deque if ts >= cutoff]
    if len(samples) < 4:
        return None

    # Resample to 1-min buckets (latest price per bucket)
    bucketed: dict[int, float] = {}
    for ts, p in samples:
        bucket = int(ts // 60)
        bucketed[bucket] = p

    sorted_prices = [bucketed[k] for k in sorted(bucketed.keys())]
    if len(sorted_prices) < 4:
        return None

    log_returns = []
    for i in range(1, len(sorted_prices)):
        if sorted_prices[i - 1] > 0 and sorted_prices[i] > 0:
            log_returns.append(math.log(sorted_prices[i] / sorted_prices[i - 1]))

    if len(log_returns) < 3:
        return None

    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
    return variance ** 0.5
