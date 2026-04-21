"""
intraday_signals.py -- Technical signals for mean-reversion BTC hourly strategy.

All functions operate on lists of (timestamp, value) tuples, or plain price lists.
All functions handle None/empty input gracefully.
"""

from __future__ import annotations
import math
from typing import Optional


# ---------------------------------------------------------------------------
# VWAP deviation signal
# ---------------------------------------------------------------------------

def vwap_deviation(
    prices: list,
    volumes: list,
) -> Optional[tuple]:
    """
    Compute VWAP and z-score of current price relative to VWAP.

    Args:
        prices:  list of (ts, price) tuples
        volumes: list of (ts, volume) tuples

    Returns:
        (vwap, deviation_zscore) where deviation_zscore = (current - vwap) / std_dev
        or None if insufficient data.
    """
    if not prices or not volumes or len(prices) < 5:
        return None

    # Build timestamp-aligned price/volume pairs
    price_map = {int(ts): p for ts, p in prices}
    vol_map   = {int(ts): v for ts, v in volumes}

    common_ts = sorted(set(price_map) & set(vol_map))
    if len(common_ts) < 5:
        return None

    # Compute VWAP = sum(price * volume) / sum(volume)
    total_pv  = 0.0
    total_vol = 0.0
    pv_vals   = []
    for ts in common_ts:
        p = price_map[ts]
        v = vol_map[ts]
        if v < 0:
            v = 0.0
        total_pv  += p * v
        total_vol += v
        pv_vals.append((p, v))

    if total_vol <= 0:
        # Fall back to simple mean if no volume
        prices_only = [price_map[ts] for ts in common_ts]
        vwap = sum(prices_only) / len(prices_only)
    else:
        vwap = total_pv / total_vol

    # Compute std deviation of prices around VWAP
    prices_only = [price_map[ts] for ts in common_ts]
    if len(prices_only) < 2:
        return None

    mean_p = sum(prices_only) / len(prices_only)
    var_p  = sum((p - mean_p) ** 2 for p in prices_only) / (len(prices_only) - 1)
    std_p  = math.sqrt(var_p) if var_p > 0 else None

    if std_p is None or std_p <= 0:
        return None

    current_price = prices_only[-1]
    deviation_zscore = (current_price - vwap) / std_p

    return (vwap, deviation_zscore)


# ---------------------------------------------------------------------------
# RSI signal
# ---------------------------------------------------------------------------

def rsi(prices: list, period: int = 14) -> Optional[float]:
    """
    Standard RSI on 1-min close prices.

    Args:
        prices: list of (ts, price) tuples
        period: RSI period (default 14)

    Returns:
        RSI value 0-100 or None if insufficient data.
    """
    if not prices or len(prices) < period + 2:
        return None

    price_vals = [p for _, p in prices]
    if len(price_vals) < period + 1:
        return None

    # Compute gains and losses
    changes = []
    for i in range(1, len(price_vals)):
        delta = price_vals[i] - price_vals[i - 1]
        changes.append(delta)

    if len(changes) < period:
        return None

    # Wilder smoothing: use simple average for first period, then exponential
    gains  = [max(0.0, c) for c in changes]
    losses = [max(0.0, -c) for c in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ---------------------------------------------------------------------------
# Momentum reversal timing
# ---------------------------------------------------------------------------

def momentum_reversal_signal(
    prices: list,
    elapsed_seconds: float,
    window_open_price: float,
) -> str:
    """
    Returns 'fade_up', 'fade_down', or 'neutral'.

    Logic: if elapsed > 2100s (35 min) AND price moved >0.4% in one direction
    from window open, expect a fade in the remaining time.

    Args:
        prices:            list of (ts, price) tuples (current window prices)
        elapsed_seconds:   seconds since window opened
        window_open_price: price at window open
    """
    if not prices or elapsed_seconds < 2100 or window_open_price <= 0:
        return "neutral"

    current_price = prices[-1][1] if isinstance(prices[-1], (list, tuple)) else prices[-1]

    if current_price <= 0:
        return "neutral"

    pct_move = (current_price - window_open_price) / window_open_price

    FADE_THRESHOLD = 0.004  # 0.4%

    if pct_move > FADE_THRESHOLD:
        return "fade_up"
    elif pct_move < -FADE_THRESHOLD:
        return "fade_down"
    else:
        return "neutral"


# ---------------------------------------------------------------------------
# Bollinger band signal
# ---------------------------------------------------------------------------

def bollinger_signal(
    prices: list,
    period: int = 20,
    num_std: float = 2.0,
) -> Optional[str]:
    """
    Bollinger band signal based on recent prices.

    Args:
        prices:  list of (ts, price) tuples
        period:  lookback period
        num_std: number of standard deviations for bands

    Returns:
        'above_upper', 'below_lower', or 'neutral', or None if insufficient data.
    """
    if not prices or len(prices) < period:
        return None

    price_vals = [p for _, p in prices[-period:]]
    if len(price_vals) < period:
        return None

    mean_p = sum(price_vals) / len(price_vals)
    var_p  = sum((p - mean_p) ** 2 for p in price_vals) / (len(price_vals) - 1)
    std_p  = math.sqrt(var_p) if var_p > 0 else 0.0

    if std_p <= 0:
        return "neutral"

    upper = mean_p + num_std * std_p
    lower = mean_p - num_std * std_p

    current = price_vals[-1]
    if current > upper:
        return "above_upper"
    elif current < lower:
        return "below_lower"
    else:
        return "neutral"
