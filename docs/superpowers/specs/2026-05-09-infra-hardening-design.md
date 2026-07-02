# Infrastructure Hardening Design - 2026-05-09

## Summary

Six targeted bug fixes across `bot_market.py`, `bot_infra.py`, `bot.py`, and `bot_stats.py`. All small (1-5 lines each). One is a crash bug already affecting production.

---

## Fix 1 - `bot_market.py:411` SyntaxError in f-string

**Problem:** Nested dict comprehension inside f-string braces causes a `SyntaxError` at runtime when the warning branch is reached (any market with unparseable strike).

**Current:**
```python
log.warning(f"Cannot parse strike. Full market fields: { {k: market.get(k) for k in ('ticker','title',...)} }")
```

**Fix:** Extract comprehension to a variable:
```python
_diag = {k: market.get(k) for k in ('ticker','title','subtitle','floor_strike','cap_strike','strike_price','result','yes_sub_title','no_sub_title')}
log.warning(f"Cannot parse strike. Full market fields: {_diag}")
```

---

## Fix 2 - `bot_infra.py` `db_write_trade` silent failure

**Problem:** When the INSERT fails, `db_write_trade` returns `None` with no error log. The calling bot code continues as if the trade was saved. Trade is lost from the database.

**Fix:** Add explicit error log in the except block:
```python
except Exception as exc:
    log.error("db_write_trade FAILED - trade NOT recorded in DB: %s | trade=%s", exc, trade)
    return None
```

The existing `None` return is kept so callers can still check; the log makes the failure observable.

---

## Fix 3 - `bot_infra.py:447` fragile date filter (`LIKE` → `DATE()`)

**Problem:** `WHERE ts LIKE f"{today}%"` is fragile. Breaks if the `ts` column ever contains timezone offsets or non-standard separators. `DATE(ts)` in SQLite correctly extracts the date part from any ISO-8601 string.

**Current:**
```python
"WHERE mode = ? AND ts LIKE ? AND outcome != 'pending'",
(mode, f"{today}%"),
```

**Fix:**
```python
"WHERE mode = ? AND DATE(ts) = ? AND outcome != 'pending'",
(mode, today),
```

---

## Fix 4 - `bot_infra.py:83` unclosed file handle

**Problem:** `open(_be_state).read().strip()` opens a file handle without closing it. Safe in CPython due to reference counting, but a real leak in other implementations.

**Fix:**
```python
with open(_be_state) as _f:
    cfg["bot_enabled"] = _f.read().strip() == "1"
```

---

## Fix 5 - `bot.py` startup DB connection not in `finally`

**Problem:** Startup zombie-cleanup block opens a SQLite connection, executes an UPDATE, then calls `.close()`. If the UPDATE raises, `.close()` is never reached - connection leaks.

**Fix:** Wrap in try/finally:
```python
conn = sqlite3.connect(db_path)
try:
    conn.execute("UPDATE trades SET outcome = 'abandoned' ...")
    conn.commit()
finally:
    conn.close()
```

---

## Fix 6 - `bot_stats.py` unknown strategy variant silenced + `db_update_trade` column whitelist

### 6a - `bot_stats.py` unknown strategy variant

**Problem:** If DB contains a `strategy_variant` not in `_STRATEGY_LABELS`, the row is silently skipped. Stats appear as 0 for that strategy. No operator warning.

**Fix:** Add log warning when skipping:
```python
if sv not in _STRATEGY_LABELS:
    log.warning("Unknown strategy_variant in DB: %r - row excluded from stats", sv)
    continue
```

### 6b - `db_update_trade` column whitelist

**Problem:** `db_update_trade` builds `UPDATE trades SET {set_clause}` where column names come directly from caller-supplied dict keys with no validation. A bad key corrupts the SQL statement.

**Valid columns for the `trades` table** (from `init_db` CREATE + ALTER statements):
```python
_VALID_TRADE_COLS = frozenset({
    "ts", "market_id", "mode", "side", "entry_price_cents", "contracts",
    "strike", "exit_price_cents", "exit_reason", "outcome", "pnl_dollars",
    "order_id", "asset", "raw_p_yes", "entry_signals", "strategy_variant",
    "signal_name", "strategy_version", "brain",
})
```

**Fix:** Add guard at top of `db_update_trade`:
```python
bad_cols = set(fields) - _VALID_TRADE_COLS
if bad_cols:
    log.error("db_update_trade: unknown column(s) %s - skipping update", bad_cols)
    return
```

---

## Files Changed

| File | Fix |
|------|-----|
| `bot_market.py` | Fix 1: extract dict comprehension from f-string |
| `bot_infra.py` | Fix 2: error log on db_write_trade failure; Fix 3: LIKE→DATE(); Fix 4: close file handle; Fix 6b: column whitelist |
| `bot.py` | Fix 5: finally block on startup DB connection |
| `bot_stats.py` | Fix 6a: log unknown strategy_variant |

---

## Tests

New file: `tests/test_infra_hardening.py`

1. `test_db_write_trade_failure_logs_error` - mock aiosqlite to throw on INSERT; verify `log.error` called with "NOT recorded"
2. `test_db_update_trade_rejects_unknown_column` - call `db_update_trade(1, {"bad_col": 99})`; verify it returns early without executing SQL
3. `test_db_get_today_pnl_uses_date_function` - inspect source of `db_get_today_pnl`; assert `DATE(ts)` in source and `LIKE` not in source
4. `test_bot_market_strike_warning_no_syntax_error` - import `bot_market`; verify no SyntaxError on import (the f-string fix makes the module importable)

Note: Fixes 4 (file handle) and 5 (finally block) are structural - verified by code review rather than runtime tests. Fix 6a (unknown variant log) verified by source inspection.
