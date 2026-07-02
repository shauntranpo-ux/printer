# Win-Rate Calibration Design - S1 + S2
Date: 2026-05-07

## Goal
Replace uncalibrated tanh win-probability formulas in `strategy_brain_s1` and `strategy_brain_s2`
with empirical win-rate lookup tables derived from historical data.

## Scope
- **S1**: Binance 1-minute close prices (90 days, 5 assets)
- **S2**: Kalshi historical market yes_ask series (60 days, 5 assets)
- **Output**: Two Python dicts printed to stdout → manual paste into `bot_strategy.py`
- **OBI**: Not calibrated (no historical OBI data available); OBI gate remains a live-only filter

---

## Script: `scripts/calibrate_winrates.py`

Standalone - no bot imports. Reads `KALSHI_API_KEY` env var for S2 phase.
Progress to stderr; dict output to stdout (clean for redirect/paste).

---

## S1 - Binance Phase

### Data fetch
- Endpoint: `GET https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=1000`
- Symbols: `BTCUSDT ETHUSDT SOLUSDT XRPUSDT DOGEUSDT`
- Range: last 90 days (~130 requests/asset, 650 total - within Binance rate limits)
- Store: list of `(timestamp_ms, close)`

### Simulation
- Slide 15-min windows aligned to `:00 :15 :30 :45` of each hour
- Strike = close at window open
- Outcome = `close_at_expiry > strike` → YES wins; `≤ strike` → NO wins
- Entry offsets: 3, 5, 7, 10, 12 min remaining
- EMA params: per-asset from `_S1_ASSET_CONFIG` (same as live strategy)
- **Continuation filter**: skip record if EMA direction disagrees with price side of strike
  - EMA bullish (YES) requires `current_price > strike`
  - EMA bearish (NO) requires `current_price < strike`
- Record: `(abs_pct, mins_left, outcome)`

Expected volume: ~8,640 windows/asset × 5 entry points ≈ **43,000 records/asset** pre-filter.

### Bucketing
Per asset, 4 × 3 = 12 buckets:

| dist idx | range |
|---|---|
| 0 | `[min_dist, 0.5%)` |
| 1 | `[0.5%, 1.0%)` |
| 2 | `[1.0%, 2.0%)` |
| 3 | `[2.0%+)` |

| time idx | range |
|---|---|
| 0 | `3-6 min` |
| 1 | `6-9 min` |
| 2 | `9-12 min` |

Min samples threshold: **50 per bucket**. Buckets below → `None` → bot falls back to tanh.

---

## S2 - Kalshi Phase

### Data fetch
- Series tickers: `KXBTCD KXETHD KXSOLD KXXRPD KXDOGED` *(verified on first run; abort with clear error if wrong)*
- List markets: `GET /markets?series_ticker={series}&limit=200` (paginated)
- Per market: `GET /markets/{ticker}/history` → yes_ask series
- Outcome: `GET /markets/{ticker}` → `result` field (`yes` / `no`)
- Range: last 60 days
- Auth: `KALSHI_API_KEY` env var

### Simulation
- Entry offsets: 2, 4, 6, 8, 10, 12 min remaining
- Velocity signal: first-half vs second-half of last `vel_lookback` yes_ask ticks (per-asset config)
- **Continuation filter**: skip record if velocity direction disagrees with price side of strike
- Record: `(vel_delta, mins_left, outcome)`

Expected volume: ~5,760 markets/asset × 6 entry points ≈ **34,000 records/asset** pre-filter.

### Bucketing
Per asset, 3 × 3 = 9 buckets:

| vel idx | range |
|---|---|
| 0 | `[min_vel_delta, 2× min_vel_delta)` |
| 1 | `[2×, 4× min_vel_delta)` |
| 2 | `[4×+)` |

| time idx | range |
|---|---|
| 0 | `2-5 min` |
| 1 | `5-8 min` |
| 2 | `8-13 min` |

Min samples threshold: **50 per bucket**. Buckets below → `None` → bot falls back to tanh.

---

## Output Format

```python
# ── PASTE INTO bot_strategy.py ──────────────────────────────────────
_S1_WIN_RATE = {
    "BTC": {(0,0): 0.631, (0,1): 0.604, (0,2): 0.588, (1,0): 0.658, ...},
    "ETH": {...},
    "SOL": {...},
    "XRP": {...},
    "DOGE": {...},
}
_S2_WIN_RATE = {
    "BTC": {(0,0): 0.587, ...},
    ...
}
```

---

## bot_strategy.py Changes

1. Add `_S1_WIN_RATE` and `_S2_WIN_RATE` dicts (populated by paste after calibration run)
2. Add `_s1_lookup_win_rate(asset, abs_pct, mins_left) -> float` - returns empirical rate or tanh fallback
3. Add `_s2_lookup_win_rate(asset, vel_delta, mins_left) -> float` - same pattern
4. Replace tanh formula lines in `strategy_brain_s1` and `strategy_brain_s2` with lookup calls
5. EMA-strength and session adjustments remain as additive boosts on top of base lookup rate

---

## Error Handling
- Binance: retry on 429 with backoff; skip asset if 3 consecutive failures
- Kalshi series ticker wrong: abort with `ERROR: no markets found for {series}` - do not silently produce empty table
- Bucket with <50 samples: output `None`, log count to stderr
- Market with missing result or history: skip silently, count and report total skipped at end
