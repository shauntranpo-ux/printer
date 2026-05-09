#!/usr/bin/env python3
"""
scripts/calibrate_winrates.py

Fetch historical price data, compute empirical S1+S2 win-rate tables, print
to stdout as Python dicts ready to paste into bot_strategy.py.

Usage:
    python scripts/calibrate_winrates.py

Env vars required for S2 (Kalshi) phase:
    KALSHI_API_KEY         -- API key ID (uuid)
    KALSHI_PRIVATE_KEY     -- PEM-encoded RSA private key (or path to .pem file)

Optional env vars:
    SKIP_S1=1              -- skip Binance/S1 phase (S1 tables already populated)
    SKIP_S2=1              -- skip Kalshi/S2 phase (no credentials needed)
    CALIBRATE_DAYS=N       -- days of history to fetch (default: 30)

Recommended usage (S2-only, auto-patch bot_strategy.py):
    py scripts/run_calibration.py
"""
import datetime
import math
import os
import sys
import time
from base64 import b64encode
from collections import defaultdict

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ── Kalshi API constants ──────────────────────────────────────────────────────
KALSHI_BASE_URL    = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_PATH_PREFIX = "/trade-api/v2"

# Candidate series tickers per asset — tried in order until one returns markets
KALSHI_SERIES = {
    "BTC":  ["KXBTC15M", "KXBTCD", "BTCD-B"],
    "ETH":  ["KXETH15M", "KXETHD"],
    "SOL":  ["KXSOL15M", "KXSOLD"],
    "XRP":  ["KXXRP15M", "KXXRPD"],
    "DOGE": ["KXDOGE15M", "KXDOGED"],
}

# ── Binance API constants ─────────────────────────────────────────────────────
BINANCE_BASE_URL = "https://api.binance.us"  # binance.com blocks US IPs (451); .us same API
BINANCE_SYMBOLS  = {
    "BTC":  "BTCUSDT",
    "ETH":  "ETHUSDT",
    "SOL":  "SOLUSDT",
    "XRP":  "XRPUSDT",
    "DOGE": "DOGEUSDT",
}

# ── Per-asset config — KEEP IN SYNC WITH bot_strategy.py ─────────────────────
S1_ASSET_CONFIG = {
    "BTC":  dict(min_dist=0.0025, ema_short=3, ema_long=10),
    "ETH":  dict(min_dist=0.0030, ema_short=3, ema_long=10),
    "SOL":  dict(min_dist=0.0050, ema_short=3, ema_long=8),
    "XRP":  dict(min_dist=0.0040, ema_short=3, ema_long=10),
    "DOGE": dict(min_dist=0.0080, ema_short=2, ema_long=8),
}
S2_ASSET_CONFIG = {
    "BTC":  dict(min_dist=0.0035, min_vel_delta=0.80, vel_lookback=4),
    "ETH":  dict(min_dist=0.0030, min_vel_delta=0.70, vel_lookback=4),
    "SOL":  dict(min_dist=0.0060, min_vel_delta=1.20, vel_lookback=3),
    "XRP":  dict(min_dist=0.0050, min_vel_delta=0.90, vel_lookback=4),
    "DOGE": dict(min_dist=0.0100, min_vel_delta=1.50, vel_lookback=3),
}

MIN_SAMPLES     = 50   # buckets below this get None → bot falls back to tanh
CALIBRATE_DAYS  = int(os.environ.get("CALIBRATE_DAYS", "30"))


def _log(msg: str) -> None:
    """Progress output to stderr so stdout stays clean for paste."""
    print(msg, file=sys.stderr, flush=True)


def _binance_get(path: str, params: dict) -> dict:
    """GET from Binance public API with retry on 429."""
    url = BINANCE_BASE_URL + path
    for attempt in range(5):
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 2 ** attempt))
            _log(f"  Binance 429 — sleeping {wait}s")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Binance {path} failed after retries")


def _load_kalshi_key() -> tuple:
    """Load KALSHI_API_KEY and KALSHI_PRIVATE_KEY from env. Returns (key_id, private_key)."""
    key_id = os.environ.get("KALSHI_API_KEY", "").strip()
    pem_val = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()
    if not key_id or not pem_val:
        raise RuntimeError(
            "KALSHI_API_KEY and KALSHI_PRIVATE_KEY env vars are required for S2 calibration.\n"
            "Skip S2 by setting SKIP_S2=1."
        )
    if os.path.exists(pem_val):
        with open(pem_val, "rb") as fh:
            pem_bytes = fh.read()
    else:
        pem_bytes = pem_val.encode()
    private_key = serialization.load_pem_private_key(pem_bytes, password=None)
    return key_id, private_key


def _kalshi_headers(method: str, path: str, key_id: str, private_key) -> dict:
    """Generate Kalshi RSA-PSS auth headers."""
    ts = str(int(time.time() * 1000))
    full_path = KALSHI_PATH_PREFIX + path
    msg = (ts + method.upper() + full_path).encode()
    sig = b64encode(
        private_key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
    ).decode()
    return {
        "KALSHI-ACCESS-KEY":       key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Content-Type":            "application/json",
    }


def _kalshi_get(path: str, params: dict, key_id: str, private_key) -> dict:
    """GET from Kalshi API with auth and retry on 429."""
    url = KALSHI_BASE_URL + path
    for attempt in range(5):
        headers = _kalshi_headers("GET", path, key_id, private_key)
        r = requests.get(url, headers=headers, params=params, timeout=15)
        if r.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 401:
            raise RuntimeError("Kalshi 401 — check KALSHI_API_KEY and KALSHI_PRIVATE_KEY")
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Kalshi {path} failed after retries")


# ── Binance data fetch ────────────────────────────────────────────────────────

def fetch_binance_1m(symbol: str, days: int) -> list:
    """
    Fetch last `days` of 1-minute close prices from Binance.
    Returns list of (timestamp_ms, close_price), oldest first.
    """
    end_ms   = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 3600 * 1000
    result = []
    cursor = start_ms

    while cursor < end_ms:
        batch = _binance_get(
            "/api/v3/klines",
            {"symbol": symbol, "interval": "1m", "limit": 1000, "startTime": cursor},
        )
        if not batch:
            break
        for row in batch:
            ts    = int(row[0])
            close = float(row[4])
            result.append((ts, close))
        last_ts = int(batch[-1][0])
        if last_ts <= cursor:
            break
        cursor = last_ts + 60_000
        if len(batch) < 1000:
            break

    return result


# ── EMA helper ────────────────────────────────────────────────────────────────

def _compute_ema(values: list) -> float:
    """Standard EMA over a list of floats, newest value last."""
    if not values:
        return 0.0
    alpha = 2.0 / (len(values) + 1)
    v = values[0]
    for x in values[1:]:
        v = alpha * x + (1.0 - alpha) * v
    return v


# ── S1 simulation ─────────────────────────────────────────────────────────────

S1_ENTRY_OFFSETS = [3, 5, 7, 10, 12]  # minutes remaining before expiry


def simulate_s1_window(
    open_ms: int,
    strike: float,
    all_closes: list,
    cfg: dict,
) -> list:
    """
    Simulate S1 EMA entries for one 15-min market window.

    Args:
        open_ms:    Window open timestamp in ms.
        strike:     Strike price (close at window open).
        all_closes: Full list of (ts_ms, close) for the asset, sorted ascending.
        cfg:        S1_ASSET_CONFIG entry for this asset.

    Returns:
        List of (abs_pct, mins_left, won) for each valid entry point.
    """
    close_ms = open_ms + 15 * 60_000
    min_dist = cfg["min_dist"]
    ema_short_min = cfg["ema_short"]
    ema_long_min  = cfg["ema_long"]

    # Final close price at expiry
    expiry_close = None
    for ts, px in all_closes:
        if ts >= close_ms:
            expiry_close = px
            break
    if expiry_close is None:
        return []

    outcome_yes_wins = expiry_close > strike

    records = []

    for mins_remaining in S1_ENTRY_OFFSETS:
        entry_ms = close_ms - mins_remaining * 60_000
        if entry_ms <= open_ms:
            continue

        prices_at_entry = [(ts, px) for ts, px in all_closes if ts <= entry_ms]
        if len(prices_at_entry) < ema_long_min + 2:
            continue

        current_price = prices_at_entry[-1][1]
        abs_pct = abs(current_price - strike) / strike if strike > 0 else 0.0

        if abs_pct < min_dist:
            continue

        short_window = ema_short_min * 60_000
        long_window  = ema_long_min  * 60_000
        entry_ts     = prices_at_entry[-1][0]

        short_prices = [px for ts, px in prices_at_entry if ts >= entry_ts - short_window]
        long_prices  = [px for ts, px in prices_at_entry if ts >= entry_ts - long_window]

        if len(short_prices) < 2 or len(long_prices) < 3:
            continue

        s_ema = _compute_ema(short_prices)
        l_ema = _compute_ema(long_prices)
        ema_side = "yes" if s_ema > l_ema else "no"

        # Continuation-only filter
        if ema_side == "yes" and current_price < strike:
            continue
        if ema_side == "no" and current_price > strike:
            continue

        won = (ema_side == "yes" and outcome_yes_wins) or \
              (ema_side == "no"  and not outcome_yes_wins)

        records.append((abs_pct, float(mins_remaining), won))

    return records


def simulate_s1(closes: list, asset: str) -> list:
    """
    Run S1 simulation over all 15-min windows in the closes list.
    Returns list of (abs_pct, mins_left, won) records.
    """
    cfg = S1_ASSET_CONFIG[asset]
    records = []

    if not closes:
        return records

    first_ts = closes[0][0]
    last_ts  = closes[-1][0]
    WINDOW_MS = 15 * 60_000

    start = ((first_ts // WINDOW_MS) + 1) * WINDOW_MS
    t = start
    while t + WINDOW_MS <= last_ts:
        strike_px = None
        for ts, px in closes:
            if ts >= t:
                strike_px = px
                break
        if strike_px is not None:
            window_records = simulate_s1_window(t, strike_px, closes, cfg)
            records.extend(window_records)
        t += WINDOW_MS

    return records


# ── S1 bucketing ──────────────────────────────────────────────────────────────

S1_DIST_BOUNDS = [0.005, 0.010, 0.020]  # 0.5%, 1.0%, 2.0%
S1_TIME_BOUNDS = [6.0, 9.0]             # 3-6, 6-9, 9-12 min remaining


def _s1_dist_idx(abs_pct: float) -> int:
    for i, bound in enumerate(S1_DIST_BOUNDS):
        if abs_pct < bound:
            return i
    return len(S1_DIST_BOUNDS)


def _s1_time_idx(mins_left: float) -> int:
    for i, bound in enumerate(S1_TIME_BOUNDS):
        if mins_left < bound:
            return i
    return len(S1_TIME_BOUNDS)


def bucket_s1(records: list, min_dist: float, min_samples: int = MIN_SAMPLES) -> dict:
    """
    Bucket S1 records into (dist_idx, time_idx) → win_rate table.
    Entries with fewer than min_samples records return None.
    """
    counts  = defaultdict(int)
    wins    = defaultdict(int)

    for abs_pct, mins_left, won in records:
        if abs_pct < min_dist:
            continue
        key = (_s1_dist_idx(abs_pct), _s1_time_idx(mins_left))
        counts[key] += 1
        if won:
            wins[key] += 1

    n_dist = len(S1_DIST_BOUNDS) + 1
    n_time = len(S1_TIME_BOUNDS) + 1
    table = {}
    for d in range(n_dist):
        for t in range(n_time):
            key = (d, t)
            n = counts[key]
            if n < min_samples:
                table[key] = None
            else:
                table[key] = round(wins[key] / n, 4)
    return table


# ── Kalshi market listing ─────────────────────────────────────────────────────

def fetch_kalshi_markets(series_ticker: str, days: int, key_id: str, private_key) -> list:
    """
    Fetch all settled 15-min markets for a series within the last `days` days.
    Returns list of dicts with keys: ticker, floor_strike, open_time, close_time, result.
    """
    cutoff_ts = time.time() - days * 86400
    markets = []
    cursor = ""

    while True:
        params = {
            "series_ticker": series_ticker,
            "status":        "settled",
            "limit":         200,
        }
        if cursor:
            params["cursor"] = cursor

        data   = _kalshi_get("/markets", params, key_id, private_key)
        batch  = data.get("markets", [])
        cursor = data.get("cursor", "")

        stop = False
        for mkt in batch:
            close_str = mkt.get("close_time", "")
            if not close_str:
                continue
            try:
                ct = datetime.datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                if ct.timestamp() < cutoff_ts:
                    stop = True
                    break
            except Exception:
                continue
            if mkt.get("result") not in ("yes", "no"):
                continue
            markets.append({
                "ticker":       mkt["ticker"],
                "floor_strike": float(mkt.get("floor_strike", 0)),
                "open_time":    mkt.get("open_time", ""),
                "close_time":   close_str,
                "result":       mkt["result"],
            })

        if stop or not cursor:
            break

    return markets


# ── Kalshi price history ──────────────────────────────────────────────────────

def fetch_market_history(
    series_ticker: str,
    market_ticker: str,
    open_ts: int,
    close_ts: int,
    key_id: str,
    private_key,
) -> list:
    """
    Fetch 1-minute yes_ask candlesticks for a single Kalshi market.
    Returns list of (ts_seconds, yes_ask_cents), sorted ascending by time.
    Returns empty list if unavailable.
    """
    path = f"/series/{series_ticker}/markets/{market_ticker}/candlesticks"
    try:
        data = _kalshi_get(
            path,
            {"start_ts": open_ts, "end_ts": close_ts, "period_interval": 1},
            key_id,
            private_key,
        )
    except Exception:
        return []

    result = []
    for cs in data.get("candlesticks", []):
        ts = cs.get("end_period_ts")
        ya = cs.get("yes_ask", {})
        close_str = ya.get("close_dollars")
        if ts is None or close_str is None:
            continue
        result.append((int(ts), round(float(close_str) * 100)))

    result.sort(key=lambda x: x[0])
    return result


# ── S2 simulation ─────────────────────────────────────────────────────────────

S2_ENTRY_OFFSETS = [2, 4, 6, 8, 10, 12]  # minutes remaining before expiry


def _s2_velocity(history: list, entry_ts: int, lookback: int, min_delta: float) -> tuple:
    """
    Compute velocity direction from yes_ask history up to entry_ts.
    Returns (side, abs_delta) or (None, 0.0) if signal too weak.
    """
    available = [ask for ts, ask in history if ts <= entry_ts]
    if len(available) < lookback + 1:
        return None, 0.0
    recent = available[-(lookback + 1):]
    mid    = max(1, len(recent) // 2)
    first_avg  = sum(recent[:mid]) / mid
    second_avg = sum(recent[mid:]) / max(1, len(recent) - mid)
    delta = second_avg - first_avg
    if abs(delta) < min_delta:
        return None, 0.0
    return ("yes" if delta > 0 else "no"), abs(delta)


def simulate_s2_window(
    open_time_s: int,
    close_time_s: int,
    strike: float,
    current_price: float,
    history: list,
    result_yes_wins: bool,
    cfg: dict,
) -> list:
    """
    Simulate S2 velocity entries for one 15-min market window.

    Args:
        open_time_s:     Market open timestamp in seconds.
        close_time_s:    Market close timestamp in seconds.
        strike:          Strike price at market open.
        current_price:   Asset price at entry (used for continuation filter).
        history:         List of (ts_sec, yes_ask_cents), sorted ascending.
        result_yes_wins: True if YES contract won.
        cfg:             S2_ASSET_CONFIG entry for this asset.

    Returns:
        List of (vel_delta, mins_left, won).
    """
    min_vel  = cfg["min_vel_delta"]
    lookback = cfg["vel_lookback"]
    records  = []

    for mins_remaining in S2_ENTRY_OFFSETS:
        entry_s = close_time_s - mins_remaining * 60
        if entry_s <= open_time_s:
            continue

        # yes_ask at entry — infer implied direction
        entry_yes_ask = None
        for ts, ask in history:
            if ts <= entry_s:
                entry_yes_ask = ask
        if entry_yes_ask is None:
            continue

        # Infer price-vs-strike direction from yes_ask implied probability
        implied_above = entry_yes_ask > 50

        # Velocity signal
        side, vel_delta = _s2_velocity(history, entry_s, lookback, min_vel)
        if side is None:
            continue

        # Continuation-only filter
        if side == "yes" and not implied_above:
            continue
        if side == "no" and implied_above:
            continue

        won = (side == "yes" and result_yes_wins) or \
              (side == "no"  and not result_yes_wins)

        records.append((vel_delta, float(mins_remaining), won))

    return records


# ── S2 bucketing ──────────────────────────────────────────────────────────────

S2_VEL_MULTIPLIERS = [2.0, 4.0]   # breakpoints as multipliers of min_vel_delta
S2_TIME_BOUNDS_S2  = [5.0, 8.0]   # 2-5, 5-8, 8-13 min remaining


def _s2_vel_idx(vel_delta: float, min_vel_delta: float) -> int:
    ratio = vel_delta / max(min_vel_delta, 1e-9)
    for i, mult in enumerate(S2_VEL_MULTIPLIERS):
        if ratio < mult:
            return i
    return len(S2_VEL_MULTIPLIERS)


def _s2_time_idx(mins_left: float) -> int:
    for i, bound in enumerate(S2_TIME_BOUNDS_S2):
        if mins_left < bound:
            return i
    return len(S2_TIME_BOUNDS_S2)


def bucket_s2(records: list, min_vel_delta: float, min_samples: int = MIN_SAMPLES) -> dict:
    """
    Bucket S2 records into (vel_idx, time_idx) → win_rate table.
    Entries with fewer than min_samples records return None.
    """
    counts = defaultdict(int)
    wins   = defaultdict(int)

    for vel_delta, mins_left, won in records:
        key = (_s2_vel_idx(vel_delta, min_vel_delta), _s2_time_idx(mins_left))
        counts[key] += 1
        if won:
            wins[key] += 1

    n_vel  = len(S2_VEL_MULTIPLIERS) + 1
    n_time = len(S2_TIME_BOUNDS_S2)  + 1
    table  = {}
    for v in range(n_vel):
        for t in range(n_time):
            key = (v, t)
            n = counts[key]
            table[key] = None if n < min_samples else round(wins[key] / n, 4)
    return table


# ── Output formatting ─────────────────────────────────────────────────────────

def _format_table(table: dict) -> str:
    """Format a bucket dict as a compact Python dict literal."""
    items = ", ".join(
        f"({k[0]},{k[1]}): {v!r}" for k, v in sorted(table.items())
    )
    return "{" + items + "}"


# ── Phase runners ─────────────────────────────────────────────────────────────

def run_s1_phase(assets: list) -> dict:
    """Fetch Binance data and compute S1 win-rate tables for all assets."""
    result = {}
    for asset in assets:
        symbol = BINANCE_SYMBOLS[asset]
        _log(f"[S1] {asset}: fetching {symbol} 1m data (90 days)...")
        try:
            closes = fetch_binance_1m(symbol, days=90)
            _log(f"[S1] {asset}: {len(closes):,} bars fetched")
        except Exception as exc:
            _log(f"[S1] {asset}: FETCH FAILED — {exc} — skipping")
            result[asset] = {}
            continue

        records = simulate_s1(closes, asset)
        _log(f"[S1] {asset}: {len(records):,} records after simulation")

        cfg   = S1_ASSET_CONFIG[asset]
        table = bucket_s1(records, min_dist=cfg["min_dist"])

        none_count = sum(1 for v in table.values() if v is None)
        _log(f"[S1] {asset}: {len(table) - none_count}/{len(table)} buckets populated")
        result[asset] = table

    return result


def run_s2_phase(assets: list, key_id: str, private_key) -> dict:
    """Fetch Kalshi markets + histories and compute S2 win-rate tables for all assets."""
    result = {}
    for asset in assets:
        cfg        = S2_ASSET_CONFIG[asset]
        candidates = KALSHI_SERIES[asset]

        # Discover working series ticker
        series_ticker = None
        for candidate in candidates:
            _log(f"[S2] {asset}: probing series {candidate}...")
            try:
                markets = fetch_kalshi_markets(candidate, days=2, key_id=key_id, private_key=private_key)
                if markets:
                    series_ticker = candidate
                    _log(f"[S2] {asset}: using series {candidate}")
                    break
            except Exception:
                continue

        if series_ticker is None:
            _log(f"[S2] {asset}: ERROR — no markets found for any candidate ticker {candidates} — skipping")
            result[asset] = {}
            continue

        _log(f"[S2] {asset}: fetching {CALIBRATE_DAYS} days of markets...")
        markets = fetch_kalshi_markets(series_ticker, days=CALIBRATE_DAYS, key_id=key_id, private_key=private_key)
        _log(f"[S2] {asset}: {len(markets)} markets found")

        all_records = []
        skipped = 0
        for i, mkt in enumerate(markets):
            if i % 100 == 0:
                _log(f"[S2] {asset}: processing market {i}/{len(markets)}...")
            if i > 0:
                time.sleep(0.08)

            try:
                ct = datetime.datetime.fromisoformat(mkt["close_time"].replace("Z", "+00:00"))
                ot = datetime.datetime.fromisoformat(mkt["open_time"].replace("Z", "+00:00"))
                close_ts = int(ct.timestamp())
                open_ts  = int(ot.timestamp())
            except Exception:
                skipped += 1
                continue

            history = fetch_market_history(
                series_ticker, mkt["ticker"],
                open_ts, close_ts,
                key_id=key_id, private_key=private_key,
            )
            if not history:
                skipped += 1
                continue

            result_yes_wins = mkt["result"] == "yes"
            strike = mkt["floor_strike"]

            records = simulate_s2_window(
                open_time_s=open_ts,
                close_time_s=close_ts,
                strike=strike,
                current_price=strike,
                history=history,
                result_yes_wins=result_yes_wins,
                cfg=cfg,
            )
            all_records.extend(records)

        if skipped:
            _log(f"[S2] {asset}: {skipped} markets skipped (no history / bad data)")
        _log(f"[S2] {asset}: {len(all_records):,} total records")

        table   = bucket_s2(all_records, min_vel_delta=cfg["min_vel_delta"])
        none_ct = sum(1 for v in table.values() if v is None)
        _log(f"[S2] {asset}: {len(table) - none_ct}/{len(table)} buckets populated")
        result[asset] = table

    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    assets  = ["BTC", "ETH", "SOL", "XRP", "DOGE"]
    skip_s1 = os.environ.get("SKIP_S1", "").strip() == "1"
    skip_s2 = os.environ.get("SKIP_S2", "").strip() == "1"

    s1_tables = {}
    if not skip_s1:
        _log("=" * 60)
        _log("PHASE 1: S1 calibration (Binance 1m data)")
        _log("=" * 60)
        s1_tables = run_s1_phase(assets)
    else:
        _log("S1 phase skipped (SKIP_S1=1)")

    s2_tables = {}
    if not skip_s2:
        _log("=" * 60)
        _log("PHASE 2: S2 calibration (Kalshi historical markets)")
        _log("=" * 60)
        try:
            key_id, private_key = _load_kalshi_key()
            s2_tables = run_s2_phase(assets, key_id, private_key)
        except RuntimeError as e:
            _log(f"S2 SKIPPED: {e}")
    else:
        _log("S2 phase skipped (SKIP_S2=1)")

    print()
    print("# " + "-" * 60)
    print("# PASTE INTO bot_strategy.py (replace existing _S1_WIN_RATE / _S2_WIN_RATE)")
    print("# " + "-" * 60)
    print()
    if not skip_s1:
        print("_S1_WIN_RATE: dict = {")
        for asset in assets:
            table = s1_tables.get(asset, {})
            print(f'    "{asset}": {_format_table(table)},')
        print("}")
    print()
    if not skip_s2:
        print("_S2_WIN_RATE: dict = {")
        for asset in assets:
            table = s2_tables.get(asset, {})
            print(f'    "{asset}": {_format_table(table)},')
        print("}")


if __name__ == "__main__":
    main()
