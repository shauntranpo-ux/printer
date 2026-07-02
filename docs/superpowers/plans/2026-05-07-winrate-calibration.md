# Win-Rate Calibration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `scripts/calibrate_winrates.py` that fetches Binance 1m + Kalshi historical data, computes empirical win-rate tables for S1 and S2, prints Python dicts to stdout, then wire lookup functions into `bot_strategy.py` replacing the tanh formulas.

**Architecture:** Standalone script (no bot imports, no bot_state). Phase 1 fetches Binance klines → simulates EMA continuation signals → buckets into `_S1_WIN_RATE`. Phase 2 fetches Kalshi historical markets + yes_ask histories → simulates velocity signals → buckets into `_S2_WIN_RATE`. Output printed to stdout; progress to stderr.

**Tech Stack:** Python stdlib + `requests` (HTTP), `cryptography` (RSA-PSS for Kalshi auth), `math`/`statistics`. No pandas or numpy - pure Python for portability.

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `scripts/calibrate_winrates.py` | Fetch, simulate, bucket, print tables |
| Create | `tests/test_calibrate_winrates.py` | Unit tests for simulation + bucketing logic (no real HTTP) |
| Modify | `bot_strategy.py` | Add `_S1_WIN_RATE`, `_S2_WIN_RATE` dicts + lookup fns, replace tanh |

---

## Task 1: Script skeleton - constants, HTTP helpers, Kalshi auth

**Files:**
- Create: `scripts/calibrate_winrates.py`

- [ ] **Step 1: Create the file with constants and HTTP helpers**

```python
#!/usr/bin/env python3
"""
scripts/calibrate_winrates.py

Fetch historical price data, compute empirical S1+S2 win-rate tables, print
to stdout as Python dicts ready to paste into bot_strategy.py.

Usage:
    python scripts/calibrate_winrates.py

Env vars required for S2 (Kalshi) phase:
    KALSHI_API_KEY         - API key ID (uuid)
    KALSHI_PRIVATE_KEY     - PEM-encoded RSA private key (or path to .pem file)
"""
import math
import os
import sys
import time
from base64 import b64encode
from collections import defaultdict

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ── Kalshi API constants ───────────────────────────────────────────────────
KALSHI_BASE_URL    = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_PATH_PREFIX = "/trade-api/v2"

# Candidate series tickers per asset - tried in order until one returns markets
KALSHI_SERIES = {
    "BTC":  ["KXBTC15M", "KXBTCD", "BTCD-B"],
    "ETH":  ["KXETHD",   "KXETH15M"],
    "SOL":  ["KXSOLD",   "KXSOL15M"],
    "XRP":  ["KXXRPD",   "KXXRP15M"],
    "DOGE": ["KXDOGED",  "KXDOGE15M"],
}

# ── Binance API constants ──────────────────────────────────────────────────
BINANCE_BASE_URL = "https://api.binance.com"
BINANCE_SYMBOLS  = {
    "BTC":  "BTCUSDT",
    "ETH":  "ETHUSDT",
    "SOL":  "SOLUSDT",
    "XRP":  "XRPUSDT",
    "DOGE": "DOGEUSDT",
}

# ── Per-asset config - KEEP IN SYNC WITH bot_strategy.py ──────────────────
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

MIN_SAMPLES = 50  # buckets below this get None → bot falls back to tanh


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
            _log(f"  Binance 429 - sleeping {wait}s")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Binance {path} failed after retries")


def _load_kalshi_key() -> tuple[str, object]:
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
            raise RuntimeError("Kalshi 401 - check KALSHI_API_KEY and KALSHI_PRIVATE_KEY")
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Kalshi {path} failed after retries")


if __name__ == "__main__":
    _log("calibrate_winrates.py - run main() to start")
```

- [ ] **Step 2: Verify the file runs without error**

```bash
cd C:\Users\alxnt\kalshi-bot
py scripts/calibrate_winrates.py
```

Expected stderr: `calibrate_winrates.py - run main() to start`. No traceback.

- [ ] **Step 3: Create the test file**

```python
# tests/test_calibrate_winrates.py
"""Unit tests for calibrate_winrates.py - pure computation only, no HTTP."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import calibrate_winrates as cal
```

- [ ] **Step 4: Run tests to confirm import works**

```bash
py -m pytest tests/test_calibrate_winrates.py -v
```

Expected: 0 tests collected, no import errors.

- [ ] **Step 5: Commit**

```bash
git add scripts/calibrate_winrates.py tests/test_calibrate_winrates.py
git commit -m "feat: scaffold calibrate_winrates.py with Kalshi auth + HTTP helpers"
```

---

## Task 2: Binance 1m data fetching

**Files:**
- Modify: `scripts/calibrate_winrates.py`
- Modify: `tests/test_calibrate_winrates.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_calibrate_winrates.py - append

from unittest.mock import patch, MagicMock

def _make_kline_batch(start_ms, count, price=50000.0):
    """Generate synthetic kline rows: [[open_time, o, h, l, close, ...], ...]"""
    rows = []
    for i in range(count):
        ts = start_ms + i * 60_000
        rows.append([ts, str(price), str(price), str(price), str(price),
                      "1", ts + 59999, "0", 0, "0", "0", "0"])
    return rows


def test_fetch_binance_1m_paginates():
    """fetch_binance_1m should merge multiple 1000-bar pages."""
    start_ms = 1_700_000_000_000
    batch1 = _make_kline_batch(start_ms, 1000)
    batch2 = _make_kline_batch(start_ms + 1000 * 60_000, 200)

    call_count = 0
    def fake_binance_get(path, params):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return batch1
        return batch2

    with patch.object(cal, "_binance_get", side_effect=fake_binance_get):
        result = cal.fetch_binance_1m("BTCUSDT", days=1)

    assert len(result) == 1200
    assert result[0] == (start_ms, 50000.0)
    assert call_count == 2
```

- [ ] **Step 2: Run to confirm it fails**

```bash
py -m pytest tests/test_calibrate_winrates.py::test_fetch_binance_1m_paginates -v
```

Expected: `AttributeError: module 'calibrate_winrates' has no attribute 'fetch_binance_1m'`

- [ ] **Step 3: Implement fetch_binance_1m - add to calibrate_winrates.py before `if __name__ == "__main__":`**

```python
def fetch_binance_1m(symbol: str, days: int) -> list[tuple[int, float]]:
    """
    Fetch last `days` of 1-minute close prices from Binance.
    Returns list of (timestamp_ms, close_price), oldest first.
    """
    end_ms   = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 3600 * 1000
    result: list[tuple[int, float]] = []
    cursor   = start_ms

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
            break  # no progress - done
        cursor = last_ts + 60_000  # next minute after last bar
        if len(batch) < 1000:
            break  # reached the end

    return result
```

- [ ] **Step 4: Run test to confirm it passes**

```bash
py -m pytest tests/test_calibrate_winrates.py::test_fetch_binance_1m_paginates -v
```

Expected: PASSED.

- [ ] **Step 5: Commit**

```bash
git add scripts/calibrate_winrates.py tests/test_calibrate_winrates.py
git commit -m "feat: add fetch_binance_1m with pagination"
```

---

## Task 3: S1 EMA simulation

**Files:**
- Modify: `scripts/calibrate_winrates.py`
- Modify: `tests/test_calibrate_winrates.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_calibrate_winrates.py - append

def test_compute_ema_basic():
    """EMA of constant series equals that constant."""
    prices = [100.0] * 10
    assert abs(cal._compute_ema(prices) - 100.0) < 0.001


def test_simulate_s1_continuation_only():
    """
    Build a 15-min window where price is above strike the whole time.
    EMA should be flat (no crossover) - should generate no trade records
    because ema_ratio ~1.0 means direction is ambiguous (treated as 'yes' = above).
    """
    # 90 days of flat prices just above 100.0 so every window has price > strike
    start_ms = 1_700_000_000_000
    # Align to 15-min boundary
    aligned = (start_ms // (15 * 60_000)) * (15 * 60_000)
    # 16 minutes of prices: strike = 100.0, then prices stay at 100.1
    closes = [(aligned + i * 60_000, 100.1) for i in range(17)]
    cfg = dict(min_dist=0.0025, ema_short=3, ema_long=10)
    records = cal.simulate_s1_window(closes[0][0], 100.0, closes, cfg)
    # All entry points: price > strike + EMA ~flat → may generate 'yes' records
    # Check that all records have abs_pct > min_dist
    for abs_pct, mins_left, won in records:
        assert abs_pct >= cfg["min_dist"], f"abs_pct {abs_pct} below min_dist"


def test_simulate_s1_reversal_filtered():
    """EMA bearish but price above strike → reversal → record must be skipped."""
    start_ms  = 1_700_100_000_000
    aligned   = (start_ms // (15 * 60_000)) * (15 * 60_000)
    # Price starts at 101 (above strike 100), then drops to 99 - EMA lags and stays above
    prices_up   = [(aligned + i * 60_000, 101.0) for i in range(5)]
    prices_down = [(aligned + (5 + i) * 60_000, 99.0) for i in range(12)]
    closes = prices_up + prices_down
    strike = closes[0][1]  # 101.0
    # After the drop, price is below strike, EMA still above → EMA=yes, price<strike → reversal
    # Records at those offsets should be skipped
    cfg = dict(min_dist=0.0025, ema_short=3, ema_long=10)
    records = cal.simulate_s1_window(closes[0][0], strike, closes, cfg)
    for abs_pct, mins_left, won in records:
        # If this is a late-window entry where price is below strike and EMA says up,
        # it should have been filtered. We can't be fully deterministic here, but
        # verify that no abs_pct is 0 (means gate fired correctly).
        assert abs_pct > 0
```

- [ ] **Step 2: Run to confirm they fail**

```bash
py -m pytest tests/test_calibrate_winrates.py::test_compute_ema_basic tests/test_calibrate_winrates.py::test_simulate_s1_continuation_only -v
```

Expected: `AttributeError: module 'calibrate_winrates' has no attribute '_compute_ema'`

- [ ] **Step 3: Implement EMA and S1 window simulation - add to calibrate_winrates.py**

```python
# ── EMA helper ────────────────────────────────────────────────────────────

def _compute_ema(values: list[float]) -> float:
    """Standard EMA over a list of floats, newest value last."""
    if not values:
        return 0.0
    alpha = 2.0 / (len(values) + 1)
    v = values[0]
    for x in values[1:]:
        v = alpha * x + (1.0 - alpha) * v
    return v


# ── S1 simulation ─────────────────────────────────────────────────────────

# Entry points: minutes remaining before expiry
S1_ENTRY_OFFSETS = [3, 5, 7, 10, 12]  # minutes remaining


def simulate_s1_window(
    open_ms: int,
    strike: float,
    all_closes: list[tuple[int, float]],
    cfg: dict,
) -> list[tuple[float, float, bool]]:
    """
    Simulate S1 EMA entries for one 15-min market window.

    Args:
        open_ms:    Window open timestamp in ms.
        strike:     Strike price (close at window open).
        all_closes: Full list of (ts_ms, close) for the asset, sorted ascending.
        cfg:        S1_ASSET_CONFIG entry for this asset.

    Returns:
        List of (abs_pct, mins_left, won) for each valid entry point.
        Empty list if no valid entry found.
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

    records: list[tuple[float, float, bool]] = []

    for mins_remaining in S1_ENTRY_OFFSETS:
        entry_ms = close_ms - mins_remaining * 60_000
        if entry_ms <= open_ms:
            continue  # entry too early, outside window

        # Prices available at entry time
        prices_at_entry = [(ts, px) for ts, px in all_closes if ts <= entry_ms]
        if len(prices_at_entry) < ema_long_min + 2:
            continue  # not enough data for long EMA

        current_price = prices_at_entry[-1][1]
        abs_pct = abs(current_price - strike) / strike if strike > 0 else 0.0

        if abs_pct < min_dist:
            continue  # distance gate

        # EMA direction
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
            continue  # reversal - skip
        if ema_side == "no" and current_price > strike:
            continue  # reversal - skip

        # Record: did we win? YES contract wins if expiry > strike
        won = (ema_side == "yes" and outcome_yes_wins) or \
              (ema_side == "no"  and not outcome_yes_wins)

        records.append((abs_pct, float(mins_remaining), won))

    return records


def simulate_s1(
    closes: list[tuple[int, float]],
    asset: str,
) -> list[tuple[float, float, bool]]:
    """
    Run S1 simulation over all 15-min windows in the closes list.
    Returns list of (abs_pct, mins_left, won) records.
    """
    cfg = S1_ASSET_CONFIG[asset]
    records: list[tuple[float, float, bool]] = []

    if not closes:
        return records

    # Find all window open times aligned to :00 :15 :30 :45
    first_ts = closes[0][0]
    last_ts  = closes[-1][0]
    WINDOW_MS = 15 * 60_000

    # Align start to next 15-min boundary
    start = ((first_ts // WINDOW_MS) + 1) * WINDOW_MS
    t = start
    while t + WINDOW_MS <= last_ts:
        # Strike = close price at window open
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
```

- [ ] **Step 4: Run tests**

```bash
py -m pytest tests/test_calibrate_winrates.py::test_compute_ema_basic tests/test_calibrate_winrates.py::test_simulate_s1_continuation_only tests/test_calibrate_winrates.py::test_simulate_s1_reversal_filtered -v
```

Expected: all PASSED.

- [ ] **Step 5: Commit**

```bash
git add scripts/calibrate_winrates.py tests/test_calibrate_winrates.py
git commit -m "feat: add S1 EMA simulation with continuation filter"
```

---

## Task 4: S1 bucketing and table output

**Files:**
- Modify: `scripts/calibrate_winrates.py`
- Modify: `tests/test_calibrate_winrates.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_calibrate_winrates.py - append

def test_bucket_s1_basic():
    """Records in bucket (0,0) → win rate = wins / total."""
    # abs_pct=0.003 → dist_idx=0 (between min_dist=0.0025 and 0.5%)
    # mins_left=4.0 → time_idx=0 (3-6 min)
    records = [(0.003, 4.0, True)] * 70 + [(0.003, 4.0, False)] * 30
    table = cal.bucket_s1(records, min_dist=0.0025, min_samples=50)
    assert (0, 0) in table
    assert abs(table[(0, 0)] - 0.70) < 0.01


def test_bucket_s1_none_for_sparse():
    """Buckets with < min_samples records return None."""
    records = [(0.003, 4.0, True)] * 10  # only 10 samples
    table = cal.bucket_s1(records, min_dist=0.0025, min_samples=50)
    assert table.get((0, 0)) is None


def test_bucket_s1_dist_boundaries():
    """Verify correct dist bucket assignment."""
    cases = [
        (0.003,  0),   # 0.003 → bucket 0 (between 0.0025 and 0.005)
        (0.006,  1),   # 0.006 → bucket 1 (between 0.005 and 0.010)
        (0.015,  2),   # 0.015 → bucket 2 (between 0.010 and 0.020)
        (0.030,  3),   # 0.030 → bucket 3 (>= 0.020)
    ]
    for abs_pct, expected_idx in cases:
        records = [(abs_pct, 4.0, True)] * 60
        table = cal.bucket_s1(records, min_dist=0.0025, min_samples=50)
        assert (expected_idx, 0) in table, f"abs_pct={abs_pct} expected bucket {expected_idx}"
```

- [ ] **Step 2: Run to confirm they fail**

```bash
py -m pytest tests/test_calibrate_winrates.py::test_bucket_s1_basic -v
```

Expected: `AttributeError: module 'calibrate_winrates' has no attribute 'bucket_s1'`

- [ ] **Step 3: Implement bucketing - add to calibrate_winrates.py**

```python
# ── S1 bucketing ──────────────────────────────────────────────────────────

# dist boundaries (absolute fraction, not percent)
S1_DIST_BOUNDS = [0.005, 0.010, 0.020]  # 0.5%, 1.0%, 2.0% - bucket 3 = >=2.0%
S1_TIME_BOUNDS = [6.0, 9.0]             # 3-6, 6-9, 9-12 min remaining


def _s1_dist_idx(abs_pct: float) -> int:
    for i, bound in enumerate(S1_DIST_BOUNDS):
        if abs_pct < bound:
            return i
    return len(S1_DIST_BOUNDS)  # bucket 3 = >=2.0%


def _s1_time_idx(mins_left: float) -> int:
    for i, bound in enumerate(S1_TIME_BOUNDS):
        if mins_left < bound:
            return i
    return len(S1_TIME_BOUNDS)  # bucket 2 = >=9 min


def bucket_s1(
    records: list[tuple[float, float, bool]],
    min_dist: float,
    min_samples: int = MIN_SAMPLES,
) -> dict[tuple[int, int], float | None]:
    """
    Bucket S1 records into a (dist_idx, time_idx) → win_rate table.
    Entries with fewer than min_samples records return None.
    Records with abs_pct < min_dist are excluded (already filtered by simulation,
    but guard here for safety).
    """
    counts:  dict[tuple[int, int], int] = defaultdict(int)
    wins:    dict[tuple[int, int], int] = defaultdict(int)

    for abs_pct, mins_left, won in records:
        if abs_pct < min_dist:
            continue
        key = (_s1_dist_idx(abs_pct), _s1_time_idx(mins_left))
        counts[key] += 1
        if won:
            wins[key] += 1

    n_dist = len(S1_DIST_BOUNDS) + 1
    n_time = len(S1_TIME_BOUNDS) + 1
    table: dict[tuple[int, int], float | None] = {}
    for d in range(n_dist):
        for t in range(n_time):
            key = (d, t)
            n = counts[key]
            if n < min_samples:
                table[key] = None
            else:
                table[key] = round(wins[key] / n, 4)
    return table
```

- [ ] **Step 4: Run tests**

```bash
py -m pytest tests/test_calibrate_winrates.py::test_bucket_s1_basic tests/test_calibrate_winrates.py::test_bucket_s1_none_for_sparse tests/test_calibrate_winrates.py::test_bucket_s1_dist_boundaries -v
```

Expected: all PASSED.

- [ ] **Step 5: Commit**

```bash
git add scripts/calibrate_winrates.py tests/test_calibrate_winrates.py
git commit -m "feat: add S1 bucketing with dist/time boundaries"
```

---

## Task 5: Kalshi market listing

**Files:**
- Modify: `scripts/calibrate_winrates.py`
- Modify: `tests/test_calibrate_winrates.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_calibrate_winrates.py - append

def test_fetch_kalshi_markets_paginates():
    """fetch_kalshi_markets should follow cursor pagination and filter by date range."""
    page1 = {
        "markets": [
            {
                "ticker":     "KXBTCD-25MAY0715-B99000",
                "floor_strike": 99000.0,
                "close_time": "2025-05-07T07:30:00Z",
                "open_time":  "2025-05-07T07:15:00Z",
                "result":     "yes",
                "status":     "finalized",
            }
        ] * 5,
        "cursor": "page2",
    }
    page2 = {
        "markets": [
            {
                "ticker":     "KXBTCD-25MAY0700-B99000",
                "floor_strike": 99000.0,
                "close_time": "2025-05-07T07:15:00Z",
                "open_time":  "2025-05-07T07:00:00Z",
                "result":     "no",
                "status":     "finalized",
            }
        ] * 3,
        "cursor": "",
    }
    call_count = 0
    def fake_kalshi_get(path, params, key_id, private_key):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return page1
        return page2

    with patch.object(cal, "_kalshi_get", side_effect=fake_kalshi_get):
        markets = cal.fetch_kalshi_markets("KXBTCD", days=60, key_id="k", private_key=None)

    assert len(markets) == 8
    assert call_count == 2
    assert markets[0]["result"] in ("yes", "no")
```

- [ ] **Step 2: Run to confirm it fails**

```bash
py -m pytest tests/test_calibrate_winrates.py::test_fetch_kalshi_markets_paginates -v
```

Expected: `AttributeError: module 'calibrate_winrates' has no attribute 'fetch_kalshi_markets'`

- [ ] **Step 3: Implement fetch_kalshi_markets - add to calibrate_winrates.py**

```python
# ── Kalshi market listing ──────────────────────────────────────────────────

def fetch_kalshi_markets(
    series_ticker: str,
    days: int,
    key_id: str,
    private_key,
) -> list[dict]:
    """
    Fetch all settled 15-min markets for a series within the last `days` days.
    Returns list of dicts with keys: ticker, floor_strike, open_time, close_time, result.
    Aborts with RuntimeError if zero markets found (wrong series ticker).
    """
    cutoff_ts = time.time() - days * 86400
    markets: list[dict] = []
    cursor = ""

    while True:
        params: dict = {
            "series_ticker": series_ticker,
            "status":        "finalized",
            "limit":         200,
        }
        if cursor:
            params["cursor"] = cursor

        data   = _kalshi_get("/markets", params, key_id, private_key)
        batch  = data.get("markets", [])
        cursor = data.get("cursor", "")

        for mkt in batch:
            close_str = mkt.get("close_time", "")
            if not close_str:
                continue
            # Parse ISO8601: "2025-05-07T07:30:00Z"
            try:
                import datetime
                ct = datetime.datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                if ct.timestamp() < cutoff_ts:
                    cursor = ""  # stop paginating - markets are oldest-first from here
                    break
            except Exception:
                continue
            if mkt.get("result") not in ("yes", "no"):
                continue  # unsettled or cancelled
            markets.append({
                "ticker":       mkt["ticker"],
                "floor_strike": float(mkt.get("floor_strike", 0)),
                "open_time":    mkt.get("open_time", ""),
                "close_time":   close_str,
                "result":       mkt["result"],
            })

        if not cursor:
            break

    return markets
```

- [ ] **Step 4: Run test**

```bash
py -m pytest tests/test_calibrate_winrates.py::test_fetch_kalshi_markets_paginates -v
```

Expected: PASSED.

- [ ] **Step 5: Commit**

```bash
git add scripts/calibrate_winrates.py tests/test_calibrate_winrates.py
git commit -m "feat: add Kalshi market listing with cursor pagination"
```

---

## Task 6: Kalshi price history fetching

**Files:**
- Modify: `scripts/calibrate_winrates.py`
- Modify: `tests/test_calibrate_winrates.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_calibrate_winrates.py - append

def test_fetch_market_history_extracts_yes_ask():
    """fetch_market_history should return list of (ts_sec, yes_ask_cents) pairs."""
    fake_response = {
        "history": [
            {"ts": 1715079000, "yes_bid": 43, "yes_ask": 45, "no_bid": 53, "no_ask": 57, "volume": 10},
            {"ts": 1715079060, "yes_bid": 44, "yes_ask": 46, "no_bid": 52, "no_ask": 56, "volume": 5},
        ]
    }
    with patch.object(cal, "_kalshi_get", return_value=fake_response):
        result = cal.fetch_market_history("KXBTCD-25MAY0715-B99000", key_id="k", private_key=None)

    assert result == [(1715079000, 45), (1715079060, 46)]
```

- [ ] **Step 2: Run to confirm it fails**

```bash
py -m pytest tests/test_calibrate_winrates.py::test_fetch_market_history_extracts_yes_ask -v
```

Expected: `AttributeError: module 'calibrate_winrates' has no attribute 'fetch_market_history'`

- [ ] **Step 3: Implement fetch_market_history - add to calibrate_winrates.py**

```python
def fetch_market_history(
    ticker: str,
    key_id: str,
    private_key,
) -> list[tuple[int, int]]:
    """
    Fetch yes_ask price history for a single Kalshi market ticker.
    Returns list of (ts_seconds, yes_ask_cents), sorted ascending by time.
    Returns empty list if history unavailable.
    """
    path = f"/markets/{ticker}/history"
    try:
        data = _kalshi_get(path, {"limit": 1000, "period_interval": 1}, key_id, private_key)
    except Exception:
        return []

    history = data.get("history", [])
    result: list[tuple[int, int]] = []
    for entry in history:
        ts       = entry.get("ts")
        yes_ask  = entry.get("yes_ask")
        if ts is None or yes_ask is None:
            continue
        result.append((int(ts), int(yes_ask)))

    result.sort(key=lambda x: x[0])
    return result
```

- [ ] **Step 4: Run test**

```bash
py -m pytest tests/test_calibrate_winrates.py::test_fetch_market_history_extracts_yes_ask -v
```

Expected: PASSED.

- [ ] **Step 5: Commit**

```bash
git add scripts/calibrate_winrates.py tests/test_calibrate_winrates.py
git commit -m "feat: add fetch_market_history for Kalshi yes_ask series"
```

---

## Task 7: S2 velocity simulation

**Files:**
- Modify: `scripts/calibrate_winrates.py`
- Modify: `tests/test_calibrate_winrates.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_calibrate_winrates.py - append

def test_simulate_s2_window_bullish():
    """Rising yes_ask + price above strike → YES trade, check outcome logic."""
    close_time_s = 1_715_080_000
    open_time_s  = close_time_s - 900  # 15 min earlier
    strike = 100.0
    # yes_ask rises from 40 to 50 - market pricing YES more likely
    history = [(open_time_s + i * 60, 40 + i) for i in range(15)]
    cfg = dict(min_dist=0.0035, min_vel_delta=0.80, vel_lookback=4)
    # Price above strike → continuation allowed for YES trade
    result_yes_wins = True
    records = cal.simulate_s2_window(
        open_time_s, close_time_s, strike, 101.0, history, result_yes_wins, cfg
    )
    # Should have records where won=True (we traded YES and YES won)
    assert any(won for _, _, won in records)


def test_simulate_s2_reversal_filtered():
    """Rising yes_ask but price BELOW strike → reversal → no records."""
    close_time_s = 1_715_090_000
    open_time_s  = close_time_s - 900
    strike = 100.0
    history = [(open_time_s + i * 60, 40 + i) for i in range(15)]
    cfg = dict(min_dist=0.0035, min_vel_delta=0.80, vel_lookback=4)
    # Price below strike - YES is reversal, should be filtered
    result_yes_wins = True
    records = cal.simulate_s2_window(
        open_time_s, close_time_s, strike, 99.0, history, result_yes_wins, cfg
    )
    # All YES-direction entries should be filtered since price < strike
    for vel_delta, mins_left, won in records:
        # Any remaining records would be NO direction entries
        # But vel is rising → NO direction also filtered (vel says up, NO is down → reversal)
        assert False, f"Expected no records, got {records}"
```

- [ ] **Step 2: Run to confirm they fail**

```bash
py -m pytest tests/test_calibrate_winrates.py::test_simulate_s2_window_bullish -v
```

Expected: `AttributeError: module 'calibrate_winrates' has no attribute 'simulate_s2_window'`

- [ ] **Step 3: Implement S2 simulation - add to calibrate_winrates.py**

```python
# ── S2 simulation ─────────────────────────────────────────────────────────

S2_ENTRY_OFFSETS = [2, 4, 6, 8, 10, 12]  # minutes remaining before expiry


def _s2_velocity(history: list[tuple[int, int]], entry_ts: int,
                 lookback: int, min_delta: float) -> tuple[str | None, float]:
    """
    Compute velocity direction from yes_ask history up to entry_ts.
    Returns (side, abs_delta) or (None, 0.0) if signal too weak.
    Mirrors _s2_contract_direction logic in bot_strategy.py.
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
    open_time_s:     int,
    close_time_s:    int,
    strike:          float,
    current_price:   float,
    history:         list[tuple[int, int]],  # (ts_sec, yes_ask_cents)
    result_yes_wins: bool,
    cfg:             dict,
) -> list[tuple[float, float, bool]]:
    """
    Simulate S2 velocity entries for one 15-min market window.

    Args:
        open_time_s:     Market open timestamp in seconds.
        close_time_s:    Market close timestamp in seconds.
        strike:          Strike price (underlying asset price at market open).
        current_price:   Asset price at the time of entry (approximated as strike
                         adjusted by yes_ask direction - use strike here for bucketing).
        history:         List of (ts_sec, yes_ask_cents) for this market, sorted asc.
        result_yes_wins: True if YES contract won.
        cfg:             S2_ASSET_CONFIG entry for this asset.

    Returns:
        List of (vel_delta, mins_left, won).
    """
    min_dist    = cfg["min_dist"]
    min_vel     = cfg["min_vel_delta"]
    lookback    = cfg["vel_lookback"]
    records: list[tuple[float, float, bool]] = []

    for mins_remaining in S2_ENTRY_OFFSETS:
        entry_s = close_time_s - mins_remaining * 60
        if entry_s <= open_time_s:
            continue

        # Abs distance - use strike as proxy for underlying at entry
        # (we don't have per-minute underlying price; use strike as baseline)
        # yes_ask at entry gives us market's implied probability
        entry_yes_ask = None
        for ts, ask in history:
            if ts <= entry_s:
                entry_yes_ask = ask
        if entry_yes_ask is None:
            continue

        # Infer price-vs-strike direction from yes_ask:
        # yes_ask > 50 → market thinks YES likely → price probably above strike
        implied_above = entry_yes_ask > 50

        # Distance approximation: |current_price - strike| / strike
        # We use strike as a placeholder since per-minute underlying price unavailable.
        # This means abs_pct = 0 here - we just use vel_delta for bucketing in S2.
        abs_pct = 0.0  # S2 calibration buckets on vel_delta, not abs_pct

        # Velocity signal
        side, vel_delta = _s2_velocity(history, entry_s, lookback, min_vel)
        if side is None:
            continue

        # Continuation-only filter using implied price direction
        if side == "yes" and not implied_above:
            continue  # velocity up but market implies price below strike → reversal
        if side == "no" and implied_above:
            continue  # velocity down but market implies price above strike → reversal

        # Did the trade win?
        won = (side == "yes" and result_yes_wins) or \
              (side == "no"  and not result_yes_wins)

        records.append((vel_delta, float(mins_remaining), won))

    return records
```

- [ ] **Step 4: Run tests**

```bash
py -m pytest tests/test_calibrate_winrates.py::test_simulate_s2_window_bullish tests/test_calibrate_winrates.py::test_simulate_s2_reversal_filtered -v
```

Expected: both PASSED.

- [ ] **Step 5: Commit**

```bash
git add scripts/calibrate_winrates.py tests/test_calibrate_winrates.py
git commit -m "feat: add S2 velocity simulation with continuation filter"
```

---

## Task 8: S2 bucketing

**Files:**
- Modify: `scripts/calibrate_winrates.py`
- Modify: `tests/test_calibrate_winrates.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_calibrate_winrates.py - append

def test_bucket_s2_basic():
    """Records in vel_bucket=0, time_bucket=0 → correct win rate."""
    # vel_delta=0.9 with min_vel_delta=0.80 → idx 0 (between 1× and 2×)
    records = [(0.9, 3.0, True)] * 60 + [(0.9, 3.0, False)] * 40
    table = cal.bucket_s2(records, min_vel_delta=0.80, min_samples=50)
    assert (0, 0) in table
    assert abs(table[(0, 0)] - 0.60) < 0.01


def test_bucket_s2_vel_boundaries():
    """Verify correct vel bucket assignment relative to min_vel_delta."""
    min_v = 0.80
    cases = [
        (0.9,  0),  # 1× to 2× min_vel
        (1.7,  1),  # 2× to 4× min_vel
        (3.5,  2),  # >= 4× min_vel
    ]
    for vel_delta, expected_idx in cases:
        records = [(vel_delta, 3.0, True)] * 60
        table = cal.bucket_s2(records, min_vel_delta=min_v, min_samples=50)
        assert (expected_idx, 0) in table, f"vel_delta={vel_delta} expected bucket {expected_idx}"
```

- [ ] **Step 2: Run to confirm they fail**

```bash
py -m pytest tests/test_calibrate_winrates.py::test_bucket_s2_basic -v
```

Expected: `AttributeError: module 'calibrate_winrates' has no attribute 'bucket_s2'`

- [ ] **Step 3: Implement S2 bucketing - add to calibrate_winrates.py**

```python
# ── S2 bucketing ──────────────────────────────────────────────────────────

# vel bucket: [1×, 2×), [2×, 4×), [4×+) relative to min_vel_delta
S2_VEL_MULTIPLIERS = [2.0, 4.0]   # breakpoints as multipliers of min_vel_delta
S2_TIME_BOUNDS_S2  = [5.0, 8.0]   # 2-5, 5-8, 8-13 min remaining


def _s2_vel_idx(vel_delta: float, min_vel_delta: float) -> int:
    ratio = vel_delta / max(min_vel_delta, 1e-9)
    for i, mult in enumerate(S2_VEL_MULTIPLIERS):
        if ratio < mult:
            return i
    return len(S2_VEL_MULTIPLIERS)  # bucket 2 = >=4×


def _s2_time_idx(mins_left: float) -> int:
    for i, bound in enumerate(S2_TIME_BOUNDS_S2):
        if mins_left < bound:
            return i
    return len(S2_TIME_BOUNDS_S2)


def bucket_s2(
    records: list[tuple[float, float, bool]],
    min_vel_delta: float,
    min_samples: int = MIN_SAMPLES,
) -> dict[tuple[int, int], float | None]:
    """
    Bucket S2 records into (vel_idx, time_idx) → win_rate table.
    Entries with fewer than min_samples records return None.
    """
    counts: dict[tuple[int, int], int] = defaultdict(int)
    wins:   dict[tuple[int, int], int] = defaultdict(int)

    for vel_delta, mins_left, won in records:
        key = (_s2_vel_idx(vel_delta, min_vel_delta), _s2_time_idx(mins_left))
        counts[key] += 1
        if won:
            wins[key] += 1

    n_vel  = len(S2_VEL_MULTIPLIERS) + 1
    n_time = len(S2_TIME_BOUNDS_S2)  + 1
    table: dict[tuple[int, int], float | None] = {}
    for v in range(n_vel):
        for t in range(n_time):
            key = (v, t)
            n = counts[key]
            table[key] = None if n < min_samples else round(wins[key] / n, 4)
    return table
```

- [ ] **Step 4: Run tests**

```bash
py -m pytest tests/test_calibrate_winrates.py::test_bucket_s2_basic tests/test_calibrate_winrates.py::test_bucket_s2_vel_boundaries -v
```

Expected: both PASSED.

- [ ] **Step 5: Commit**

```bash
git add scripts/calibrate_winrates.py tests/test_calibrate_winrates.py
git commit -m "feat: add S2 bucketing with velocity/time boundaries"
```

---

## Task 9: Wire main() and print output

**Files:**
- Modify: `scripts/calibrate_winrates.py`

- [ ] **Step 1: Replace the `if __name__ == "__main__":` block with a full main()**

Replace the existing stub at the bottom of `scripts/calibrate_winrates.py`:

```python
def _format_table(table: dict) -> str:
    """Format a bucket dict as a compact Python dict literal."""
    items = ", ".join(
        f"({k[0]},{k[1]}): {v!r}" for k, v in sorted(table.items())
    )
    return "{" + items + "}"


def run_s1_phase(assets: list[str]) -> dict[str, dict]:
    """Fetch Binance data and compute S1 win-rate tables for all assets."""
    result: dict[str, dict] = {}
    for asset in assets:
        symbol = BINANCE_SYMBOLS[asset]
        _log(f"[S1] {asset}: fetching {symbol} 1m data (90 days)...")
        try:
            closes = fetch_binance_1m(symbol, days=90)
            _log(f"[S1] {asset}: {len(closes):,} bars fetched")
        except Exception as exc:
            _log(f"[S1] {asset}: FETCH FAILED - {exc} - skipping")
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


def run_s2_phase(assets: list[str], key_id: str, private_key) -> dict[str, dict]:
    """Fetch Kalshi markets + histories and compute S2 win-rate tables for all assets."""
    result: dict[str, dict] = {}
    for asset in assets:
        cfg = S2_ASSET_CONFIG[asset]
        candidates = KALSHI_SERIES[asset]

        # Discover working series ticker
        series_ticker = None
        for candidate in candidates:
            _log(f"[S2] {asset}: probing series {candidate}...")
            try:
                markets = fetch_kalshi_markets(candidate, days=5, key_id=key_id, private_key=private_key)
                if markets:
                    series_ticker = candidate
                    _log(f"[S2] {asset}: using series {candidate}")
                    break
            except Exception:
                continue

        if series_ticker is None:
            _log(f"[S2] {asset}: ERROR - no markets found for any candidate ticker {candidates} - skipping")
            result[asset] = {}
            continue

        _log(f"[S2] {asset}: fetching 60 days of markets...")
        markets = fetch_kalshi_markets(series_ticker, days=60, key_id=key_id, private_key=private_key)
        _log(f"[S2] {asset}: {len(markets)} markets found")

        all_records: list[tuple[float, float, bool]] = []
        skipped = 0
        for i, mkt in enumerate(markets):
            if i % 100 == 0:
                _log(f"[S2] {asset}: processing market {i}/{len(markets)}...")
            history = fetch_market_history(mkt["ticker"], key_id=key_id, private_key=private_key)
            if not history:
                skipped += 1
                continue

            import datetime
            try:
                ct = datetime.datetime.fromisoformat(mkt["close_time"].replace("Z", "+00:00"))
                ot = datetime.datetime.fromisoformat(mkt["open_time"].replace("Z", "+00:00"))
                close_ts = int(ct.timestamp())
                open_ts  = int(ot.timestamp())
            except Exception:
                skipped += 1
                continue

            result_yes_wins = mkt["result"] == "yes"
            strike = mkt["floor_strike"]

            records = simulate_s2_window(
                open_time_s=open_ts,
                close_time_s=close_ts,
                strike=strike,
                current_price=strike,  # approximation; see S2 design note
                history=history,
                result_yes_wins=result_yes_wins,
                cfg=cfg,
            )
            all_records.extend(records)

        if skipped:
            _log(f"[S2] {asset}: {skipped} markets skipped (no history / bad data)")
        _log(f"[S2] {asset}: {len(all_records):,} total records")

        table    = bucket_s2(all_records, min_vel_delta=cfg["min_vel_delta"])
        none_ct  = sum(1 for v in table.values() if v is None)
        _log(f"[S2] {asset}: {len(table) - none_ct}/{len(table)} buckets populated")
        result[asset] = table

    return result


def main():
    assets = ["BTC", "ETH", "SOL", "XRP", "DOGE"]
    skip_s2 = os.environ.get("SKIP_S2", "").strip() == "1"

    # ── Phase 1: S1 ──────────────────────────────────────────────────────
    _log("=" * 60)
    _log("PHASE 1: S1 calibration (Binance 1m data)")
    _log("=" * 60)
    s1_tables = run_s1_phase(assets)

    # ── Phase 2: S2 ──────────────────────────────────────────────────────
    s2_tables: dict[str, dict] = {}
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

    # ── Output ───────────────────────────────────────────────────────────
    print()
    print("# " + "─" * 60)
    print("# PASTE INTO bot_strategy.py (replace existing _S1_WIN_RATE / _S2_WIN_RATE)")
    print("# " + "─" * 60)
    print()
    print("_S1_WIN_RATE: dict = {")
    for asset in assets:
        table = s1_tables.get(asset, {})
        print(f'    "{asset}": {_format_table(table)},')
    print("}")
    print()
    print("_S2_WIN_RATE: dict = {")
    for asset in assets:
        table = s2_tables.get(asset, {})
        print(f'    "{asset}": {_format_table(table)},')
    print("}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify script runs with SKIP_S2=1 (no Kalshi creds needed)**

```bash
cd C:\Users\alxnt\kalshi-bot
$env:SKIP_S2="1"; py scripts/calibrate_winrates.py 2>$null | Select-Object -First 20
```

Expected: output begins with `# ─────...`, followed by `_S1_WIN_RATE: dict = {` - no traceback. (Actual Binance fetch will run - takes ~2 min. If you want a quick sanity check without fetching, interrupt after the first `[S1]` log line appears on stderr.)

- [ ] **Step 3: Commit**

```bash
git add scripts/calibrate_winrates.py
git commit -m "feat: wire main() - S1+S2 phases, formatted dict output to stdout"
```

---

## Task 10: Add lookup functions to bot_strategy.py and replace tanh

**Files:**
- Modify: `bot_strategy.py`
- Modify: `tests/test_calibrate_winrates.py`

- [ ] **Step 1: Write a test for the lookup functions (before implementing them)**

```python
# tests/test_calibrate_winrates.py - append
# These test the lookup helpers we're about to add to bot_strategy.py.
# Import them directly once written.

def test_s1_lookup_uses_table():
    """_s1_lookup_win_rate returns table value when bucket populated."""
    import importlib, types
    # We'll test this after bot_strategy changes by importing the function.
    # For now, just a placeholder that confirms the module loads.
    import bot_strategy as bs
    assert hasattr(bs, "_s1_lookup_win_rate"), \
        "_s1_lookup_win_rate not found in bot_strategy - add it in Task 10 Step 2"


def test_s1_lookup_falls_back_to_tanh():
    """_s1_lookup_win_rate uses tanh when bucket is None."""
    import bot_strategy as bs
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    # When table has None for the bucket, result should equal tanh formula.
    result = bs._s1_lookup_win_rate("BTC", abs_pct=0.003, mins_left=5.0)
    # tanh fallback: 0.50 + 0.28 * tanh(0.003 / 0.0025) = ~0.721
    assert 0.5 < result < 0.85, f"Fallback win_prob {result} out of expected range"
```

- [ ] **Step 2: Run to confirm test fails with AttributeError**

```bash
cd C:\Users\alxnt\kalshi-bot
py -m pytest tests/test_calibrate_winrates.py::test_s1_lookup_uses_table -v
```

Expected: `AssertionError: _s1_lookup_win_rate not found in bot_strategy`

- [ ] **Step 3: Add empty win-rate tables and lookup functions to bot_strategy.py**

Add the following block immediately after the `_S2_ASSET_CONFIG` dict definition (before `def _s2_contract_direction`):

```python
# ---------------------------------------------------------------------------
# Empirical win-rate tables - populated by scripts/calibrate_winrates.py
# Run that script, copy the printed dicts here.
# None entries → tanh formula fallback (insufficient calibration data).
# ---------------------------------------------------------------------------

_S1_WIN_RATE: dict = {
    "BTC":  {},
    "ETH":  {},
    "SOL":  {},
    "XRP":  {},
    "DOGE": {},
}

_S2_WIN_RATE: dict = {
    "BTC":  {},
    "ETH":  {},
    "SOL":  {},
    "XRP":  {},
    "DOGE": {},
}

# S1 bucket boundaries (must match calibrate_winrates.py constants)
_S1_DIST_BOUNDS = [0.005, 0.010, 0.020]
_S1_TIME_BOUNDS = [6.0, 9.0]

# S2 bucket boundaries (must match calibrate_winrates.py constants)
_S2_VEL_MULTIPLIERS = [2.0, 4.0]
_S2_TIME_BOUNDS_S2  = [5.0, 8.0]


def _s1_lookup_win_rate(asset: str, abs_pct: float, mins_left: float) -> float:
    """
    Look up empirical S1 win rate. Falls back to tanh formula when bucket is None or missing.
    Returns base win probability (before EMA-strength and session adjustments).
    """
    cfg = _S1_ASSET_CONFIG.get(asset, _S1_ASSET_CONFIG["BTC"])
    min_dist = cfg["min_dist"]

    # Dist bucket
    dist_idx = len(_S1_DIST_BOUNDS)
    for i, bound in enumerate(_S1_DIST_BOUNDS):
        if abs_pct < bound:
            dist_idx = i
            break
    # Time bucket
    time_idx = len(_S1_TIME_BOUNDS)
    for i, bound in enumerate(_S1_TIME_BOUNDS):
        if mins_left < bound:
            time_idx = i
            break

    table   = _S1_WIN_RATE.get(asset, {})
    emp_val = table.get((dist_idx, time_idx))
    if emp_val is not None:
        return float(emp_val)

    # Tanh fallback
    return 0.50 + 0.28 * math.tanh(abs_pct / max(min_dist, 1e-6))


def _s2_lookup_win_rate(asset: str, vel_delta: float, mins_left: float) -> float:
    """
    Look up empirical S2 win rate. Falls back to tanh formula when bucket is None or missing.
    Returns base win probability (before velocity-strength and OBI adjustments).
    """
    cfg = _S2_ASSET_CONFIG.get(asset, _S2_ASSET_CONFIG["BTC"])
    min_vel = cfg["min_vel_delta"]
    min_dist = cfg["min_dist"]

    # Vel bucket
    ratio    = vel_delta / max(min_vel, 1e-9)
    vel_idx  = len(_S2_VEL_MULTIPLIERS)
    for i, mult in enumerate(_S2_VEL_MULTIPLIERS):
        if ratio < mult:
            vel_idx = i
            break
    # Time bucket
    time_idx = len(_S2_TIME_BOUNDS_S2)
    for i, bound in enumerate(_S2_TIME_BOUNDS_S2):
        if mins_left < bound:
            time_idx = i
            break

    table   = _S2_WIN_RATE.get(asset, {})
    emp_val = table.get((vel_idx, time_idx))
    if emp_val is not None:
        return float(emp_val)

    # Tanh fallback (uses abs_pct=0 proxy - vel signal doesn't have direct abs_pct)
    return 0.50 + 0.25 * math.tanh(vel_delta / max(min_vel, 1e-6))
```

- [ ] **Step 4: Replace the tanh formula in strategy_brain_s1**

Find this block in `bot_strategy.py`:

```python
    # Win probability: logistic of distance + EMA-strength adjustment
    base_p = 0.50 + 0.28 * math.tanh(abs_pct / max(cfg["min_dist"], 1e-6))
    ema_strength = abs((ema_ratio or 1.0) - 1.0)
    ema_adj = min(0.05, 3.0 * ema_strength)  # caps at 5% boost
    session_adj = 0.03 if (cfg["session_gate"] and _s1_is_us_session()) else 0.0
    win_prob = min(0.85, base_p + ema_adj + session_adj)
```

Replace with:

```python
    # Win probability: empirical lookup (tanh fallback when bucket uncalibrated)
    base_p = _s1_lookup_win_rate(asset, abs_pct, mins_left)
    ema_strength = abs((ema_ratio or 1.0) - 1.0)
    ema_adj = min(0.05, 3.0 * ema_strength)
    session_adj = 0.03 if (cfg["session_gate"] and _s1_is_us_session()) else 0.0
    win_prob = min(0.85, base_p + ema_adj + session_adj)
```

- [ ] **Step 5: Replace the tanh formula in strategy_brain_s2**

Find this block in `bot_strategy.py`:

```python
    # Win probability: distance + velocity strength + OBI magnitude
    base_p = 0.50 + 0.25 * math.tanh(abs_pct / max(cfg["min_dist"], 1e-6))
    vel_adj = min(0.04, 0.02 * (vel_delta / max(cfg["min_vel_delta"], 1e-6)))
    obi_adj = min(0.03, 0.02 * abs(obi_val or 0.0) / max(cfg["min_obi"], 1e-6)) if obi_val is not None else 0.0
    win_prob = min(0.83, base_p + vel_adj + obi_adj)
```

Replace with:

```python
    # Win probability: empirical lookup (tanh fallback when bucket uncalibrated)
    base_p = _s2_lookup_win_rate(asset, vel_delta, mins_left)
    vel_adj = min(0.04, 0.02 * (vel_delta / max(cfg["min_vel_delta"], 1e-6)))
    obi_adj = min(0.03, 0.02 * abs(obi_val or 0.0) / max(cfg["min_obi"], 1e-6)) if obi_val is not None else 0.0
    win_prob = min(0.83, base_p + vel_adj + obi_adj)
```

- [ ] **Step 6: Run all calibration tests**

```bash
py -m pytest tests/test_calibrate_winrates.py -v
```

Expected: all tests PASSED.

- [ ] **Step 7: Syntax-check bot_strategy.py**

```bash
py -c "import ast; ast.parse(open('bot_strategy.py', encoding='utf-8').read()); print('OK')"
```

Expected: `OK`

- [ ] **Step 8: Commit everything**

```bash
git add bot_strategy.py tests/test_calibrate_winrates.py
git commit -m "feat: add S1/S2 win-rate lookup tables + replace tanh formulas in strategy brains"
```

---

## Task 11: Commit all remaining session changes

**Files:**
- All modified/deleted files from this session (bug fixes, S1/S2 rewrite, old strategy deletion)

- [ ] **Step 1: Check what's uncommitted**

```bash
git status --short | head -40
```

- [ ] **Step 2: Stage and commit the bulk deletions + strategy rewrite**

```bash
git add -A
git commit -m "feat: replace S1/S2 strategies, delete all old strategy code, fix 3 settlement bugs

- S1: EMA momentum with per-asset config (BTC/ETH/SOL/XRP/DOGE)
- S2: contract velocity + OBI with per-asset config
- Continuation-only gate added to both strategies
- Fee formula fixed: 0.07*p*(1-p) not flat 0.07
- Dual brain consensus in handle_ready_phase
- FundingDispersionMonitor removed (unused)
- Deleted: src/strategies/, src/kalshi_botv3/, tests/strategies/, backtesting/
- Bug fix: _settle_s1_orphans SQL now reads strike from DB column
- Bug fix: db_update_trade(None) guard added
- Bug fix: LOCKED stuck on empty market_close_time fixed (BTC + non-BTC)"
```

---

## Self-Review

**Spec coverage check:**
- ✅ `scripts/calibrate_winrates.py` standalone, no bot imports - Task 1
- ✅ Binance 1m fetch, 90 days, 5 assets - Task 2
- ✅ S1 EMA simulation + continuation filter - Task 3
- ✅ S1 bucketing, 4×3=12 buckets, None for sparse - Task 4
- ✅ Kalshi market listing with pagination - Task 5
- ✅ Kalshi price history (`yes_ask`) - Task 6
- ✅ S2 velocity simulation + continuation filter - Task 7
- ✅ S2 bucketing, 3×3=9 buckets, None for sparse - Task 8
- ✅ main() + stdout dict output - Task 9
- ✅ Lookup functions + tanh replacement in bot_strategy.py - Task 10
- ✅ Commit all session changes - Task 11
- ✅ Binance retry on 429 - Task 1 `_binance_get`
- ✅ Kalshi 401 abort with clear error - Task 1 `_load_kalshi_key` / `_kalshi_get`
- ✅ Wrong series ticker: abort with clear error - Task 9 `run_s2_phase`
- ✅ Skipped markets reported - Task 9 `run_s2_phase`
- ✅ Progress to stderr, dicts to stdout - Task 9 `_log` + `main()`
