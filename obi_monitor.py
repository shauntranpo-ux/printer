"""
obi_monitor.py — Real-time Order Book Imbalance via Coinbase Exchange WebSocket.

Connects to wss://ws-feed.exchange.coinbase.com and subscribes to the
level2_batch channel (public, no auth) for BTC-USD and ETH-USD.

OBI = (bid_depth - ask_depth) / (bid_depth + ask_depth)  — top N levels.
Positive OBI → buy-side pressure; negative → sell-side pressure.

Usage in bot.py:
    monitor = OBIMonitor(["BTC", "ETH"])
    asyncio.create_task(monitor.run())
    ...
    obi = monitor.get_obi("BTC")   # float in [-1, 1] or None if stale/unavailable
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Optional

log = logging.getLogger("obi_monitor")

_ASSET_TO_PRODUCT: dict[str, str] = {
    "BTC":  "BTC-USD",
    "ETH":  "ETH-USD",
    "SOL":  "SOL-USD",
    "XRP":  "XRP-USD",
    "DOGE": "DOGE-USD",
}

_WS_URL = "wss://ws-feed.exchange.coinbase.com"
_STALE_SECONDS = 30
_TOP_LEVELS = 10
_RECONNECT_DELAY = 5


class OBIMonitor:
    def __init__(self, assets: list[str]) -> None:
        self.assets = [a for a in assets if a in _ASSET_TO_PRODUCT]
        self._books: dict[str, dict] = {a: {"bids": {}, "asks": {}} for a in self.assets}
        self._obi: dict[str, Optional[float]] = {a: None for a in self.assets}
        self._last_update: dict[str, float] = {a: 0.0 for a in self.assets}

    def get_obi(self, asset: str) -> Optional[float]:
        """Return OBI for asset, or None if feed is stale (>30s)."""
        age = time.time() - self._last_update.get(asset, 0.0)
        if age > _STALE_SECONDS:
            return None
        return self._obi.get(asset)

    def _compute_obi(self, asset: str) -> None:
        book = self._books[asset]
        bids = sorted(book["bids"].items(), key=lambda x: -float(x[0]))[:_TOP_LEVELS]
        asks = sorted(book["asks"].items(), key=lambda x: float(x[0]))[:_TOP_LEVELS]
        bid_depth = sum(float(sz) for _, sz in bids)
        ask_depth = sum(float(sz) for _, sz in asks)
        total = bid_depth + ask_depth
        if total < 1e-9:
            self._obi[asset] = None
        else:
            self._obi[asset] = (bid_depth - ask_depth) / total
        self._last_update[asset] = time.time()

    def _apply_snapshot(self, asset: str, bids: list, asks: list) -> None:
        self._books[asset]["bids"] = {price: size for price, size in bids}
        self._books[asset]["asks"] = {price: size for price, size in asks}
        self._compute_obi(asset)

    def _apply_changes(self, asset: str, changes: list) -> None:
        book = self._books[asset]
        for side, price, size in changes:
            target = book["bids"] if side == "buy" else book["asks"]
            if float(size) == 0:
                target.pop(price, None)
            else:
                target[price] = size
        self._compute_obi(asset)

    async def run(self) -> None:
        """Run forever with reconnection. Call as asyncio.create_task(monitor.run())."""
        products = [_ASSET_TO_PRODUCT[a] for a in self.assets]
        product_to_asset = {v: k for k, v in _ASSET_TO_PRODUCT.items()}

        while True:
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(_WS_URL, heartbeat=20) as ws:
                        subscribe_msg = {
                            "type": "subscribe",
                            "product_ids": products,
                            "channels": ["level2_batch"],
                        }
                        await ws.send_str(json.dumps(subscribe_msg))
                        log.info(f"OBI monitor connected for {products}")

                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                continue
                            try:
                                data = json.loads(msg.data)
                            except Exception:
                                continue

                            msg_type = data.get("type")
                            product_id = data.get("product_id", "")
                            asset = product_to_asset.get(product_id)
                            if asset is None:
                                continue

                            if msg_type == "snapshot":
                                self._apply_snapshot(
                                    asset,
                                    data.get("bids", []),
                                    data.get("asks", []),
                                )
                            elif msg_type == "l2update":
                                self._apply_changes(asset, data.get("changes", []))

            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.warning(f"OBI monitor disconnected: {exc} — reconnecting in {_RECONNECT_DELAY}s")
                await asyncio.sleep(_RECONNECT_DELAY)
