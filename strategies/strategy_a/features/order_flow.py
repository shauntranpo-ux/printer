from __future__ import annotations
import numpy as np
import pandas as pd
from collections import deque


def _ofi_at_depth(bids: list, asks: list, depth: int) -> float:
    bid_q = sum(q for _, q in bids[:depth])
    ask_q = sum(q for _, q in asks[:depth])
    total = bid_q + ask_q
    return (bid_q - ask_q) / total if total > 0 else 0.0


def _vamp(bids: list, asks: list) -> float:
    """Volume-Adjusted Mid-Price at top of book."""
    if not bids or not asks:
        return float("nan")
    bp, bq = bids[0]
    ap, aq = asks[0]
    denom = bq + aq
    return (bp * aq + ap * bq) / denom if denom > 0 else (bp + ap) / 2.0


class OrderFlowFeatures:
    """
    Online-computable microstructure features from L2 book snapshots + trade tape.
    All rolling windows use wall-clock time from trade timestamps.
    """

    _WINDOWS: dict[str, int] = {"1m": 60, "5m": 300, "15m": 900}

    def __init__(self, config: dict) -> None:
        ofc = config["order_flow"]
        self._depths: list[int] = ofc["ofi_depths"]
        raw_bucket = ofc.get("vpin_bucket_size")
        self._bucket_size: float = float(raw_bucket) if raw_bucket else 1000.0
        self._vpin_n: int = int(ofc["vpin_rolling_buckets"])
        self._trade_bufs: dict[str, deque] = {k: deque() for k in self._WINDOWS}
        self._vpin_buy = self._vpin_sell = self._vpin_vol = 0.0
        self._vpin_buckets: deque[float] = deque(maxlen=self._vpin_n)

    def _ingest(self, trades: list[dict]) -> None:
        for t in trades:
            ts = pd.Timestamp(t["timestamp"])
            if ts.tz is None:
                ts = ts.tz_localize("UTC")
            side = t["aggressor_side"]
            signed = t["size"] if side == "buy" else -t["size"] if side == "sell" else 0.0
            for buf in self._trade_bufs.values():
                buf.append((ts, signed))
            if side not in ("buy", "sell"):
                continue
            buy_vol  = t["size"] if side == "buy"  else 0.0
            sell_vol = t["size"] if side == "sell" else 0.0
            self._vpin_buy  += buy_vol
            self._vpin_sell += sell_vol
            self._vpin_vol  += t["size"]
            while self._vpin_vol >= self._bucket_size:
                # imbalance = |B - S| / V, guaranteed in [0, 1]
                imb = abs(self._vpin_buy - self._vpin_sell) / self._vpin_vol
                self._vpin_buckets.append(float(imb))
                # carry over the residual proportionally
                carry = self._vpin_vol - self._bucket_size
                if self._vpin_vol > 0:
                    ratio = carry / self._vpin_vol
                    self._vpin_buy  *= ratio
                    self._vpin_sell *= ratio
                else:
                    self._vpin_buy = self._vpin_sell = 0.0
                self._vpin_vol = carry

    def _purge(self, now: pd.Timestamp) -> None:
        if now.tz is None:
            now = now.tz_localize("UTC")
        for window, buf in self._trade_bufs.items():
            cutoff = now - pd.Timedelta(seconds=self._WINDOWS[window])
            while buf and buf[0][0] < cutoff:
                buf.popleft()

    def compute(self, data_window: dict) -> dict[str, float]:
        """
        data_window:
          book:   {timestamp, bids: [(price, size),...], asks: [(price, size),...]}
          trades: [{timestamp, price, size, aggressor_side}, ...]
        """
        book   = data_window.get("book", {})
        bids   = book.get("bids", [])
        asks   = book.get("asks", [])
        now    = pd.Timestamp(book.get("timestamp", pd.Timestamp.now("UTC")))
        self._ingest(data_window.get("trades", []))
        self._purge(now)

        out: dict[str, float] = {}
        for d in self._depths:
            out[f"ofi_l{d}"] = _ofi_at_depth(bids, asks, d)
        out["vamp"] = _vamp(bids, asks)
        for window, buf in self._trade_bufs.items():
            out[f"signed_flow_{window}"] = float(sum(v for _, v in buf))
        out["vpin"] = float(np.mean(list(self._vpin_buckets))) if self._vpin_buckets else 0.0
        if bids and asks:
            bp, bq = bids[0]
            ap, aq = asks[0]
            mid    = (bp + ap) / 2.0
            spread = ap - bp
            out["spread_abs"]  = float(spread)
            out["spread_bps"]  = float(spread / mid * 10_000) if mid > 0 else float("nan")
            out["depth_bid"]   = float(bq)
            out["depth_ask"]   = float(aq)
        else:
            out.update({"spread_abs": float("nan"), "spread_bps": float("nan"),
                        "depth_bid": float("nan"), "depth_ask": float("nan")})
        return out
