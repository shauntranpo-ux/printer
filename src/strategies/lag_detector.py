"""
AMM Lag Detector — identifies when the Kalshi AMM hasn't repriced to reflect
recent BTC/asset price movement.

When the underlying asset moves significantly but the contract price barely
moves, the contract is temporarily mispriced. This creates a brief window to
trade at a better price before the AMM catches up.

Usage:
    signal, magnitude = amm_lag_signal(asset_prices, contract_history)
    # signal: "lag_yes" | "lag_no" | "neutral"
    # magnitude: 0.0–1.0, fraction of BTC's move that hasn't been repriced
"""

from __future__ import annotations

import time


def amm_lag_signal(
    asset_prices,
    contract_history,
    window_secs: int = 45,
    min_asset_move: float = 0.0015,
    lag_threshold: float = 0.50,
) -> tuple[str, float]:
    """
    Detect AMM repricing lag.

    asset_prices: deque of (timestamp, price) for the underlying asset.
    contract_history: deque of (timestamp, yes_ask_cents) for the contract.
    window_secs: lookback window in seconds.
    min_asset_move: minimum asset % move to trigger the check (0.003 = 0.3%).
    lag_threshold: contract must have captured less than this fraction of the
                   asset's move to count as lagging (0.35 = <35% repriced).

    Returns:
        ("lag_yes", magnitude)  — BTC rose, YES is underpriced (buy YES)
        ("lag_no",  magnitude)  — BTC fell, NO is underpriced (buy NO)
        ("neutral", 0.0)        — no meaningful lag detected
    """
    now = time.time()
    cutoff = now - window_secs

    # ── Asset price N seconds ago ─────────────────────────────────────────────
    btc_old = None
    for ts, p in asset_prices:
        if ts >= cutoff:
            btc_old = p
            break
    if btc_old is None or btc_old <= 0 or not asset_prices:
        return "neutral", 0.0
    btc_now = list(asset_prices)[-1][1]
    btc_pct = (btc_now - btc_old) / btc_old

    if abs(btc_pct) < min_asset_move:
        return "neutral", 0.0

    # ── Contract YES price N seconds ago ──────────────────────────────────────
    if not contract_history or len(contract_history) < 3:
        return "neutral", 0.0
    contract_old = None
    for ts, p in contract_history:
        if ts >= cutoff:
            contract_old = p
            break
    if contract_old is None or contract_old <= 0:
        return "neutral", 0.0
    contract_now = list(contract_history)[-1][1]
    contract_pct = (contract_now - contract_old) / contract_old

    # lag_ratio: fraction of the asset's directional move captured by the contract.
    # 1.0 = fully repriced, 0 = no repricing, negative = moved opposite.
    lag_ratio = contract_pct / btc_pct  # same sign = same direction

    if lag_ratio < lag_threshold:
        # Scale magnitude: 0 at lag_threshold, 1 at lag_ratio <= 0
        clamped = max(0.0, lag_ratio)
        magnitude = (lag_threshold - clamped) / lag_threshold
        magnitude = max(0.0, min(1.0, magnitude))
        if btc_pct > 0:
            return "lag_yes", magnitude  # asset rose, YES is cheap
        else:
            return "lag_no", magnitude   # asset fell, NO is cheap (YES is expensive)

    return "neutral", 0.0
