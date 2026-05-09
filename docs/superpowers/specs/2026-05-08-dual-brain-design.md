# Dual Brain + Strategy Fixes Design

**Goal:** Run S1 (EMA momentum) and S2 (velocity+OBI) as two fully isolated competing strategies in paper mode, each with its own position state, tracked P&L, and a nightly Telegram scorecard showing per-asset daily and all-time results.

**Architecture:** Two independent execution paths sharing no position state. Each brain writes its own position dict in `bot_state`, tags DB rows with `brain='s1'` or `brain='s2'`, and is never aware of the other brain's trades. Four S2 strategy bugs are fixed in the same change.

**Tech Stack:** Python asyncio, aiosqlite, Telegram Bot API, existing `bot_state` / `bot_loops` / `bot_risk` / `bot_infra` modules.

---

## 1. State Isolation (`bot_state.py`)

**Add:**
```python
_s1_attempted_tickers: set = set()   # replaces shared _order_attempted_tickers for S1
_s2_attempted_tickers: set = set()   # replaces shared _order_attempted_tickers for S2
_s2_positions: dict = {}             # ticker → {side, entry_cents, contracts, dollars, open_time, brain}
```

`_s1_pending_trades` already tracks open S1 trades — no new S1 state needed.

`_order_attempted_tickers` remains in `__all__` but both execution paths migrate to their own set. A ticker blocked for S1 does not block S2 and vice versa.

**Remove from `__all__`** (keep the variable for any legacy callers until confirmed dead):
No removals yet — add the two new sets and one new dict; removal is a follow-up.

---

## 2. Execution Path Isolation

### S2 "already in market" check (`bot_loops.py`)

Replace `_portfolio_has_position` API call with:
```python
if ticker in bot_state._s2_positions:
    continue  # S2 already holds this ticker
if ticker in bot_state._s2_attempted_tickers:
    continue  # S2 already attempted this cycle
```

### S2 position open (on fill, `bot_loops.py`)

```python
bot_state._s2_positions[ticker] = {
    "side": side,
    "entry_cents": fill_price,
    "contracts": contracts,
    "dollars": dollars_used,
    "open_time": time.time(),
    "brain": "s2",
}
bot_state._s2_attempted_tickers.add(ticker)
```

### S2 position close (on settlement, `bot_loops.py`)

S2 settlement happens in `monitor_market` when the market resolves (result != None). On resolution:
1. Pop `bot_state._s2_positions[ticker]`
2. Compute P&L: `(1.0 - entry_cents/100) * contracts * 100 - fee` for win, `-entry_cents/100 * contracts * 100` for loss
3. Write DB row (`db_update_trade`) with `brain='s2'`
4. Remove ticker from `_s2_attempted_tickers`

### S1 (`bot_risk.py` — `_execute_s1_trade` / `_settle_s1_trade`)

- Replace any use of `_order_attempted_tickers` with `_s1_attempted_tickers`
- On DB write in `_settle_s1_trade`: set `brain='s1'`

---

## 3. Strategy Bug Fixes (`bot_strategy.py`)

### Bug 1 — Win-prob inflation removed

`vel_adj` and `obi_adj` additive adjustments were inflating win probability beyond calibrated values. The 30-day calibration already priced velocity and OBI signals into `base_p`.

```python
# Before
win_prob = min(0.99, base_p + vel_adj + obi_adj)

# After
win_prob = min(0.99, base_p)
```

### Bug 2 — S2 fee hardcoded

```python
# Before
fee = 0.07 * _ep_s2 * (1.0 - _ep_s2)

# After
_fee_cents = config.get("kalshi_fee_per_contract_cents", 7)
fee = (_fee_cents / 100) * _ep_s2 * (1.0 - _ep_s2)
```

### Bug 3 — S2 max entry price reads global not per-asset

`get_asset_config` must be added to `bot_strategy.py` imports from `bot_infra`.

```python
# Before
_max_p = float(config.get("max_entry_price_cents", 76.0))

# After
_max_p = float(get_asset_config(config, asset, "max_entry_price_cents", 76.0))
```

---

## 4. DB Migration

Add `brain TEXT DEFAULT NULL` to the `trades` table. Applied once on startup via `init_db`.

```sql
ALTER TABLE trades ADD COLUMN brain TEXT DEFAULT NULL;
```

Existing rows remain `NULL`. New S1 rows get `'s1'`, new S2 rows get `'s2'`.

---

## 5. Daily Telegram Scorecard

Fires inside `midnight_reset` (`bot_risk.py`), which already runs at midnight and sends Telegram.

**Two queries:**

Daily (today's closed trades):
```sql
SELECT brain, asset,
       COUNT(*) AS trades,
       SUM(profit_loss) AS pnl,
       SUM(CASE WHEN profit_loss > 0 THEN 1 ELSE 0 END) AS wins
FROM trades
WHERE date(close_time) = date('now', 'localtime')
  AND brain IN ('s1', 's2')
  AND profit_loss IS NOT NULL
GROUP BY brain, asset
```

All-time (no date filter, same columns).

**Message format:**
```
📊 Daily Brain Scorecard

S1 (EMA momentum)
  BTC  +$2.10  3W/1L
  ETH  +$1.50  2W/0L
  SOL  —
  XRP  -$0.40  1W/2L
  DOGE —
  Total: +$3.20  6W/3L

S2 (velocity+OBI)
  BTC  -$0.50  2W/3L
  ETH  +$1.00  2W/1L
  SOL  +$0.80  1W/0L
  XRP  —
  DOGE -$0.40  0W/2L
  Total: +$0.90  5W/6L

All-time │ S1: +$31.50 52W/21L │ S2: +$8.40 38W/29L
Winner today: S1 🏆
```

Assets with no trades show `—`. If both brains have zero trades today, no scorecard is sent. If P&L is tied, no winner declared.

---

## 6. Files Changed

| File | Change |
|---|---|
| `bot_state.py` | Add `_s1_attempted_tickers`, `_s2_attempted_tickers`, `_s2_positions` to module body and `__all__` |
| `bot_strategy.py` | Fix 3 S2 bugs (win_prob inflation, hardcoded fee, per-asset price cap) |
| `bot_loops.py` | S2 position open/close/check wired to `_s2_positions` + `_s2_attempted_tickers` |
| `bot_risk.py` | S1 uses `_s1_attempted_tickers`; `_settle_s1_trade` writes `brain='s1'`; `midnight_reset` sends scorecard |
| `bot_infra.py` | `init_db` applies `ALTER TABLE trades ADD COLUMN brain TEXT DEFAULT NULL` migration; scorecard query helper |

---

## 7. Testing

- `test_dual_brain_isolation.py` — S1 and S2 can hold opposite sides on same ticker simultaneously
- `test_dual_brain_no_bleed.py` — S1 attempted set does not block S2 entry and vice versa
- `test_scorecard_query.py` — scorecard SQL returns correct per-brain per-asset rows
- `test_s2_fee_fix.py` — S2 fee reads from config not hardcoded
- `test_s2_winprob_no_inflation.py` — win_prob equals `base_p` with no additive adjustments
- `test_s2_price_cap_per_asset.py` — S2 reads per-asset max entry price
- `test_midnight_scorecard_telegram.py` — midnight reset sends scorecard when trades exist; skips when none
