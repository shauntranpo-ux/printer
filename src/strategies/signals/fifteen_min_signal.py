"""
D3-hybrid 15-minute signal — momentum-continuation ensemble.

Mean-reversion thesis rejected by live data (14.5% win rate on S2).
V2–V5 now vote WITH momentum (continuation), not against it.

Direction voters (3-of-5 required):
  V1  BS p_yes > 0.5        (BTC only; ETH/SOL/XRP IC near zero)
  V2  MTF momentum > +T     → YES  (upward momentum, expect continuation)
  V3  RSI dev > +T          → YES  (RSI above midline, bullish momentum)
  V4  Bollinger z > +T      → YES  (ETH/SOL/XRP only; above band, bullish)
  V5  MTF magnitude > +T/2  → YES  (soft confirmation of V2)

Returns (side, raw_p_yes, vote_count) or None if fewer than 3 voters agree.
"""
from __future__ import annotations

import math
from typing import Optional

from strategies.features import MarketFeatures
from strategies.signals.black_scholes import compute_bs_p_yes

# Per-asset thresholds from real-data IC sweep (backtesting/research/sweep_results_real.md)
_MTF_THRESHOLDS: dict[str, float] = {
    "BTC": 0.0005,
    "ETH": 0.0005,
    "SOL": 0.0005,
    "XRP": 0.0005,
}
_RSI_THRESHOLDS: dict[str, float] = {
    "BTC": 5.0,
    "ETH": 8.0,
    "SOL": 10.0,
    "XRP": 8.0,
}
_BOLL_THRESHOLDS: dict[str, float] = {
    "BTC": 0.75,
    "ETH": 0.50,
    "SOL": 0.50,
    "XRP": 0.35,
}
_MTF_DEFAULT  = 0.0005
_RSI_DEFAULT  = 8.0
_BOLL_DEFAULT = 0.50

# V1 (BS p_yes) is only informative for BTC — real-IC sweep shows IC near zero
# or negative for ETH/SOL/XRP, so it abstains rather than adding noise.
_V1_ASSETS: frozenset[str] = frozenset({"BTC"})

# V4 Bollinger abstains for BTC — IC=0.005, t=0.33 (FAIL) across both ternary
# and continuous evaluations. Passes for ETH/SOL/XRP (t=3.5–3.9).
_V4_SKIP_ASSETS: frozenset[str] = frozenset({"BTC"})


# ── feature helpers ────────────────────────────────────────────────────────────

def _rsi(prices: list[float], period: int = 14) -> Optional[float]:
    if len(prices) < period + 2:
        return None
    changes = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains  = [max(0.0, c) for c in changes]
    losses = [max(0.0, -c) for c in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _boll_zscore(prices: list[float], period: int = 20) -> Optional[float]:
    if len(prices) < period:
        return None
    recent = prices[-period:]
    mean_p = sum(recent) / len(recent)
    var_p  = sum((p - mean_p) ** 2 for p in recent) / (len(recent) - 1)
    std_p  = math.sqrt(var_p) if var_p > 0 else 0.0
    if std_p <= 0:
        return None
    return (prices[-1] - mean_p) / std_p


def _multi_tf_mom(prices: list[float]) -> Optional[float]:
    if len(prices) < 31:
        return None
    cur = prices[-1]
    if cur <= 0:
        return None
    r5  = (cur - prices[-6])  / prices[-6]  if prices[-6]  > 0 else 0.0
    r15 = (cur - prices[-16]) / prices[-16] if prices[-16] > 0 else 0.0
    r30 = (cur - prices[-31]) / prices[-31] if prices[-31] > 0 else 0.0
    return (r5 + r15 + r30) / 3.0


# ── public API ─────────────────────────────────────────────────────────────────

def compute_15m_signal(
    features: MarketFeatures,
) -> Optional[tuple[str, float, int]]:
    """
    Returns (side, raw_p_yes, vote_count) or None if 3-of-5 vote cannot be reached.

    vote_count is the number of voters agreeing with the returned side (3–5).
    raw_p_yes is the un-calibrated BS probability; caller calibrates before EV gate.
    """
    prices_60m = list(features.prices_60m)
    if len(prices_60m) < 32:
        return None

    prices = [p for _, p in prices_60m]

    bs_p = compute_bs_p_yes(
        features.current_price,
        features.strike,
        features.realized_vol_1min or 0.0,
        features.seconds_left,
    )
    if bs_p is None:
        return None

    asset  = features.asset.upper()
    mtf_T  = _MTF_THRESHOLDS.get(asset, _MTF_DEFAULT)
    rsi_T  = _RSI_THRESHOLDS.get(asset, _RSI_DEFAULT)
    boll_T = _BOLL_THRESHOLDS.get(asset, _BOLL_DEFAULT)

    mtf     = _multi_tf_mom(prices)
    rsi_val = _rsi(prices)
    rsi_dev = (float(rsi_val) - 50.0) if rsi_val is not None else None
    boll    = _boll_zscore(prices)

    # V1: only predictive for BTC (ETH/SOL/XRP IC near zero / negative per real-IC sweep)
    v1 = (+1 if bs_p > 0.5 else -1) if asset in _V1_ASSETS else 0

    # V2–V5: momentum-following — vote WITH trend (mean-reversion failed live; switched to continuation)
    v2 = (+1 if mtf is not None and float(mtf) >  mtf_T else
          (-1 if mtf is not None and float(mtf) < -mtf_T else 0))

    v3 = (+1 if rsi_dev is not None and rsi_dev >  rsi_T else
          (-1 if rsi_dev is not None and rsi_dev < -rsi_T else 0))

    v4 = (0 if asset in _V4_SKIP_ASSETS else
          (+1 if boll is not None and float(boll) >  boll_T else
           (-1 if boll is not None and float(boll) < -boll_T else 0)))

    v5 = (+1 if mtf is not None and abs(float(mtf)) > mtf_T / 2 and float(mtf) >  0 else
          (-1 if mtf is not None and abs(float(mtf)) > mtf_T / 2 and float(mtf) < 0 else 0))

    yes_votes = sum(1 for v in [v1, v2, v3, v4, v5] if v == +1)
    no_votes  = sum(1 for v in [v1, v2, v3, v4, v5] if v == -1)

    if yes_votes >= 3:
        side = "yes"
    elif no_votes >= 3:
        side = "no"
    else:
        return None

    vote_count = yes_votes if side == "yes" else no_votes
    return (side, bs_p, vote_count)
