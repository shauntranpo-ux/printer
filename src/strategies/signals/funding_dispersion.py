"""
S1 — Cross-venue funding-rate dispersion for SOL.

Plan source (Part 2.5): when SOL perp funding is materially higher on
Binance than on Hyperliquid (or vice versa), one venue's longs are
crowded relative to the other.  The literature reports Sharpe 0.80-1.66
on alts (incl. SOL) for funding-rate arbitrage strategies, vs -0.28 on
BTC, because alt funding markets are more fragmented.

Sources:
- Risk and return profiles of funding-rate arbitrage on CEX/DEX
  https://www.sciencedirect.com/science/article/pii/S2096720925000818
- Hyperliquid funding-comparison view (live data)
  https://app.hyperliquid.xyz/fundingComparison
- BitMEX 2025-Q3 derivatives report
  https://www.bitmex.com/blog/2025q3-derivatives-report

Honest scope:
- This module fetches Binance and Hyperliquid public REST APIs.  Drift
  is not included in v1; adding a third venue is straightforward via
  `register_venue_fetcher` but is left as future work.
- Funding rates are normalised to %/8h before comparison.  Hyperliquid
  publishes hourly funding; multiply by 8.  Binance publishes 8h
  funding directly.
- The bot.py background task should call `await monitor.refresh()` on
  ~60 s cadence.  Until that wiring lands, the monitor returns
  `current_dispersion() == None` and the strategy falls through with
  zero signal — graceful degradation, not a silent edge.
"""

from __future__ import annotations
import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional, Awaitable, Callable

try:
    import httpx
except ImportError:  # pragma: no cover - tests may stub
    httpx = None  # type: ignore


# Fire thresholds, in %/8h (decimal, e.g. 0.008 == 0.8%/8h).
S1_DISPERSION_FIRE_ABS = 0.008
S1_FUNDING_ADJ_MAX     = 0.06
# Cache freshness — stale data should not drive decisions.
S1_MAX_CACHE_AGE_SECS  = 180.0


@dataclass
class VenueFunding:
    venue: str
    rate_8h: float           # decimal, e.g. 0.001 == 0.1%/8h
    fetched_at: float        # unix seconds


VenueFetcher = Callable[[str], Awaitable[Optional[VenueFunding]]]


async def fetch_binance_funding(asset: str = "SOL") -> Optional[VenueFunding]:
    """Binance USDT-M perp 8 h funding rate (decimal)."""
    if httpx is None:
        return None
    symbol = f"{asset.upper()}USDT"
    url = "https://fapi.binance.com/fapi/v1/premiumIndex"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url, params={"symbol": symbol})
            r.raise_for_status()
            data = r.json()
        rate = float(data["lastFundingRate"])
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        return None
    return VenueFunding("binance", rate, time.time())


async def fetch_hyperliquid_funding(asset: str = "SOL") -> Optional[VenueFunding]:
    """
    Hyperliquid 1 h funding rate, scaled to 8 h for comparability.
    Endpoint returns `(meta, assetCtxs)`; we look up the asset by name.
    """
    if httpx is None:
        return None
    url = "https://api.hyperliquid.xyz/info"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(url, json={"type": "metaAndAssetCtxs"})
            r.raise_for_status()
            payload = r.json()
        meta, ctxs = payload[0], payload[1]
        names = [u["name"] for u in meta["universe"]]
        idx = names.index(asset.upper())
        funding_1h = float(ctxs[idx]["funding"])
    except (httpx.HTTPError, KeyError, ValueError, TypeError, IndexError):
        return None
    return VenueFunding("hyperliquid", funding_1h * 8.0, time.time())


class FundingDispersionMonitor:
    """
    Maintains the latest funding rate per venue for one asset.  Sync
    consumers call `current_dispersion()` to read the cached spread.
    Async background tasks call `refresh()` to repopulate the cache.

    Test path: pass `fetchers={...}` to inject deterministic stubs.
    """

    def __init__(
        self,
        asset: str = "SOL",
        fetchers: Optional[dict[str, VenueFetcher]] = None,
        max_age_secs: float = S1_MAX_CACHE_AGE_SECS,
    ):
        self.asset = asset
        self.max_age_secs = max_age_secs
        self._cache: dict[str, VenueFunding] = {}
        self._fetchers: dict[str, VenueFetcher] = fetchers or {
            "binance": fetch_binance_funding,
            "hyperliquid": fetch_hyperliquid_funding,
        }

    async def refresh(self) -> dict[str, VenueFunding]:
        coros = [f(self.asset) for f in self._fetchers.values()]
        results = await asyncio.gather(*coros, return_exceptions=True)
        for r in results:
            if isinstance(r, VenueFunding):
                self._cache[r.venue] = r
        return dict(self._cache)

    def inject(self, venue: str, rate_8h: float) -> None:
        """Test/dev helper: pre-populate without an HTTP call."""
        self._cache[venue] = VenueFunding(venue, rate_8h, time.time())

    def current_funding(self) -> dict[str, VenueFunding]:
        """Return only fresh entries."""
        now = time.time()
        return {
            v: f for v, f in self._cache.items()
            if (now - f.fetched_at) <= self.max_age_secs
        }

    def current_dispersion(self) -> Optional[float]:
        """
        Spread in %/8h (decimal): rate_binance - rate_hyperliquid.

        Returns None if either venue's cache is missing or stale.
        """
        fresh = self.current_funding()
        b = fresh.get("binance")
        h = fresh.get("hyperliquid")
        if b is None or h is None:
            return None
        return b.rate_8h - h.rate_8h


def funding_dispersion_adjustment(
    dispersion: Optional[float],
    fire_abs: float = S1_DISPERSION_FIRE_ABS,
    adj_magnitude: float = S1_FUNDING_ADJ_MAX,
) -> tuple[float, dict]:
    """
    Translate a venue spread into a p_yes nudge.

    Sign convention: positive spread (Binance > Hyperliquid) means longs
    are crowded on Binance → expect mean-reversion downward → negative
    p_yes nudge.  Symmetric on the other side.
    """
    info: dict = {"funding_dispersion": dispersion}
    if dispersion is None:
        info["funding_signal"] = "no_data"
        return 0.0, info
    if abs(dispersion) < fire_abs:
        info["funding_signal"] = "below_threshold"
        return 0.0, info
    info["funding_signal"] = "fire"
    return (-adj_magnitude if dispersion > 0 else +adj_magnitude), info
