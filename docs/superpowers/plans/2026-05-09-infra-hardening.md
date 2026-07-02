# Infrastructure Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix six infrastructure bugs - a crash in bot_market.py, silent DB failures, fragile date query, file handle leak, connection leak on startup, and silent stats data loss.

**Architecture:** Four independent targeted edits across four files. Each fix is 1-5 lines. Tests are source-inspection style (inspect.getsource) or lightweight functional checks - no live DB or API calls needed. All tests go in `tests/test_infra_hardening.py`.

**Tech Stack:** Python 3.11, pytest, sqlite3, aiosqlite, asyncio.

---

## File Map

| File | Fixes |
|------|-------|
| `bot_market.py` | Fix 1: extract dict comprehension from f-string (line 411) |
| `bot.py` | Fix 5: wrap startup DB connection in try/finally (lines 56-67) |
| `bot_infra.py` | Fix 2: add trade data to db_write_trade error log (line 343); Fix 3: LIKE->DATE() in db_get_today_pnl (line 447); Fix 4: close file handle with context manager (line 137); Fix 6b: add _VALID_TRADE_COLS guard in db_update_trade (before line 347) |
| `bot_stats.py` | Fix 6a: log warning for unknown strategy_variant in _run_queries (line 62) |
| `tests/test_infra_hardening.py` | New - 4 tests covering Fixes 1, 3, 6a, 6b |

---

### Task 1: Fix 1 + Fix 5 - bot_market.py crash + bot.py connection leak

**Files:**
- Create: `tests/test_infra_hardening.py`
- Modify: `bot_market.py:410-411`
- Modify: `bot.py:56-67`

**Context:**

Fix 1 - `bot_market.py:411`: The warning log uses a nested dict comprehension directly inside an f-string (`f"...{ {k: v for k in (...)} }"`). This is confusing and can cause issues in some Python versions. Extract to a variable.

Fix 5 - `bot.py:55-71`: The startup zombie-cleanup block opens `conn = sqlite3.connect(...)` at line 56 and calls `conn.close()` at line 67 inside the `try` block. If `conn.execute()` or `conn.commit()` raises, `.close()` is never reached - connection leaks.

- [ ] **Step 1: Create `tests/test_infra_hardening.py`**

```python
"""Tests for infrastructure hardening fixes."""
import os
import sys
import inspect

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot_market
import bot_infra
import bot_stats


def test_bot_market_fstring_no_nested_comprehension():
    """parse_strike warning log must use a variable, not nested dict comprehension."""
    src = inspect.getsource(bot_market.parse_strike)
    assert "{ {k:" not in src, \
        "Nested dict comprehension still in f-string - extract to _diag variable"
    assert "_diag" in src, \
        "parse_strike warning log must use _diag variable"
```

- [ ] **Step 2: Run test to verify it fails**

```
py -m pytest tests/test_infra_hardening.py::test_bot_market_fstring_no_nested_comprehension -v
```

Expected: FAIL (`{ {k:` still in source).

- [ ] **Step 3: Fix `bot_market.py:410-411`**

Find this block (lines 410-411):
```python
    else:
        log.warning(f"Cannot parse strike. Full market fields: { {k: market.get(k) for k in ('ticker','title','subtitle','floor_strike','cap_strike','strike_price','result','yes_sub_title','no_sub_title')} }")
```

Replace with:
```python
    else:
        _diag = {k: market.get(k) for k in ('ticker', 'title', 'subtitle', 'floor_strike', 'cap_strike', 'strike_price', 'result', 'yes_sub_title', 'no_sub_title')}
        log.warning(f"Cannot parse strike. Full market fields: {_diag}")
```

- [ ] **Step 4: Fix `bot.py:56-67` - add try/finally around DB connection**

Find this block (lines 55-71):
```python
    try:
        conn = sqlite3.connect(bot_state._DB_FILE)
        cleaned = conn.execute(
            """UPDATE trades
               SET outcome         = 'expired_untracked',
                   exit_price_cents = 0,
                   pnl_dollars      = -(COALESCE(entry_price_cents,0) * COALESCE(contracts,1) / 100.0),
                   fill_confirmed   = 0
               WHERE outcome IN ('pending', '', NULL)
                 AND ts < datetime('now', '-30 minutes')"""
        ).rowcount
        conn.commit()
        conn.close()
        if cleaned:
            log.warning(f"Startup cleanup: marked {cleaned} zombie pending trade(s) as expired_untracked")
    except Exception as _e:
        log.warning(f"Startup zombie-trade cleanup failed (non-fatal): {_e}")
```

Replace with:
```python
    try:
        conn = sqlite3.connect(bot_state._DB_FILE)
        try:
            cleaned = conn.execute(
                """UPDATE trades
                   SET outcome         = 'expired_untracked',
                       exit_price_cents = 0,
                       pnl_dollars      = -(COALESCE(entry_price_cents,0) * COALESCE(contracts,1) / 100.0),
                       fill_confirmed   = 0
                   WHERE outcome IN ('pending', '', NULL)
                     AND ts < datetime('now', '-30 minutes')"""
            ).rowcount
            conn.commit()
        finally:
            conn.close()
        if cleaned:
            log.warning(f"Startup cleanup: marked {cleaned} zombie pending trade(s) as expired_untracked")
    except Exception as _e:
        log.warning(f"Startup zombie-trade cleanup failed (non-fatal): {_e}")
```

- [ ] **Step 5: Run test to verify it passes**

```
py -m pytest tests/test_infra_hardening.py::test_bot_market_fstring_no_nested_comprehension -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/test_infra_hardening.py bot_market.py bot.py
git commit -m "fix: extract f-string dict comprehension in bot_market; add finally to startup DB conn"
```

---

### Task 2: bot_infra.py - Fixes 2, 3, 4, 6b

**Files:**
- Modify: `bot_infra.py` - four separate changes
- Modify: `tests/test_infra_hardening.py` - append 2 tests

**Context:**

- **Fix 2 (line 343):** `db_write_trade` except block logs `"DB write_trade error: {exc}"` but omits the trade dict, making it hard to diagnose which trade was lost. Update the message to include trade data.

- **Fix 3 (line 447):** `db_get_today_pnl` uses `ts LIKE f"{today}%"` - fragile if ts format includes timezone offset. Change to `DATE(ts) = ?`.

- **Fix 4 (line 137):** `open(_be_state)` in `_init_config` is not closed with a context manager. Change to `with open(_be_state) as _f:`.

- **Fix 6b (before line 347):** `db_update_trade` builds `UPDATE trades SET {set_clause}` with column names from caller dict keys - no validation. Add `_VALID_TRADE_COLS` frozenset at module level and guard at top of function.

- [ ] **Step 1: Append 2 tests to `tests/test_infra_hardening.py`**

```python
def test_db_get_today_pnl_uses_date_function():
    """db_get_today_pnl must use DATE(ts) = ? not ts LIKE for date filtering."""
    src = inspect.getsource(bot_infra.db_get_today_pnl)
    assert "DATE(ts)" in src, "db_get_today_pnl must use DATE(ts) = ?"
    assert "ts LIKE" not in src, "db_get_today_pnl must not use ts LIKE"


def test_db_update_trade_has_column_whitelist():
    """db_update_trade must validate column names against _VALID_TRADE_COLS."""
    src = inspect.getsource(bot_infra.db_update_trade)
    assert "_VALID_TRADE_COLS" in src, "db_update_trade missing _VALID_TRADE_COLS guard"
    assert hasattr(bot_infra, "_VALID_TRADE_COLS"), "_VALID_TRADE_COLS must be module-level"
    assert isinstance(bot_infra._VALID_TRADE_COLS, frozenset)
    assert "outcome" in bot_infra._VALID_TRADE_COLS
    assert "brain" in bot_infra._VALID_TRADE_COLS
    assert "exit_price_cents" in bot_infra._VALID_TRADE_COLS
```

- [ ] **Step 2: Run to verify both fail**

```
py -m pytest tests/test_infra_hardening.py::test_db_get_today_pnl_uses_date_function tests/test_infra_hardening.py::test_db_update_trade_has_column_whitelist -v
```

Expected: both FAIL.

- [ ] **Step 3: Fix 4 - close file handle in `bot_infra.py:137`**

Find (line 137):
```python
            cfg["bot_enabled"] = open(_be_state).read().strip() == "1"
```

Replace with:
```python
            with open(_be_state) as _f:
                cfg["bot_enabled"] = _f.read().strip() == "1"
```

- [ ] **Step 4: Fix 2 - improve db_write_trade error log in `bot_infra.py:342-344`**

Find (lines 342-344):
```python
    except Exception as exc:
        log.error(f"DB write_trade error: {exc}")
        return None
```

Replace with:
```python
    except Exception as exc:
        log.error("db_write_trade FAILED - trade NOT recorded: %s | trade=%s", exc, trade)
        return None
```

- [ ] **Step 5: Fix 3 - change LIKE to DATE() in `bot_infra.py:446-448`**

Find (lines 446-448):
```python
            async with db.execute(
                "SELECT COALESCE(SUM(pnl_dollars), 0) FROM trades "
                "WHERE mode = ? AND ts LIKE ? AND outcome != 'pending'",
                (mode, f"{today}%"),
```

Replace with:
```python
            async with db.execute(
                "SELECT COALESCE(SUM(pnl_dollars), 0) FROM trades "
                "WHERE mode = ? AND DATE(ts) = ? AND outcome != 'pending'",
                (mode, today),
```

- [ ] **Step 6: Fix 6b - add `_VALID_TRADE_COLS` and guard in `bot_infra.py`**

Find this line (line 347):
```python
async def db_update_trade(trade_id: int, fields: dict) -> None:
```

Insert immediately before it:
```python
_VALID_TRADE_COLS = frozenset({
    "ts", "market_id", "market_title", "mode", "side", "contracts",
    "entry_price_cents", "trade_amount_dollars", "confidence_score",
    "model_prob", "implied_prob", "btc_price_at_entry", "strike",
    "seconds_left_at_entry", "fill_confirmed", "exit_price_cents",
    "exit_reason", "outcome", "pnl_dollars", "profit_percent",
    "order_id", "asset", "raw_p_yes", "entry_signals",
    "strategy_variant", "brain", "signal_name", "strategy_version",
})


```

Then find inside `db_update_trade` (line 350-351, after the `trade_id is None` check):
```python
    if trade_id is None:
        log.error("db_update_trade called with trade_id=None - trade will stay pending in DB")
        return
    try:
```

Replace with:
```python
    if trade_id is None:
        log.error("db_update_trade called with trade_id=None - trade will stay pending in DB")
        return
    bad_cols = set(fields) - _VALID_TRADE_COLS
    if bad_cols:
        log.error("db_update_trade: unknown column(s) %s - skipping update for trade %s", bad_cols, trade_id)
        return
    try:
```

- [ ] **Step 7: Run tests to verify both pass**

```
py -m pytest tests/test_infra_hardening.py::test_db_get_today_pnl_uses_date_function tests/test_infra_hardening.py::test_db_update_trade_has_column_whitelist -v
```

Expected: both PASS.

- [ ] **Step 8: Run all infra hardening tests**

```
py -m pytest tests/test_infra_hardening.py -v
```

Expected: all 3 tests pass.

- [ ] **Step 9: Commit**

```bash
git add bot_infra.py tests/test_infra_hardening.py
git commit -m "fix: bot_infra - db_write_trade log, DATE() query, file handle, column whitelist"
```

---

### Task 3: bot_stats.py - Fix 6a + full suite

**Files:**
- Modify: `bot_stats.py:62-73`
- Modify: `tests/test_infra_hardening.py` - append 1 test

**Context:** `_run_queries` builds `by_sa` from all DB rows without checking if `strategy_variant` is in `_STRATEGY_LABELS`. Unknown variants silently accumulate in `by_sa` and are never displayed in stats. Fix: add `log.warning` when an unknown variant is encountered.

The loop is at line 62:
```python
    for row in rows:
        key = (row["strategy_variant"], row["asset"])
        if key not in by_sa:
            by_sa[key] = {"wins": 0, "losses": 0, "pnl": 0.0}
```

- [ ] **Step 1: Append test to `tests/test_infra_hardening.py`**

```python
def test_bot_stats_unknown_variant_warned():
    """_run_queries must check strategy_variant against _STRATEGY_LABELS and log warning."""
    src = inspect.getsource(bot_stats._run_queries)
    assert "_STRATEGY_LABELS" in src, \
        "_run_queries must check row strategy_variant against _STRATEGY_LABELS"
    assert "log.warning" in src, \
        "_run_queries must log warning for unknown strategy_variant"
```

- [ ] **Step 2: Run to verify it fails**

```
py -m pytest tests/test_infra_hardening.py::test_bot_stats_unknown_variant_warned -v
```

Expected: FAIL.

- [ ] **Step 3: Fix `bot_stats.py:62-73`**

Find this block (lines 62-73):
```python
    for row in rows:
        key = (row["strategy_variant"], row["asset"])
        if key not in by_sa:
            by_sa[key] = {"wins": 0, "losses": 0, "pnl": 0.0}
        if row["outcome"] == "win":
            by_sa[key]["wins"] += row["n"]
            today_wins += row["n"]
        else:
            by_sa[key]["losses"] += row["n"]
            today_losses += row["n"]
        by_sa[key]["pnl"] += row["pnl"] or 0.0
        today_pnl += row["pnl"] or 0.0
```

Replace with:
```python
    for row in rows:
        sv = row["strategy_variant"]
        if sv not in _STRATEGY_LABELS:
            log.warning("Unknown strategy_variant in DB: %r - row excluded from stats display", sv)
        key = (sv, row["asset"])
        if key not in by_sa:
            by_sa[key] = {"wins": 0, "losses": 0, "pnl": 0.0}
        if row["outcome"] == "win":
            by_sa[key]["wins"] += row["n"]
            today_wins += row["n"]
        else:
            by_sa[key]["losses"] += row["n"]
            today_losses += row["n"]
        by_sa[key]["pnl"] += row["pnl"] or 0.0
        today_pnl += row["pnl"] or 0.0
```

- [ ] **Step 4: Run test to verify it passes**

```
py -m pytest tests/test_infra_hardening.py::test_bot_stats_unknown_variant_warned -v
```

Expected: PASS.

- [ ] **Step 5: Run full test suite**

```
py -m pytest tests/test_infra_hardening.py tests/test_risk_guards.py tests/test_dual_brain.py -v
```

Expected: all tests pass (4 + 8 + 14 = 26 total), 0 failures.

- [ ] **Step 6: Commit**

```bash
git add bot_stats.py tests/test_infra_hardening.py
git commit -m "fix: warn on unknown strategy_variant in bot_stats._run_queries"
```
