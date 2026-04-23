#!/usr/bin/env python3
"""
collect_kalshi_ladder_history.py

Fetches historical Kalshi hourly above/below ladder data for BTC and ETH
and saves one parquet file per event under:
  data/kalshi/hourly/BTC/{event_ticker}.parquet
  data/kalshi/hourly/ETH/{event_ticker}.parquet

Each parquet row = one (event, strike, snapshot_timestamp) observation.
Columns: event_id, event_close_time, timestamp, strike,
         yes_bid, yes_ask, no_bid, no_ask, mid_price, volume, market_id

Credentials: set KALSHI_API_KEY and KALSHI_PRIVATE_KEY (path to PEM or inline PEM string).

Usage:
  python collect_kalshi_ladder_history.py
  python collect_kalshi_ladder_history.py --asset BTC
  python collect_kalshi_ladder_history.py --asset ETH
  python collect_kalshi_ladder_history.py --days 180
  python collect_kalshi_ladder_history.py --days 365 --delay 0.25
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from base64 import b64encode
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

try:
    from dotenv import load_dotenv
    # Load .env from next to this script, and let it override any stale
    # system env vars (avoids picking up leftover PEM strings from prior shells).
    _ENV_PATH = Path(__file__).resolve().parent / ".env"
    load_dotenv(dotenv_path=_ENV_PATH, override=True)
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("collect_ladder")

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_PATH_PREFIX = "/trade-api/v2"

# Series tickers for hourly above/below markets
SERIES = {
    "BTC": ["KXBTCD", "BTCD-B"],
    "ETH": ["KXETHD", "ETHD-B"],
}

_private_key = None
_api_key: str = ""


# ── Auth ────────────────────────────────────────────────────────────────────

def load_credentials() -> None:
    global _api_key, _private_key

    _api_key = os.environ.get("KALSHI_API_KEY", "").strip()
    pem_val = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()

    if not _api_key or not pem_val:
        # Fall back to local PEM file if env vars not set
        pem_file = Path("kalshi_private_key.pem")
        if pem_file.exists() and not pem_val:
            pem_val = str(pem_file)
        config_file = Path("config.json")
        if config_file.exists() and not _api_key:
            import json
            try:
                cfg = json.loads(config_file.read_text())
                _api_key = cfg.get("kalshi_api_key", "").strip()
            except Exception:
                pass

    if not _api_key:
        raise RuntimeError(
            "KALSHI_API_KEY not set.\n"
            "Fix: create a .env file next to this script with:\n"
            "    KALSHI_API_KEY=<your-api-key-id>\n"
            "    KALSHI_PRIVATE_KEY=./kalshi_private_key.pem\n"
            "(or export the vars in your shell before running)."
        )
    if not pem_val:
        raise RuntimeError(
            "KALSHI_PRIVATE_KEY not set. "
            "Set it to the PEM file path (e.g. ./kalshi_private_key.pem) "
            "or an inline PEM string."
        )

    # Resolve path-like values against the script's own directory so the
    # script works regardless of the CWD it was launched from.
    script_dir = Path(__file__).resolve().parent
    looks_like_path = not pem_val.lstrip().startswith("-----BEGIN")

    pem_bytes: bytes | None = None
    if looks_like_path:
        candidates = [Path(pem_val), script_dir / pem_val]
        for cand in candidates:
            if cand.is_file():
                pem_bytes = cand.read_bytes()
                log.info("Loaded private key from file: %s", cand)
                break
        if pem_bytes is None:
            raise RuntimeError(
                f"KALSHI_PRIVATE_KEY looks like a path ({pem_val!r}) but no such "
                f"file exists. Tried: {[str(c) for c in candidates]}"
            )
    else:
        pem_bytes = pem_val.encode()
        log.info("Loaded private key from inline PEM string.")

    _private_key = serialization.load_pem_private_key(pem_bytes, password=None)
    log.info("Credentials ready. API key: %s...", _api_key[:8])


def _headers(method: str, path: str) -> dict:
    ts = str(int(time.time() * 1000))
    full_path = KALSHI_PATH_PREFIX + path
    msg = (ts + method.upper() + full_path).encode()
    sig = b64encode(
        _private_key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
    ).decode()
    return {
        "KALSHI-ACCESS-KEY": _api_key,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Content-Type": "application/json",
    }


def _get(path: str, params: dict | None = None, retries: int = 3) -> dict:
    url = KALSHI_BASE + path
    for attempt in range(retries):
        try:
            resp = requests.get(
                url,
                headers=_headers("GET", path),
                params=params or {},
                timeout=15,
            )
            if resp.status_code == 429:
                wait = 2 ** (attempt + 1)
                log.warning("Rate limited. Sleeping %ss.", wait)
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                log.warning("HTTP %s for %s: %s", resp.status_code, url, resp.text[:200])
                return {}
            return resp.json()
        except requests.RequestException as exc:
            log.warning("Request error (attempt %d): %s", attempt + 1, exc)
            time.sleep(1)
    return {}


# ── Kalshi API wrappers ─────────────────────────────────────────────────────

def fetch_settled_events(series_ticker: str, min_close_ts: int, delay: float) -> list[dict]:
    """
    Page through all settled events for a series with close_time >= min_close_ts.
    Returns a list of event dicts.
    """
    events: list[dict] = []
    cursor = None
    page = 0

    while True:
        params: dict = {
            "series_ticker": series_ticker,
            "status": "settled",
            "limit": 200,
            "min_close_ts": min_close_ts,
        }
        if cursor:
            params["cursor"] = cursor

        data = _get("/events", params)
        if not data:
            break

        batch = data.get("events", [])
        events.extend(batch)
        page += 1
        log.info("  [%s] page %d: %d events (total so far: %d)", series_ticker, page, len(batch), len(events))

        cursor = data.get("cursor")
        if not cursor or len(batch) == 0:
            break

        time.sleep(delay)

    return events


def fetch_event_markets(event_ticker: str, delay: float) -> list[dict]:
    """
    Fetch all markets (strikes) for a given event via /markets?event_ticker=...
    Returns list of market dicts.
    """
    data = _get("/markets", {"event_ticker": event_ticker, "limit": 200})
    return data.get("markets", [])


def fetch_market_candlesticks(
    series_ticker: str,
    market_ticker: str,
    start_ts: int,
    end_ts: int,
    period_interval: int = 1,
) -> list[dict]:
    """
    Fetch candlesticks for one market over [start_ts, end_ts].
    period_interval: Kalshi accepts 1 (minute), 60 (hour), 1440 (day).
    Returns list of candlestick dicts.
    """
    data = _get(
        f"/series/{series_ticker}/markets/{market_ticker}/candlesticks",
        {
            "start_ts": start_ts,
            "end_ts": end_ts,
            "period_interval": period_interval,
        },
    )
    return data.get("candlesticks", [])


# ── Data helpers ────────────────────────────────────────────────────────────

def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _extract_strike(market: dict) -> float | None:
    """Extract the strike price (floor_strike) from a market dict."""
    # Most Kalshi above/below markets have floor_strike in dollars
    for field in ("floor_strike", "strike_price", "strike"):
        val = market.get(field)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass

    # Fall back: parse strike from ticker suffix (e.g. KXBTCD-26APR22-T89000 → 89000)
    ticker = market.get("ticker", "")
    parts = ticker.rsplit("-", 1)
    if len(parts) == 2:
        suffix = parts[-1]
        # Strip leading B/T (below/above) prefix
        num = suffix.lstrip("BTbt")
        try:
            return float(num)
        except ValueError:
            pass

    return None


def _cents_to_prob(val) -> float | None:
    """Convert a cents price (0–100) to a probability (0–1)."""
    if val is None:
        return None
    try:
        f = float(val)
        # Already in 0–1 range
        if f <= 1.0:
            return f
        return f / 100.0
    except (TypeError, ValueError):
        return None


def build_rows_from_market_snapshot(
    event_id: str,
    event_close_time: datetime,
    market: dict,
    snapshot_ts: datetime,
) -> dict | None:
    """
    Build one data row from a market dict (single snapshot).
    Returns None if the market has no usable strike or price data.
    """
    strike = _extract_strike(market)
    if strike is None:
        return None

    # Kalshi's current API returns prices as strings in dollars (e.g. "0.5000")
    # under *_dollars fields; legacy responses used integer cents under bare names.
    def _pick(*keys):
        for k in keys:
            v = market.get(k)
            if v is not None and v != "":
                return v
        return None

    yes_bid = _cents_to_prob(_pick("yes_bid_dollars", "yes_bid", "yes_bid_price"))
    yes_ask = _cents_to_prob(_pick("yes_ask_dollars", "yes_ask", "yes_ask_price"))
    no_bid  = _cents_to_prob(_pick("no_bid_dollars",  "no_bid",  "no_bid_price"))
    no_ask  = _cents_to_prob(_pick("no_ask_dollars",  "no_ask",  "no_ask_price"))

    # Detect the empty-book case on settled markets (bid=0, ask=1 means no quotes).
    empty_book = (
        (yes_bid is not None and yes_bid <= 0.0005) and
        (yes_ask is not None and yes_ask >= 0.9995)
    )
    if empty_book:
        # Clear all four sides so the complement-derivation below can't
        # resurrect the degenerate 0/1 quote; last_price fallback handles the rest.
        yes_bid = yes_ask = no_bid = no_ask = None
        # Prefer the snapshot immediately prior to settlement when available.
        prev_bid = _cents_to_prob(_pick("previous_yes_bid_dollars"))
        prev_ask = _cents_to_prob(_pick("previous_yes_ask_dollars"))
        prev_empty = (
            (prev_bid is not None and prev_bid <= 0.0005) and
            (prev_ask is not None and prev_ask >= 0.9995)
        )
        if prev_bid is not None and prev_ask is not None and not prev_empty:
            yes_bid, yes_ask = prev_bid, prev_ask

    # Derive missing sides from complement
    if yes_bid is not None and no_ask is None:
        no_ask = round(1.0 - yes_bid, 4)
    if yes_ask is not None and no_bid is None:
        no_bid = round(1.0 - yes_ask, 4)
    if no_bid is not None and yes_ask is None:
        yes_ask = round(1.0 - no_bid, 4)
    if no_ask is not None and yes_bid is None:
        yes_bid = round(1.0 - no_ask, 4)

    # Fallback: estimate 2-cent spread around last price
    last = _cents_to_prob(_pick("last_price_dollars", "last_price", "yes_last_price"))
    if yes_bid is None and last is not None:
        yes_bid = max(0.01, last - 0.01)
        yes_ask = min(0.99, last + 0.01)
        no_bid  = max(0.01, 1.0 - yes_ask)
        no_ask  = min(0.99, 1.0 - yes_bid)

    if yes_bid is None:
        # No price data at all — skip this market
        return None

    mid = (yes_bid + yes_ask) / 2.0 if yes_ask is not None else yes_bid

    return {
        "event_id":         event_id,
        "event_close_time": event_close_time,
        "timestamp":        snapshot_ts,
        "strike":           strike,
        "yes_bid":          round(yes_bid, 4),
        "yes_ask":          round(yes_ask, 4) if yes_ask is not None else round(yes_bid, 4),
        "no_bid":           round(no_bid, 4) if no_bid is not None else round(1.0 - yes_ask, 4),
        "no_ask":           round(no_ask, 4) if no_ask is not None else round(1.0 - yes_bid, 4),
        "mid_price":        round(mid, 4),
        "volume":           float(
            market.get("volume_fp")
            or market.get("volume_24h_fp")
            or market.get("volume", 0)
            or 0
        ),
        "market_id":        market.get("ticker", ""),
    }


def build_rows_from_candlesticks(
    event_id: str,
    event_close_time: datetime,
    market: dict,
    candlesticks: list[dict],
) -> list[dict]:
    """
    Build one row per candlestick snapshot for a market.
    """
    strike = _extract_strike(market)
    if strike is None:
        return []

    market_id = market.get("ticker", "")
    rows = []

    for cs in candlesticks:
        # Timestamp: prefer close_ts, fall back to open_ts
        raw_ts = cs.get("close_ts") or cs.get("open_ts") or cs.get("end_period_ts")
        if raw_ts is None:
            continue
        if isinstance(raw_ts, (int, float)):
            snap_ts = datetime.fromtimestamp(raw_ts, tz=timezone.utc)
        else:
            snap_ts = _parse_ts(str(raw_ts))
        if snap_ts is None:
            continue

        # Current API schema: nested dicts with *_dollars subfields
        #   price: {open_dollars, high_dollars, low_dollars, close_dollars, mean_dollars}
        #   yes_bid / yes_ask: same nested shape
        # Legacy: flat cents fields (yes_price_close, etc.)
        def _nested_close(container):
            if isinstance(container, dict):
                return (
                    container.get("close_dollars")
                    or container.get("mean_dollars")
                    or container.get("close")
                )
            return container

        price_close   = _nested_close(cs.get("price"))
        yes_bid_close = _nested_close(cs.get("yes_bid"))
        yes_ask_close = _nested_close(cs.get("yes_ask"))

        # Fall back to flat/legacy fields
        if price_close is None:
            price_close = cs.get("yes_price_close") or cs.get("yes_close")

        mid = _cents_to_prob(price_close)
        bid_val = _cents_to_prob(yes_bid_close)
        ask_val = _cents_to_prob(yes_ask_close)

        # Derive a usable mid if the trade mid is missing
        if mid is None and bid_val is not None and ask_val is not None:
            mid = (bid_val + ask_val) / 2.0
        if mid is None:
            continue

        # Use real book if we have it; otherwise estimate a 1.5-cent half-spread.
        if bid_val is not None and ask_val is not None and ask_val > bid_val:
            yes_bid, yes_ask = bid_val, ask_val
        else:
            half_spread = 0.015
            yes_bid = max(0.01, mid - half_spread)
            yes_ask = min(0.99, mid + half_spread)
        no_bid = max(0.01, 1.0 - yes_ask)
        no_ask = min(0.99, 1.0 - yes_bid)

        volume = float(
            cs.get("volume_fp")
            or cs.get("volume")
            or cs.get("yes_price_volume")
            or 0
        )

        rows.append({
            "event_id":         event_id,
            "event_close_time": event_close_time,
            "timestamp":        snap_ts,
            "strike":           strike,
            "yes_bid":          round(yes_bid, 4),
            "yes_ask":          round(yes_ask, 4),
            "no_bid":           round(no_bid, 4),
            "no_ask":           round(no_ask, 4),
            "mid_price":        round(mid, 4),
            "volume":           volume,
            "market_id":        market_id,
        })

    return rows


# ── Main collection ──────────────────────────────────────────────────────────

def collect_asset(
    asset: str,
    days_back: int,
    output_root: str,
    delay: float,
    candlesticks: bool,
) -> int:
    """
    Collect all settled ladder events for one asset.
    Returns the number of events saved.
    """
    out_dir = Path(output_root) / asset.upper()
    out_dir.mkdir(parents=True, exist_ok=True)

    min_close_ts = int((datetime.now(timezone.utc) - timedelta(days=days_back)).timestamp())
    saved = 0

    for series_ticker in SERIES[asset.upper()]:
        log.info("[%s] Fetching settled events for series %s ...", asset.upper(), series_ticker)
        events = fetch_settled_events(series_ticker, min_close_ts, delay)
        log.info("[%s] Found %d settled events in series %s.", asset.upper(), len(events), series_ticker)

        for event in events:
            event_ticker = event.get("event_ticker") or event.get("ticker")
            if not event_ticker:
                continue

            out_path = out_dir / f"{event_ticker}.parquet"
            if out_path.exists():
                log.debug("[%s] Already have %s — skipping.", asset.upper(), event_ticker)
                continue

            # Kalshi's /events endpoint returns `strike_date` (event close in UTC);
            # older/legacy payloads may expose close_time / settled_time.
            close_time = _parse_ts(
                event.get("close_time")
                or event.get("settled_time")
                or event.get("strike_date")
            )
            if close_time is None:
                log.warning("[%s] No close_time for event %s — skipping.", asset.upper(), event_ticker)
                continue

            # Fetch markets for this event
            log.info("[%s] Event %s: fetching markets ...", asset.upper(), event_ticker)
            time.sleep(delay)
            markets = fetch_event_markets(event_ticker, delay)

            if not markets:
                log.warning("[%s] No markets for event %s — skipping.", asset.upper(), event_ticker)
                continue

            # Filter: only markets with a valid strike
            valid_markets = [m for m in markets if _extract_strike(m) is not None]
            if len(valid_markets) < 3:
                log.warning(
                    "[%s] Event %s has only %d markets with valid strikes — skipping.",
                    asset.upper(), event_ticker, len(valid_markets),
                )
                continue

            # In --candlesticks mode, drop strikes that never traded during
            # the event's life. Deep-ITM/OTM strikes carry near-zero info for
            # the Strategy C scanner and fetching candles for them is the
            # dominant cost per event. Keeps the ladder dense near ATM.
            if candlesticks:
                def _vol(m):
                    try:
                        return float(m.get("volume_fp") or m.get("volume") or 0)
                    except (TypeError, ValueError):
                        return 0.0
                traded = [m for m in valid_markets if _vol(m) > 0]
                if len(traded) >= 5:
                    valid_markets = traded

            rows: list[dict] = []

            if candlesticks:
                log.info(
                    "[%s] Event %s: fetching candlesticks for %d markets ...",
                    asset.upper(), event_ticker, len(valid_markets),
                )
                # Collect per-minute candlesticks for the event duration
                open_time = _parse_ts(event.get("open_time"))
                if open_time is None:
                    open_time = close_time - timedelta(hours=2)

                start_ts = int(open_time.timestamp())
                end_ts = int(close_time.timestamp())

                for mi, market in enumerate(valid_markets):
                    if mi and mi % 25 == 0:
                        log.info(
                            "[%s] Event %s: %d/%d markets processed ...",
                            asset.upper(), event_ticker, mi, len(valid_markets),
                        )
                    time.sleep(delay)
                    cds = fetch_market_candlesticks(
                        series_ticker,
                        market.get("ticker", ""),
                        start_ts=start_ts,
                        end_ts=end_ts,
                        period_interval=1,  # 1-minute candles (Kalshi accepts 1/60/1440)
                    )
                    if cds:
                        rows.extend(
                            build_rows_from_candlesticks(event_ticker, close_time, market, cds)
                        )
                    else:
                        # Fall back to single snapshot from market dict
                        snap_ts = open_time
                        row = build_rows_from_market_snapshot(
                            event_ticker, close_time, market, snap_ts
                        )
                        if row:
                            rows.append(row)
            else:
                # Single snapshot per market (faster, lower API usage)
                snap_ts = close_time - timedelta(minutes=30)  # ~30m before close
                for market in valid_markets:
                    row = build_rows_from_market_snapshot(
                        event_ticker, close_time, market, snap_ts
                    )
                    if row:
                        rows.append(row)

            if not rows:
                log.warning("[%s] No rows built for event %s — skipping.", asset.upper(), event_ticker)
                continue

            df = pd.DataFrame(rows)
            df["event_close_time"] = pd.to_datetime(df["event_close_time"], utc=True)
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            df = df.sort_values(["strike", "timestamp"]).reset_index(drop=True)

            df.to_parquet(out_path, index=False)
            saved += 1
            log.info(
                "[%s] Saved %s: %d rows, %d strikes — %s",
                asset.upper(), event_ticker, len(df), df["strike"].nunique(), out_path,
            )

            time.sleep(delay)

        log.info("[%s] Series %s done. Events saved this run: %d", asset.upper(), series_ticker, saved)

    return saved


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect historical Kalshi hourly ladder data for BTC and ETH."
    )
    parser.add_argument(
        "--asset",
        choices=["BTC", "ETH", "btc", "eth"],
        default=None,
        help="Asset to collect (default: both BTC and ETH).",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=180,
        help="How many days back to collect (default: 180).",
    )
    parser.add_argument(
        "--output",
        default="data/kalshi/hourly",
        help="Root output directory (default: data/kalshi/hourly).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.3,
        help="Seconds to sleep between API calls (default: 0.3).",
    )
    parser.add_argument(
        "--candlesticks",
        action="store_true",
        help="Fetch 5-min candlesticks per market (more data but ~40x more API calls).",
    )
    args = parser.parse_args()

    load_credentials()

    assets = (
        [args.asset.upper()]
        if args.asset
        else ["BTC", "ETH"]
    )

    # Resolve the output root against the script directory when relative, so
    # double-click / shortcut launches don't try to write into protected paths
    # (e.g. C:\Program Files\WindowsApps when CWD inherits from the launcher).
    out_root = Path(args.output)
    if not out_root.is_absolute():
        out_root = Path(__file__).resolve().parent / out_root
    out_root.mkdir(parents=True, exist_ok=True)
    log.info("Output root: %s", out_root)

    total = 0
    for asset in assets:
        log.info("=== Collecting %s (last %d days) ===", asset, args.days)
        n = collect_asset(
            asset=asset,
            days_back=args.days,
            output_root=str(out_root),
            delay=args.delay,
            candlesticks=args.candlesticks,
        )
        total += n
        log.info("=== %s done: %d events saved ===", asset, n)

    log.info("Collection complete. Total events saved: %d", total)
    log.info("Output: %s/BTC/  and  %s/ETH/", out_root, out_root)
    log.info("Next step: py backtesting/cli.py backtest --asset btc --strategy c")


def _pause_if_double_clicked() -> None:
    """Keep the console open on Windows double-click so errors are readable.

    Detects double-click launch by checking whether this process is the only
    attached console process — that's true when Explorer spawned a fresh
    console for us, false when a pre-existing shell launched the script.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        buf = (ctypes.c_ulong * 4)()
        count = ctypes.windll.kernel32.GetConsoleProcessList(buf, 4)
        if count <= 1:
            input("\nPress Enter to close...")
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.warning("Interrupted by user.")
        _pause_if_double_clicked()
        sys.exit(130)
    except Exception as exc:
        log.error("%s", exc)
        _pause_if_double_clicked()
        sys.exit(1)
