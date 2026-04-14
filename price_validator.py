#!/usr/bin/env python3
"""
price_validator.py — Validates the backtest AMM price simulator against real Kalshi prices.

The backtest uses simulate_amm_prices() to generate synthetic YES/NO asks based on
BTC distance from strike. The quant review flagged a potential 8-15c gap between
simulated and real prices. This script measures that gap over 200+ samples.

How to use:
    python price_validator.py

Output:
    Appends rows to price_validation_log.csv every ~60 seconds.
    Prints a running summary every 50 samples.
    Run for a few hours to collect 200+ samples, then analyze the CSV.

What to look for in the CSV:
    price_gap = real_yes_ask - simulated_yes_ask
    - If avg gap > +8c: real prices are much more expensive than simulated.
      The backtest is using prices that are too cheap, inflating EV estimates.
      Strategy is probably not viable as-is.
    - If avg gap 3-8c: moderate discrepancy. EV estimates are somewhat optimistic.
      Worth adjusting the simulator or raising min_ev threshold further.
    - If avg gap < 3c: simulator is reasonably accurate. Backtest results are credible.

Stop when: price_validation_log.csv has 200+ rows with non-null real prices.
"""

import asyncio
import csv
import json
import os
import random
import sys
import time
from base64 import b64encode
from datetime import datetime, timezone

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ── Config ────────────────────────────────────────────────────────────────────
KALSHI_BASE_URL    = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_PATH_PREFIX = "/trade-api/v2"
COINBASE_REST_URL  = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
OUTPUT_CSV         = "price_validation_log.csv"
POLL_INTERVAL      = 60   # seconds between samples
API_TIMEOUT        = 10   # seconds

_SERIES_SEARCH_ORDER = ("KXBTCD", "BTCD-B", "KXBTC15M", "BTC15M", "KXBTC")

_rng = random.Random(42)

# ── Auth ──────────────────────────────────────────────────────────────────────

_api_key: str = ""
_private_key = None


def _load_credentials() -> None:
    global _api_key, _private_key
    _api_key = os.environ.get("KALSHI_API_KEY", "").strip()
    pem_val  = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()
    if not _api_key or not pem_val:
        print("ERROR: KALSHI_API_KEY and KALSHI_PRIVATE_KEY must be set.")
        sys.exit(1)
    pem_bytes = open(pem_val, "rb").read() if os.path.exists(pem_val) else pem_val.encode()
    _private_key = serialization.load_pem_private_key(pem_bytes, password=None)


def _kalshi_headers(method: str, path: str) -> dict:
    ts       = str(int(time.time() * 1000))
    full_path = KALSHI_PATH_PREFIX + path
    msg      = (ts + method.upper() + full_path).encode()
    sig      = b64encode(
        _private_key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    ).decode()
    return {
        "KALSHI-ACCESS-KEY":       _api_key,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Content-Type": "application/json",
    }


# ── AMM price simulator (deterministic midpoint, no random noise) ─────────────

def _simulate_midpoint(btc_price: float, strike: float) -> tuple[float, float]:
    """
    Deterministic midpoint of backtest.py's simulate_amm_prices().
    Uses the center of each distance band and fixed spread = 4.5c (midpoint of 3-6c).
    This is what the backtest 'expects' Kalshi to price the contract at.
    """
    pct    = (btc_price - strike) / strike
    ap     = abs(pct) * 100
    above  = pct > 0
    spread = 4.5  # midpoint of backtest's 3.0–6.0 spread

    if ap < 0.10:
        yes_ask = 51.5 if above else 48.5       # midpoint of 49-54 / 46-51
    elif ap < 0.30:
        yes_ask = 68.5 if above else 31.5       # midpoint of 62-75
    else:
        yes_ask = 84.5 if above else 15.5       # midpoint of 77-92

    yes_ask = max(3.0, min(97.0, yes_ask))
    no_ask  = max(3.0, min(97.0, 100.0 + spread - yes_ask))
    return yes_ask, no_ask


# ── Fetchers ──────────────────────────────────────────────────────────────────

async def _fetch_btc(session: aiohttp.ClientSession) -> float | None:
    try:
        async with session.get(
            COINBASE_REST_URL,
            timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return float(data["data"]["amount"])
    except Exception as exc:
        print(f"[validator] BTC fetch error: {exc}")
    return None


async def _fetch_markets(session: aiohttp.ClientSession) -> list[dict]:
    markets = []
    seen: set = set()
    for series in _SERIES_SEARCH_ORDER:
        path   = "/markets"
        params = {"series_ticker": series, "status": "open", "limit": 10}
        try:
            async with session.get(
                KALSHI_BASE_URL + path,
                headers=_kalshi_headers("GET", path),
                params=params,
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json()
                for m in data.get("markets", []):
                    t = m.get("ticker", "")
                    if t and t not in seen:
                        seen.add(t)
                        markets.append(m)
        except Exception as exc:
            print(f"[validator] Market fetch error (series={series}): {exc}")
    return markets


async def _fetch_real_prices(
    session: aiohttp.ClientSession, ticker: str, market: dict
) -> tuple[float | None, float | None]:
    """Return (real_yes_ask_cents, real_no_ask_cents) or (None, None) on failure."""
    def _to_cents(v) -> float | None:
        try:
            return round(float(v) * 100, 1)
        except (TypeError, ValueError):
            return None

    # Try orderbook first
    path = f"/markets/{ticker}/orderbook"
    try:
        async with session.get(
            KALSHI_BASE_URL + path,
            headers=_kalshi_headers("GET", path),
            timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
        ) as resp:
            if resp.status == 200:
                ob_data = await resp.json()
                ob = ob_data.get("orderbook", {})
                yes_arr = [(p, q) for p, q in ob.get("yes", []) if q > 0]
                no_arr  = [(p, q) for p, q in ob.get("no",  []) if q > 0]
                if yes_arr and no_arr:
                    return min(p for p, _ in yes_arr), min(p for p, _ in no_arr)
    except Exception:
        pass

    # AMM fallback: fetch the market directly for fresh yes_ask/no_ask
    path2 = f"/markets/{ticker}"
    try:
        async with session.get(
            KALSHI_BASE_URL + path2,
            headers=_kalshi_headers("GET", path2),
            timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                src  = data.get("market", data)
                yes  = _to_cents(src.get("yes_ask_dollars"))
                no   = _to_cents(src.get("no_ask_dollars"))
                if yes is None and src.get("no_bid_dollars"):
                    yes = 100.0 - _to_cents(src["no_bid_dollars"])
                if no is None and src.get("yes_bid_dollars"):
                    no  = 100.0 - _to_cents(src["yes_bid_dollars"])
                return yes, no
    except Exception as exc:
        print(f"[validator] Real price fetch error for {ticker}: {exc}")

    return None, None


# ── CSV writer ────────────────────────────────────────────────────────────────

_sample_count = 0
_sim_sum      = 0.0
_real_sum     = 0.0
_gap_sum      = 0.0
_gap_n        = 0


def _write_row(
    ts: str, ticker: str, btc: float, strike: float,
    sim_yes: float, sim_no: float,
    real_yes: float | None, real_no: float | None,
) -> None:
    global _sample_count, _sim_sum, _real_sum, _gap_sum, _gap_n

    gap = (real_yes - sim_yes) if real_yes is not None else None

    file_exists = os.path.isfile(OUTPUT_CSV)
    with open(OUTPUT_CSV, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if not file_exists:
            writer.writerow([
                "ts", "ticker", "btc_price", "strike", "abs_pct",
                "sim_yes_ask", "sim_no_ask",
                "real_yes_ask", "real_no_ask",
                "price_gap_cents",
            ])
        ap = abs((btc - strike) / strike) * 100
        writer.writerow([
            ts, ticker, round(btc, 2), round(strike, 2), round(ap, 4),
            round(sim_yes, 1), round(sim_no, 1),
            round(real_yes, 1) if real_yes is not None else "null",
            round(real_no,  1) if real_no  is not None else "null",
            round(gap, 1)      if gap       is not None else "null",
        ])

    _sample_count += 1
    _sim_sum += sim_yes
    if gap is not None:
        _real_sum += real_yes
        _gap_sum  += gap
        _gap_n    += 1

    if _sample_count % 50 == 0:
        n        = _sample_count
        avg_sim  = _sim_sum / n
        avg_real = (_real_sum / _gap_n) if _gap_n else 0.0
        avg_gap  = (_gap_sum  / _gap_n) if _gap_n else 0.0
        print(
            f"[validator] Price validation: {n} samples collected. "
            f"Avg price gap: {avg_gap:+.1f}c. "
            f"Simulated avg: {avg_sim:.1f}c. Real avg: {avg_real:.1f}c."
        )
        if _gap_n >= 50:
            if avg_gap > 8:
                print("[validator] ⚠  Gap > 8c: real prices much more expensive than simulated. "
                      "Backtest EV estimates are too optimistic. Strategy may not be viable.")
            elif avg_gap > 3:
                print("[validator] ℹ  Gap 3-8c: moderate discrepancy. Consider raising min_ev_base further.")
            else:
                print("[validator] ✓  Gap < 3c: simulator reasonably accurate. "
                      "Backtest results are credible.")


# ── Main loop ─────────────────────────────────────────────────────────────────

async def main() -> None:
    _load_credentials()
    print(f"[validator] Starting. Writing to {OUTPUT_CSV}. Poll interval: {POLL_INTERVAL}s.")
    print("[validator] Stop when 200+ rows collected. Ctrl+C to exit.")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                btc     = await _fetch_btc(session)
                markets = await _fetch_markets(session)

                if btc is None:
                    print("[validator] BTC price unavailable — skipping.")
                elif not markets:
                    print("[validator] No open markets found — skipping.")
                else:
                    # Pick the soonest-expiring non-range market
                    from datetime import timezone as _tz
                    now_utc = datetime.now(_tz.utc)
                    valid = []
                    for m in markets:
                        title = m.get("title", "").lower()
                        if "range" in title or "daily" in title:
                            continue
                        try:
                            close = datetime.fromisoformat(
                                m["close_time"].replace("Z", "+00:00")
                            )
                            mins_left = (close - now_utc).total_seconds() / 60
                            if 1 < mins_left < 16:
                                valid.append((mins_left, m))
                        except Exception:
                            pass
                    valid.sort()

                    if not valid:
                        print("[validator] No valid markets in 1-16 min window — skipping.")
                    else:
                        _, market = valid[0]
                        ticker = market.get("ticker", "")
                        try:
                            strike = float(
                                market.get("floor_strike") or
                                market.get("cap_strike") or
                                (market.get("floor_strike") or market.get("yes_sub_title", "0").replace("$", "").replace(",", ""))
                            )
                        except (TypeError, ValueError):
                            print(f"[validator] Could not parse strike for {ticker} — skipping.")
                            await asyncio.sleep(POLL_INTERVAL)
                            continue

                        sim_yes, sim_no = _simulate_midpoint(btc, strike)
                        real_yes, real_no = await _fetch_real_prices(session, ticker, market)

                        ts = datetime.now(timezone.utc).isoformat()
                        _write_row(ts, ticker, btc, strike, sim_yes, sim_no, real_yes, real_no)

                        gap_str = f"{real_yes - sim_yes:+.1f}c" if real_yes else "n/a"
                        print(
                            f"[validator] {ticker} | BTC={btc:,.0f} strike={strike:,.0f} | "
                            f"sim_yes={sim_yes:.1f}c real_yes={real_yes}c gap={gap_str} "
                            f"| n={_sample_count}"
                        )

            except KeyboardInterrupt:
                print("\n[validator] Stopped.")
                break
            except Exception as exc:
                print(f"[validator] Loop error: {exc}")

            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
