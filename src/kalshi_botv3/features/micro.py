"""Microstructure feature functions.

All pure — no I/O, no side effects.
"""

from __future__ import annotations

from datetime import timedelta

from kalshi_botv3.exchange.types import OrderbookSnapshot, Trade


def orderbook_imbalance(snapshot: OrderbookSnapshot, depth: int = 5) -> float | None:
    """(bid_volume - ask_volume) / (bid_volume + ask_volume) for top `depth` levels."""
    if not snapshot.bids or not snapshot.asks:
        return None
    bid_vol = sum(size for _, size in snapshot.bids[:depth])
    ask_vol = sum(size for _, size in snapshot.asks[:depth])
    total = bid_vol + ask_vol
    if total == 0:
        return None
    return (bid_vol - ask_vol) / total


def taker_buy_ratio(trades: list[Trade], lookback_seconds: float = 300.0) -> float | None:
    """Fraction of trade volume that was taker-buy over the last N seconds."""
    if not trades:
        return None
    cutoff = trades[-1].ts - timedelta(seconds=lookback_seconds)
    recent = [t for t in trades if t.ts >= cutoff]
    if not recent:
        return None
    total_vol = sum(t.size for t in recent)
    if total_vol == 0:
        return None
    buy_vol = sum(t.size for t in recent if t.side == "buy")
    return buy_vol / total_vol


def spread_bps(snapshot: OrderbookSnapshot) -> float | None:
    """Best spread in basis points: (ask - bid) / mid * 10_000."""
    if not snapshot.bids or not snapshot.asks:
        return None
    best_bid = snapshot.bids[0][0]
    best_ask = snapshot.asks[0][0]
    mid = (best_bid + best_ask) / 2.0
    if mid == 0:
        return None
    return (best_ask - best_bid) / mid * 10_000.0
