# Dual-Strategy A/B Comparison — Design Spec

**Date:** 2026-05-04
**Status:** Approved
**Goal:** Run the original per-asset strategies (B3/E1/S1/X3/D3, commit ce9ec3f) alongside the current D3 hybrid simultaneously in one Railway process, with separate P&L tracking, trade logs, Telegram labels, and dashboard panels.

---

## Problem

The current bot runs only the D3 hybrid (Strategy 2). The user wants to A/B compare it against the original per-asset microstructure strategies (Strategy 1) that were built in commit ce9ec3f and later replaced. Both should trade simultaneously on identical market data, with zero interference between them, and their results must be cleanly separated everywhere — database, API, Telegram, dashboard.

---

## What the Two Strategies Are

### Strategy 1 — Original (ce9ec3f)
Five separate asset-specific strategies, each with its own bespoke signal:

| Asset | Label | Signal |
|-------|-------|--------|
| BTC   | B3    | Time-of-day-conditioned order-book imbalance; skips 18–21 UTC trough + funding-reset windows |
| ETH   | E1    | Vol-gated ETH/BTC ratio mean-revert at ±1.2σ |
| SOL   | S1    | Cross-venue funding-rate dispersion (Binance vs Hyperliquid) |
| XRP   | X3    | APAC decoupling + event continuation |
| DOGE  | D3    | Retail weekend-FOMO regime (looser EV + half stake on Fri–Sun) |

Each degrades gracefully when its input data is missing (returns no-signal, not an error).

### Strategy 2 — D3 Hybrid (current)
Unified 5-voter mean-reversion ensemble for all assets:
- V1 (BTC only): Black-Scholes p_yes
- V2: MTF momentum, inverted
- V3: RSI deviation, inverted
- V4 (non-BTC): Bollinger z-score, inverted
- V5: MTF magnitude soft confirmation, inverted
- Requires 3-of-5 vote; gated by EV, vol ratio, entry range.

---

## Architecture

### Single-process, dual-brain

One Railway service. One bot loop. Every market tick both strategy brains evaluate the same snapshot independently. Each brain has its own singleton cache. Both write to the same `trades` table, distinguished by a `strategy_variant` column.

```
market tick
  ├── strategy_brain_s1(asset, ...) → original per-asset strategy → Decision
  │     └── if trade → db_write_trade(..., strategy_variant='strategy1')
  │                  → send_telegram("[S1 Original] ...")
  └── strategy_brain_s2(asset, ...) → D3 hybrid → Decision
        └── if trade → db_write_trade(..., strategy_variant='strategy2')
                     → send_telegram("[S2 D3 Hybrid] ...")
```

Neither brain blocks the other. If S1 trades and S2 skips, only one trade is logged. If both trade on the same market, two independent trade rows are written with different `strategy_variant` values.

---

## Part 1 — File Recovery

### Folder structure
Recovered files live under `src/strategies/original/` — never touching `src/strategies/` current files.

```
src/strategies/original/
  __init__.py
  btc_strategy.py          (B3)
  eth_strategy.py          (E1)
  sol_strategy.py          (S1)
  xrp_strategy.py          (X3)
  doge_strategy.py         (D3-DOGE)
  signals/
    __init__.py
    btc_diurnal_obi.py
    funding_dispersion.py
    session_awareness.py
    session_clock.py
```

### Recovery method
Each file is recovered with:
```bash
git show ce9ec3f -- src/strategies/<file>.py
```
written directly to `src/strategies/original/<file>.py`.

### Import path fix
All `from strategies.signals.<name>` imports inside recovered files are rewritten to `from strategies.original.signals.<name>` so they resolve correctly in the new location. No other logic is changed — the files are otherwise identical to ce9ec3f.

### Test files
The five original test files (`tests/strategies/test_btc_strategy.py` etc.) are also recovered from ce9ec3f and updated to import from `strategies.original.*`. They must pass before any further work proceeds.

---

## Part 2 — Database

### New column
```sql
ALTER TABLE trades ADD COLUMN strategy_variant TEXT DEFAULT 'strategy2';
```

Values:
- `'strategy1'` — original B3/E1/S1/X3/D3
- `'strategy2'` — current D3 hybrid

### Migration
`init_db()` in `bot.py` runs the ALTER at startup inside a try/except — SQLite raises `OperationalError: duplicate column name` if it already exists, which is caught and ignored. Existing rows keep `DEFAULT 'strategy2'` (correct — all historical trades are from the D3 hybrid era).

### db_write_trade
The `db_write_trade()` function gains `strategy_variant` in both the INSERT column list and the VALUES tuple. The `CREATE TABLE IF NOT EXISTS` schema also gains the column so fresh deployments don't need the migration path.

---

## Part 3 — Bot (bot.py)

### Strategy caches
```python
_S2_SINGLETONS: dict = {}   # current D3 hybrid (was _STRATEGY_SINGLETONS)
_S1_SINGLETONS: dict = {}   # original per-asset strategies
```

`_STRATEGY_SINGLETONS` is renamed to `_S2_SINGLETONS`. All existing references updated.

### New functions

**`_get_or_make_strategy_s1(asset, config)`**
Returns the correct original strategy instance for the asset:
- BTC  → `BTCStrategy` from `strategies.original.btc_strategy`
- ETH  → `ETHStrategy` from `strategies.original.eth_strategy`
- SOL  → `SOLStrategy` from `strategies.original.sol_strategy`
- XRP  → `XRPStrategy` from `strategies.original.xrp_strategy`
- DOGE → `DOGEStrategy` from `strategies.original.doge_strategy`

Uses `_S1_SINGLETONS` as cache. Config params (min_ev_base, stake, skip_config) are read from `config.json` identically to S2.

**`strategy_brain_s1(btc_price, strike, yes_ask, no_ask, elapsed_seconds, secs_left, ticker, ..., asset)`**
Mirrors the existing `strategy_brain()` function signature exactly. Calls `_get_or_make_strategy_s1()`, invokes `.decide(features)`, returns a brain dict tagged `strategy_variant='strategy1'`.

**`strategy_brain_s2(...)` (renamed from `strategy_brain`)**
Identical to current `strategy_brain()` — just renamed. Returns brain dict tagged `strategy_variant='strategy2'`.

### Market evaluation loop
At each decision point (currently calls `strategy_brain(...)`), both brains are called sequentially:

```python
brain_s2 = strategy_brain_s2(btc_price, strike, yes_ask, no_ask, ...)
brain_s1 = strategy_brain_s1(btc_price, strike, yes_ask, no_ask, ...)
```

Each brain result is handled independently through the full existing trade-execution path (EV gate, position check, order placement, DB write). The only difference is `strategy_variant` is set from the brain dict before `db_write_trade` is called.

### Telegram notifications
Both the entry notification and the outcome notification prepend the strategy label:

```
[S1 Original] BTC YES — 34 contracts @ 62¢  (5m 12s left)
[S2 D3 Hybrid] BTC YES — 28 contracts @ 58¢  (5m 12s left)
```

Outcome notifications:
```
[S1 Original] ✅ BTC YES WON  +$8.40
[S2 D3 Hybrid] ❌ BTC YES LOST  -$6.20
```

The label is derived from `brain["strategy_variant"]` and formatted as:
- `strategy1` → `S1 Original`
- `strategy2` → `S2 D3 Hybrid`

---

## Part 4 — Server (server.py)

### `/api/trades`
Gains optional `?strategy=1` or `?strategy=2` query param. Maps to `strategy_variant='strategy1'` / `'strategy2'`. When omitted, returns all trades as before (no breaking change).

### `/api/pnl`
Gains optional `?strategy=1|2` param. When provided, filters all P&L calculations to that variant only. When omitted (default), adds a new top-level `by_strategy` key alongside existing keys:

```json
{
  "today": { ... },
  "alltime": { ... },
  "by_strategy": {
    "strategy1": {
      "today": { "pnl": -12.50, "trades": 8, "wins": 3, "win_rate": 37.5 },
      "alltime": { "pnl": -12.50, "trades": 8, "wins": 3, "win_rate": 37.5 }
    },
    "strategy2": {
      "today": { "pnl": 44.20, "trades": 11, "wins": 7, "win_rate": 63.6 },
      "alltime": { "pnl": 44.20, "trades": 11, "wins": 7, "win_rate": 63.6 }
    }
  }
}
```

Existing callers that don't pass `?strategy=` are unaffected — they see the same response shape they always did, plus the new `by_strategy` key.

---

## Part 5 — Dashboard (handoff/Money Printer.html)

### Renamed existing strip
The current P&L strip (NET P&L · ALL-TIME | WIN RATE · 30D | TODAY'S P&L) is relabelled:

> **Strategy 2 — D3 Hybrid**

It polls `/api/pnl?strategy=2` instead of the unfiltered `/api/pnl`.

### New strip added below
An identical three-panel strip is inserted directly below, labelled:

> **Strategy 1 — Original (B3/E1/S1/X3/D3)**

It polls `/api/pnl?strategy=1`. Same layout, same field mapping, same 60-second refresh cadence. Uses a distinct accent color (amber `--amber` token vs the current blue `--accent`) to visually distinguish it at a glance.

### Trades table
A `Strategy` column is added to the trades table rows, showing `S1` or `S2` as a pill badge so each trade is clearly attributed.

---

## Verification

After implementation, the following must all be confirmed before declaring done:

1. **Import check** — `python -c "from strategies.original.btc_strategy import BTCStrategy"` succeeds with no errors.
2. **Test suite** — all existing tests pass; all five recovered original strategy tests pass.
3. **Dual-brain init** — bot startup log shows both `S1 initialized: BTC` and `S2 initialized: BTC` (and all other assets).
4. **DB column** — `PRAGMA table_info(trades)` shows `strategy_variant` column present.
5. **Trade tagging** — after one paper-trade cycle, `SELECT strategy_variant, COUNT(*) FROM trades GROUP BY strategy_variant` returns rows for both variants.
6. **API** — `GET /api/pnl` response contains `by_strategy.strategy1` and `by_strategy.strategy2` keys.
7. **Telegram** — live log shows both `[S1 Original]` and `[S2 D3 Hybrid]` prefixes on notifications.
8. **Dashboard** — two separate P&L strips visible, each showing independent trade counts and P&L.

---

## Out of Scope

- Changing any logic inside the original strategies (recovered exactly as-is from ce9ec3f)
- Changing any logic inside the current D3 hybrid
- Backtesting either strategy
- Adding a kill switch for one strategy at runtime (can be done later via config)
- Recovering original test files for `test_ratio_z_score_stable_ratio_is_none` (pre-existing failure, unrelated)
