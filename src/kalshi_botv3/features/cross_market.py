"""Cross-market feature functions.

Read-only access to in-memory Aggregator buffers — no network, no side effects.
"""

from __future__ import annotations

import numpy as np

from kalshi_botv3.exchange.buffers import Aggregator
from kalshi_botv3.features.price import return_over


def btc_return(aggregator: Aggregator, minutes: int) -> float | None:
    """Simple return for BTC over the last `minutes` 1-minute bars."""
    if "BTC" not in aggregator:
        return None
    df = aggregator["BTC"].get_ohlcv()
    if df.empty:
        return None
    return return_over(df, minutes)


def eth_btc_ratio_zscore(aggregator: Aggregator, window_min: int = 60) -> float | None:
    """Z-score of the current ETH/BTC close ratio versus its `window_min`-bar history."""
    if "ETH" not in aggregator or "BTC" not in aggregator:
        return None
    eth_df = aggregator["ETH"].get_ohlcv(n=window_min)
    btc_df = aggregator["BTC"].get_ohlcv(n=window_min)
    if eth_df.empty or btc_df.empty:
        return None

    common = eth_df.index.intersection(btc_df.index)
    if len(common) < 2:
        return None

    eth_closes = eth_df["close"].loc[common].to_numpy(dtype=float)
    btc_closes = btc_df["close"].loc[common].to_numpy(dtype=float)
    ratio = eth_closes / btc_closes
    std = float(np.std(ratio, ddof=1))
    if std == 0:
        return None
    mean = float(np.mean(ratio))
    return float((ratio[-1] - mean) / std)
