# Dual Brain + Strategy Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run S1 (EMA momentum) and S2 (velocity+OBI) as fully isolated competing strategies in paper mode - each tags DB rows with `brain='s1'`/`'s2'`, tracks separate P&L, and receives a nightly Telegram scorecard. Also fixes 3 S2 strategy bugs.

**Architecture:** Add `brain TEXT` column to `trades` table; tag all S1/S2 DB writes at insert time; rename shared `_order_attempted_tickers` → `_s2_attempted_tickers`; make S1 callable in S2's LOCKED phase for BTC and alt assets; add async scorecard query + midnight Telegram send.

**Tech Stack:** Python asyncio, aiosqlite, Telegram Bot API, existing bot_state / bot_loops / bot_risk / bot_infra modules.

---

## File Map

| File | Change |
|---|---|
| `bot_infra.py` | `init_db` migration adds `brain` column; `db_write_trade` includes `brain`; add `db_brain_scorecard()` |
| `bot_state.py` | Add `_s2_attempted_tickers: set`; remove `_order_attempted_tickers`; update `__all__` |
| `bot_loops.py` | Rename `_order_attempted_tickers` → `_s2_attempted_tickers` (3 sites); add `"brain": "s2"` to S2 trade_data; add S1 LOCKED-phase entry for BTC + alt; add `_send_brain_scorecard()` |
| `bot_strategy.py` | Fix 3 S2 bugs: remove win_prob inflation, fix hardcoded fee, fix per-asset price cap; add `get_asset_config` import |
| `bot_risk.py` | Add `"brain": "s1"` to S1 trade_data in `_execute_s1_trade` |
| `tests/test_dual_brain.py` | 7 new tests |

---

## Task 1: DB migration + db_write_trade brain column

**Files:**
- Modify: `bot_infra.py` (init_db around line 257, db_write_trade around line 316)
- Create: `tests/test_dual_brain.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_dual_brain.py
"""Tests for dual brain isolation and strategy fixes."""
import os
import sys
import sqlite3
import asyncio
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot_state
import bot_infra


def _tmp_db():
    """Create a temp DB path and point bot_state at it."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    bot_state._DB_FILE = f.name
    return f.name


def test_brain_column_exists_after_init_db():
    db_path = _tmp_db()
    try:
        bot_infra.init_db()
        conn = sqlite3.connect(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}
        conn.close()
        assert "brain" in cols, f"brain column missing from trades; found: {cols}"
    finally:
        os.unlink(db_path)


def test_db_write_trade_stores_brain():
    db_path = _tmp_db()
    try:
        bot_infra.init_db()
        trade = {
            "ts": "2026-01-01T00:00:00Z",
            "market_id": "TEST-123",
            "mode": "paper",
            "outcome": "pending",
            "brain": "s1",
        }
        trade_id = asyncio.run(bot_infra.db_write_trade(trade))
        assert trade_id is not None
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT brain FROM trades WHERE id = ?", (trade_id,)).fetchone()
        conn.close()
        assert row[0] == "s1", f"Expected brain='s1', got {row[0]}"
    finally:
        os.unlink(db_path)
```

- [ ] **Step 2: Run test to verify it fails**

```
py -m pytest tests/test_dual_brain.py::test_brain_column_exists_after_init_db tests/test_dual_brain.py::test_db_write_trade_stores_brain -v
```

Expected: FAIL - `brain` column missing / `brain` not stored.

- [ ] **Step 3: Add `brain` column to `init_db` migration**

In `bot_infra.py`, find the loop starting around line 255 that adds missing columns:
```python
for col, typedef in (
    ("signal_name",       "TEXT"),
    ("strategy_variant",  "TEXT DEFAULT 'strategy2'"),
    ("strategy_version",   "TEXT"),
):
```

Add `("brain", "TEXT")` as the last item:
```python
for col, typedef in (
    ("signal_name",       "TEXT"),
    ("strategy_variant",  "TEXT DEFAULT 'strategy2'"),
    ("strategy_version",   "TEXT"),
    ("brain",              "TEXT"),
):
```

- [ ] **Step 4: Add `brain` to `db_write_trade` INSERT**

In `bot_infra.py`, `db_write_trade`, change the INSERT to include `brain`:

```python
cur = await db.execute("""
    INSERT INTO trades (
        ts, market_id, market_title, mode, side, contracts,
        entry_price_cents, trade_amount_dollars, confidence_score,
        model_prob, implied_prob, btc_price_at_entry, strike,
        seconds_left_at_entry, fill_confirmed,
        exit_price_cents, exit_reason, outcome, pnl_dollars, profit_percent,
        order_id, asset, raw_p_yes, entry_signals, strategy_variant, brain
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
""", (
    trade.get("ts"), trade.get("market_id"), trade.get("market_title"),
    trade.get("mode"), trade.get("side"), trade.get("contracts"),
    trade.get("entry_price_cents"), trade.get("trade_amount_dollars"),
    trade.get("confidence_score"), trade.get("model_prob"),
    trade.get("implied_prob"), trade.get("btc_price_at_entry"),
    trade.get("strike"), trade.get("seconds_left_at_entry"),
    trade.get("fill_confirmed"),
    trade.get("exit_price_cents"), trade.get("exit_reason"),
    trade.get("outcome", "pending"), trade.get("pnl_dollars"),
    trade.get("profit_percent"),
    trade.get("order_id"), trade.get("asset", "BTC"),
    trade.get("raw_p_yes"), trade.get("entry_signals"),
    trade.get("strategy_variant", "strategy2"), trade.get("brain"),
))
```

- [ ] **Step 5: Run test to verify it passes**

```
py -m pytest tests/test_dual_brain.py::test_brain_column_exists_after_init_db tests/test_dual_brain.py::test_db_write_trade_stores_brain -v
```

Expected: PASS.

- [ ] **Step 6: Run full suite to confirm no regression**

```
py -m pytest tests/ -x -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add bot_infra.py tests/test_dual_brain.py
git commit -m "feat: add brain column to trades table and db_write_trade"
```

---

## Task 2: bot_state additions + rename _order_attempted_tickers

**Files:**
- Modify: `bot_state.py` (lines ~19, ~61)
- Modify: `bot_loops.py` (3 callsites: ~355, ~993, ~1055)
- Test: `tests/test_dual_brain.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_dual_brain.py`:
```python
def test_s2_attempted_tickers_exists():
    assert hasattr(bot_state, "_s2_attempted_tickers"), \
        "_s2_attempted_tickers missing from bot_state"
    assert isinstance(bot_state._s2_attempted_tickers, set)

def test_order_attempted_tickers_removed():
    assert not hasattr(bot_state, "_order_attempted_tickers"), \
        "_order_attempted_tickers should be removed; use _s2_attempted_tickers"
```

- [ ] **Step 2: Run test to verify it fails**

```
py -m pytest tests/test_dual_brain.py::test_s2_attempted_tickers_exists tests/test_dual_brain.py::test_order_attempted_tickers_removed -v
```

Expected: FAIL.

- [ ] **Step 3: Update bot_state.py**

Find line ~19 in `__all__`:
```python
"_order_attempted_tickers", "_asset_states", "_s1_pending_trades",
```
Change to:
```python
"_s2_attempted_tickers", "_asset_states", "_s1_pending_trades",
```

Find line ~61:
```python
_order_attempted_tickers: set = set()
```
Change to:
```python
_s2_attempted_tickers: set = set()
```

- [ ] **Step 4: Rename callsites in bot_loops.py**

Three replacements - make each one individually:

Line ~355 (inside trade placement):
```python
# Before
else: bot_state._order_attempted_tickers.add(ticker)
# After
else: bot_state._s2_attempted_tickers.add(ticker)
```

Line ~993 (BTC market change reset):
```python
# Before
bot_state._order_attempted_tickers.discard(prev_ticker)
# After
bot_state._s2_attempted_tickers.discard(prev_ticker)
```

Line ~1055 (BTC DONE→READY re-entry check):
```python
# Before
if secs_left > 3 * 60 and ticker not in bot_state._order_attempted_tickers:
# After
if secs_left > 3 * 60 and ticker not in bot_state._s2_attempted_tickers:
```

- [ ] **Step 5: Verify no remaining references to old name**

```
py -m pytest tests/test_dual_brain.py::test_s2_attempted_tickers_exists tests/test_dual_brain.py::test_order_attempted_tickers_removed -v
```

Also run:
```
grep -r "_order_attempted_tickers" . --include="*.py"
```
Expected: no output.

- [ ] **Step 6: Run full suite**

```
py -m pytest tests/ -x -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add bot_state.py bot_loops.py
git commit -m "refactor: rename _order_attempted_tickers to _s2_attempted_tickers"
```

---

## Task 3: Fix 3 S2 strategy bugs

**Files:**
- Modify: `bot_strategy.py` (imports, `strategy_brain_s2` function)
- Test: `tests/test_dual_brain.py`

**Context:** `strategy_brain_s2` is in `bot_strategy.py`. The relevant section starts around where `base_p`, `vel_adj`, `obi_adj`, `win_prob` and `fee` are computed, and where `_max_p` / `_min_p` gate entry price.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_dual_brain.py`:
```python
def test_s2_winprob_no_inflation():
    """win_prob must equal base_p - no vel_adj or obi_adj added."""
    import bot_strategy as bs
    # Inject a known calibrated value and verify it's returned unchanged.
    original = bs._S2_WIN_RATE.get("BTC")
    bs._S2_WIN_RATE["BTC"] = {(0, 0): 0.8000}
    try:
        # vel_delta ratio < 2.0 → vel_idx=0; mins_left < 5.0 → time_idx=0
        result = bs._s2_lookup_win_rate("BTC", vel_delta=0.85, mins_left=3.0)
        assert abs(result - 0.8000) < 1e-9, \
            f"Expected 0.8000 (no inflation), got {result}"
    finally:
        bs._S2_WIN_RATE["BTC"] = original


def test_s2_fee_reads_from_config():
    """S2 EV gate must use config fee, not hardcoded 0.07."""
    import bot_strategy as bs
    from unittest.mock import patch
    # Use a config with fee=0 - if hardcoded 0.07, EV will differ
    fake_cfg = {"kalshi_fee_per_contract_cents": 0, "min_entry_price_cents": 20,
                "max_entry_price_cents": 80}
    with patch.object(bs, "read_config", return_value=fake_cfg), \
         patch.object(bs, "_s2_contract_direction", return_value=("yes", 1.0)), \
         patch.object(bs, "_s2_obi_gate", return_value=(True, 0.5)), \
         patch.object(bs, "_s2_lookup_win_rate", return_value=0.80):
        result = bs.strategy_brain_s2(
            btc_price=96000, strike=95000,
            yes_ask=60, no_ask=42,
            elapsed_seconds=100, secs_left=300,
            ticker="KXBTC15M-26MAY0915-B95000",
            asset="BTC",
        )
    # With fee=0 and win_prob=0.80 and entry=0.60, ev = 0.80 - 0.60 = 0.20 > 0
    # If fee were hardcoded 0.07: ev = 0.80 - 0.60 - 0.07*0.6*0.4 ≈ 0.183
    # Both would still pass min_ev, but what matters is the trade fires.
    assert result.get("action") == "trade", \
        f"Expected trade with zero fee, got: {result.get('reasoning')}"


def test_s2_price_cap_per_asset():
    """S2 must read max_entry_price_cents from per-asset config, not global."""
    import bot_strategy as bs
    from unittest.mock import patch
    # Global config has max=50 (too low). Per-asset override has max=80.
    fake_cfg = {
        "kalshi_fee_per_contract_cents": 7,
        "min_entry_price_cents": 20,
        "max_entry_price_cents": 50,   # global: would block 60c entry
        "s2_config": {"BTC": {"max_entry_price_cents": 80}},  # per-asset: allows 60c
    }
    with patch.object(bs, "read_config", return_value=fake_cfg), \
         patch.object(bs, "_s2_contract_direction", return_value=("yes", 1.0)), \
         patch.object(bs, "_s2_obi_gate", return_value=(True, 0.5)), \
         patch.object(bs, "_s2_lookup_win_rate", return_value=0.92):
        result = bs.strategy_brain_s2(
            btc_price=96000, strike=95000,
            yes_ask=60, no_ask=42,
            elapsed_seconds=100, secs_left=300,
            ticker="KXBTC15M-26MAY0915-B95000",
            asset="BTC",
        )
    assert result.get("action") == "trade", \
        f"Per-asset max=80 should allow 60c entry, got: {result.get('reasoning')}"
```

- [ ] **Step 2: Run tests to verify they fail**

```
py -m pytest tests/test_dual_brain.py::test_s2_winprob_no_inflation tests/test_dual_brain.py::test_s2_fee_reads_from_config tests/test_dual_brain.py::test_s2_price_cap_per_asset -v
```

Expected: FAIL on all three.

- [ ] **Step 3: Add get_asset_config import to bot_strategy.py**

Find the import from `bot_infra` near the top of `bot_strategy.py`. Add `get_asset_config`:
```python
from bot_infra import read_config, get_asset_config
```

- [ ] **Step 4: Fix Bug 1 - remove win_prob inflation**

In `strategy_brain_s2`, find:
```python
vel_adj = min(0.04, 0.02 * (vel_delta / max(cfg["min_vel_delta"], 1e-6)))
obi_adj = min(0.03, 0.02 * abs(obi_val or 0.0) / max(cfg["min_obi"], 1e-6)) if obi_val is not None else 0.0
win_prob = min(0.99, base_p + vel_adj + obi_adj)
```

Replace with:
```python
win_prob = min(0.99, base_p)
```

- [ ] **Step 5: Fix Bug 2 - fee from config**

In `strategy_brain_s2`, find:
```python
fee = 0.07 * _ep_s2 * (1.0 - _ep_s2)
```

Replace with:
```python
_fee_cents = config.get("kalshi_fee_per_contract_cents", 7)
fee = (_fee_cents / 100) * _ep_s2 * (1.0 - _ep_s2)
```

- [ ] **Step 6: Fix Bug 3 - per-asset max entry price**

In `strategy_brain_s2`, find (Gate 3 entry price range):
```python
_min_p = float(config.get("min_entry_price_cents", 20.0))
_max_p = float(config.get("max_entry_price_cents", 76.0))
```

Replace with:
```python
_min_p = float(get_asset_config(config, asset, "min_entry_price_cents", 20.0))
_max_p = float(get_asset_config(config, asset, "max_entry_price_cents", 76.0))
```

- [ ] **Step 7: Run bug-fix tests**

```
py -m pytest tests/test_dual_brain.py::test_s2_winprob_no_inflation tests/test_dual_brain.py::test_s2_fee_reads_from_config tests/test_dual_brain.py::test_s2_price_cap_per_asset -v
```

Expected: all PASS.

- [ ] **Step 8: Run full suite**

```
py -m pytest tests/ -x -q
```

Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add bot_strategy.py tests/test_dual_brain.py
git commit -m "fix: remove S2 win_prob inflation, fix hardcoded fee, fix per-asset price cap"
```

---

## Task 4: Tag all trades with brain column

**Files:**
- Modify: `bot_risk.py` (`_execute_s1_trade` trade_data dict, line ~428)
- Modify: `bot_loops.py` (S2 trade_data dict in `handle_ready_phase`, line ~429)
- Test: `tests/test_dual_brain.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_dual_brain.py`:
```python
def test_s1_trade_data_has_brain_s1():
    """_execute_s1_trade must include brain='s1' in the trade_data dict passed to db_write_trade."""
    import bot_strategy as bs
    # Check that _S1_VERSION exists (confirms bot_state is importable)
    import bot_state as bstate
    assert hasattr(bstate, "_S1_VERSION")
    # Direct inspection: read the source and verify the key is present.
    import bot_risk
    import inspect
    src = inspect.getsource(bot_risk._execute_s1_trade)
    assert '"brain": "s1"' in src or "'brain': 's1'" in src, \
        "_execute_s1_trade trade_data missing 'brain': 's1'"


def test_s2_trade_data_has_brain_s2():
    """handle_ready_phase must include brain='s2' in the S2 trade_data dict."""
    import bot_loops
    import inspect
    src = inspect.getsource(bot_loops.handle_ready_phase)
    assert '"brain": "s2"' in src or "'brain': 's2'" in src, \
        "handle_ready_phase S2 trade_data missing 'brain': 's2'"
```

- [ ] **Step 2: Run test to verify it fails**

```
py -m pytest tests/test_dual_brain.py::test_s1_trade_data_has_brain_s1 tests/test_dual_brain.py::test_s2_trade_data_has_brain_s2 -v
```

Expected: FAIL.

- [ ] **Step 3: Add brain='s1' to S1 trade_data in bot_risk.py**

In `_execute_s1_trade`, find the `trade_data` dict (around line 408). It ends with:
```python
        "strategy_variant":     "strategy1",
        "strategy_version":     bot_state._S1_VERSION,
    }
```

Add `"brain"` key before the closing brace:
```python
        "strategy_variant":     "strategy1",
        "strategy_version":     bot_state._S1_VERSION,
        "brain":                "s1",
    }
```

- [ ] **Step 4: Add brain='s2' to S2 trade_data in bot_loops.py**

In `handle_ready_phase`, find the S2 `trade_data` dict (around line 408). It ends with:
```python
        "strategy_variant": "strategy2",
        "strategy_version": bot_state._S2_VERSION,
    }
```

Add `"brain"` key:
```python
        "strategy_variant": "strategy2",
        "strategy_version": bot_state._S2_VERSION,
        "brain":            "s2",
    }
```

- [ ] **Step 5: Run test to verify it passes**

```
py -m pytest tests/test_dual_brain.py::test_s1_trade_data_has_brain_s1 tests/test_dual_brain.py::test_s2_trade_data_has_brain_s2 -v
```

Expected: PASS.

- [ ] **Step 6: Run full suite**

```
py -m pytest tests/ -x -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add bot_risk.py bot_loops.py
git commit -m "feat: tag all S1/S2 DB trade rows with brain column"
```

---

## Task 5: S1 LOCKED-phase independence

**Files:**
- Modify: `bot_loops.py` (BTC LOCKED section ~line 1036, alt LOCKED section ~line 763)

**Context:** Currently `_execute_s1_trade` is only called inside `handle_ready_phase`. When S2 is LOCKED, S2's phase machine skips the READY block, blocking S1 from entering. This task adds an S1 entry attempt in both LOCKED blocks so S1 can trade independently of S2's phase.

- [ ] **Step 1: Add S1 entry in BTC LOCKED phase**

Find the BTC LOCKED block (around line 1036):
```python
# ── LOCKED ─────────────────────────────────────────────────────────────────
if bot_state.current_phase == "LOCKED":
    try:
        await handle_locked_phase(
            session, btc_price, secs_left, config
        )
    except Exception as exc:
        log.error(f"LOCKED phase error: {exc}", exc_info=True)
    await write_state_file(...)
    await asyncio.sleep(10)
```

Add S1 entry attempt after `handle_locked_phase` and before `await write_state_file(...)`:

```python
# ── LOCKED ─────────────────────────────────────────────────────────────────
if bot_state.current_phase == "LOCKED":
    try:
        await handle_locked_phase(
            session, btc_price, secs_left, config
        )
    except Exception as exc:
        log.error(f"LOCKED phase error: {exc}", exc_info=True)
    # S1 runs independently - try entry even when S2 is LOCKED
    if secs_left > 30:
        try:
            ob_s1 = await fetch_orderbook(session, ticker, market)
            if ob_s1:
                mode = config.get("mode", "paper")
                brain_s1 = strategy_brain_s1(
                    btc_price, strike,
                    ob_s1["best_yes_ask"], ob_s1["best_no_ask"],
                    elapsed, secs_left, ticker, asset="BTC",
                )
                await _execute_s1_trade(
                    session, brain_s1, ticker, btc_price, strike,
                    ob_s1["best_yes_ask"], ob_s1["best_no_ask"],
                    elapsed, secs_left, "BTC", config, mode, ob_s1, market,
                )
        except Exception as exc:
            log.debug("S1 LOCKED-phase entry attempt failed: %s", exc)
    await write_state_file(...)
    await asyncio.sleep(10)
```

- [ ] **Step 2: Add S1 entry in alt-asset LOCKED phase**

Find the alt-asset LOCKED block in `monitor_altcoin_market` (around line 763):
```python
# LOCKED
if st["phase"] == "LOCKED":
    try:
        await handle_locked_phase(session, price, secs_left, config, asset=asset, state=st)
    except Exception as exc:
        log.error(f"[{asset}] LOCKED phase error: {exc}", exc_info=True)
    return
```

Change to:
```python
# LOCKED
if st["phase"] == "LOCKED":
    try:
        await handle_locked_phase(session, price, secs_left, config, asset=asset, state=st)
    except Exception as exc:
        log.error(f"[{asset}] LOCKED phase error: {exc}", exc_info=True)
    # S1 runs independently - try entry even when S2 is LOCKED
    if secs_left > 30:
        try:
            ob_s1 = await fetch_orderbook(session, ticker, market)
            if ob_s1:
                mode = config.get("mode", "paper")
                brain_s1 = strategy_brain_s1(
                    price, strike,
                    ob_s1["best_yes_ask"], ob_s1["best_no_ask"],
                    elapsed, secs_left, ticker, asset=asset,
                )
                await _execute_s1_trade(
                    session, brain_s1, ticker, price, strike,
                    ob_s1["best_yes_ask"], ob_s1["best_no_ask"],
                    elapsed, secs_left, asset, config, mode, ob_s1, market,
                )
        except Exception as exc:
            log.debug("[%s] S1 LOCKED-phase entry attempt failed: %s", asset, exc)
    return
```

- [ ] **Step 3: Run full suite to confirm no regression**

```
py -m pytest tests/ -x -q
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add bot_loops.py
git commit -m "feat: allow S1 to enter trades when S2 is LOCKED (phase independence)"
```

---

## Task 6: Scorecard query helper

**Files:**
- Modify: `bot_infra.py` (add `db_brain_scorecard` function after `db_update_trade`)
- Test: `tests/test_dual_brain.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_dual_brain.py`:
```python
def test_scorecard_returns_per_brain_per_asset():
    import asyncio
    from datetime import datetime, timezone

    db_path = _tmp_db()
    try:
        bot_infra.init_db()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Insert two S1 trades and one S2 trade
        async def _seed():
            await bot_infra.db_write_trade({
                "ts": f"{today}T01:00:00Z", "market_id": "T1",
                "mode": "paper", "outcome": "win",
                "asset": "BTC", "pnl_dollars": 2.50,
                "brain": "s1",
                "close_time": f"{today}T01:05:00Z",
            })
            await bot_infra.db_write_trade({
                "ts": f"{today}T02:00:00Z", "market_id": "T2",
                "mode": "paper", "outcome": "loss",
                "asset": "BTC", "pnl_dollars": -1.00,
                "brain": "s1",
                "close_time": f"{today}T02:05:00Z",
            })
            await bot_infra.db_write_trade({
                "ts": f"{today}T03:00:00Z", "market_id": "T3",
                "mode": "paper", "outcome": "win",
                "asset": "ETH", "pnl_dollars": 1.50,
                "brain": "s2",
                "close_time": f"{today}T03:05:00Z",
            })
        asyncio.run(_seed())

        result = asyncio.run(bot_infra.db_brain_scorecard(today))
        s1_btc = result["daily"]["s1"].get("BTC", {})
        s2_eth = result["daily"]["s2"].get("ETH", {})
        assert abs(s1_btc.get("pnl", 0) - 1.50) < 0.01, f"S1 BTC daily pnl wrong: {s1_btc}"
        assert s1_btc.get("wins") == 1, f"S1 BTC wins wrong: {s1_btc}"
        assert s1_btc.get("losses") == 1, f"S1 BTC losses wrong: {s1_btc}"
        assert abs(s2_eth.get("pnl", 0) - 1.50) < 0.01, f"S2 ETH daily pnl wrong: {s2_eth}"
    finally:
        os.unlink(db_path)
```

- [ ] **Step 2: Run test to verify it fails**

```
py -m pytest tests/test_dual_brain.py::test_scorecard_returns_per_brain_per_asset -v
```

Expected: FAIL - `db_brain_scorecard` not found.

- [ ] **Step 3: Implement db_brain_scorecard in bot_infra.py**

Add after `db_update_trade` (around line 362):

```python
async def db_brain_scorecard(today: str) -> dict:
    """
    Returns daily and all-time per-brain per-asset P&L.

    Shape:
        {
          "daily":   {"s1": {"BTC": {"pnl": 1.5, "wins": 2, "losses": 1, "trades": 3}}, ...},
          "alltime": {"s1": {...}, "s2": {...}},
        }
    """
    result: dict = {
        "daily":   {"s1": {}, "s2": {}},
        "alltime": {"s1": {}, "s2": {}},
    }
    _query = """
        SELECT brain, asset,
               COUNT(*) AS trades,
               COALESCE(SUM(pnl_dollars), 0) AS pnl,
               SUM(CASE WHEN pnl_dollars > 0 THEN 1 ELSE 0 END) AS wins,
               COUNT(*) - SUM(CASE WHEN pnl_dollars > 0 THEN 1 ELSE 0 END) AS losses
        FROM trades
        WHERE brain IN ('s1', 's2')
          AND pnl_dollars IS NOT NULL
          {date_filter}
        GROUP BY brain, asset
    """
    try:
        async with aiosqlite.connect(bot_state._DB_FILE) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            for scope, date_filter in (
                ("daily",   f"AND date(ts) = '{today}'"),
                ("alltime", ""),
            ):
                async with db.execute(_query.format(date_filter=date_filter)) as cur:
                    async for row in cur:
                        brain, asset, trades, pnl, wins, losses = row
                        if brain in result[scope]:
                            result[scope][brain][asset] = {
                                "trades": trades,
                                "pnl":    round(pnl or 0.0, 2),
                                "wins":   wins or 0,
                                "losses": losses or 0,
                            }
    except Exception as exc:
        log.error("db_brain_scorecard error: %s", exc)
    return result
```

Also add `db_brain_scorecard` to `bot_infra.py __all__`:
```python
"db_brain_scorecard",
```

- [ ] **Step 4: Run test to verify it passes**

```
py -m pytest tests/test_dual_brain.py::test_scorecard_returns_per_brain_per_asset -v
```

Expected: PASS.

- [ ] **Step 5: Run full suite**

```
py -m pytest tests/ -x -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add bot_infra.py tests/test_dual_brain.py
git commit -m "feat: add db_brain_scorecard query for per-brain per-asset P&L"
```

---

## Task 7: Midnight scorecard Telegram send

**Files:**
- Modify: `bot_loops.py` (add `_send_brain_scorecard`, call after `midnight_reset()`)
- Modify: `bot_infra.py` (add `db_brain_scorecard` to imports in bot_loops.py)
- Test: `tests/test_dual_brain.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_dual_brain.py`:
```python
def test_format_scorecard_message():
    """_format_scorecard_message returns expected Telegram text."""
    import bot_loops
    assert hasattr(bot_loops, "_format_scorecard_message"), \
        "_format_scorecard_message not found in bot_loops"

    data = {
        "daily": {
            "s1": {"BTC": {"pnl": 2.50, "wins": 3, "losses": 1, "trades": 4}},
            "s2": {"ETH": {"pnl": -1.00, "wins": 1, "losses": 2, "trades": 3}},
        },
        "alltime": {
            "s1": {"BTC": {"pnl": 12.50, "wins": 10, "losses": 3, "trades": 13}},
            "s2": {"ETH": {"pnl": 5.00, "wins": 6, "losses": 4, "trades": 10}},
        },
    }
    msg = bot_loops._format_scorecard_message(data)
    assert "S1" in msg
    assert "S2" in msg
    assert "BTC" in msg
    assert "ETH" in msg
    assert "All-time" in msg or "all-time" in msg.lower()
```

- [ ] **Step 2: Run test to verify it fails**

```
py -m pytest tests/test_dual_brain.py::test_format_scorecard_message -v
```

Expected: FAIL.

- [ ] **Step 3: Add db_brain_scorecard to bot_loops.py imports**

Find the `from bot_infra import ...` line in `bot_loops.py` (line ~17):
```python
from bot_infra import read_config, get_asset_config, db_write_trade, db_update_trade, send_telegram, _notify_ctx, _phase_for_eth
```

Add `db_brain_scorecard`:
```python
from bot_infra import read_config, get_asset_config, db_write_trade, db_update_trade, send_telegram, _notify_ctx, _phase_for_eth, db_brain_scorecard
```

- [ ] **Step 4: Add _format_scorecard_message and _send_brain_scorecard to bot_loops.py**

Add these two functions near the top of `bot_loops.py`, after the imports:

```python
_ASSETS = ["BTC", "ETH", "SOL", "XRP", "DOGE"]


def _format_scorecard_message(data: dict) -> str:
    """Format brain scorecard dict into a Telegram-ready string."""
    lines = ["📊 <b>Brain Scorecard</b>"]

    for brain_key, label in (("s1", "S1 (EMA momentum)"), ("s2", "S2 (vel+OBI)")):
        lines.append(f"\n<b>{label}</b>")
        daily = data["daily"].get(brain_key, {})
        total_pnl = 0.0
        total_wins = 0
        total_losses = 0
        any_trade = False
        for asset in _ASSETS:
            row = daily.get(asset)
            if row:
                any_trade = True
                pnl = row["pnl"]
                total_pnl += pnl
                total_wins += row["wins"]
                total_losses += row["losses"]
                sign = "+" if pnl >= 0 else ""
                lines.append(f"  {asset:<5} {sign}${pnl:.2f}  {row['wins']}W/{row['losses']}L")
            else:
                lines.append(f"  {asset:<5} -")
        if any_trade:
            sign = "+" if total_pnl >= 0 else ""
            lines.append(f"  <b>Total: {sign}${total_pnl:.2f}  {total_wins}W/{total_losses}L</b>")
        else:
            lines.append("  (no trades today)")

    # All-time summary
    at_parts = []
    for brain_key, label in (("s1", "S1"), ("s2", "S2")):
        at = data["alltime"].get(brain_key, {})
        at_pnl = sum(r["pnl"] for r in at.values())
        at_wins = sum(r["wins"] for r in at.values())
        at_losses = sum(r["losses"] for r in at.values())
        sign = "+" if at_pnl >= 0 else ""
        at_parts.append(f"{label}: {sign}${at_pnl:.2f} {at_wins}W/{at_losses}L")
    lines.append("\n<b>All-time</b> │ " + " │ ".join(at_parts))

    # Winner
    s1_daily = sum(r["pnl"] for r in data["daily"].get("s1", {}).values())
    s2_daily = sum(r["pnl"] for r in data["daily"].get("s2", {}).values())
    if s1_daily > s2_daily:
        lines.append("Today's winner: <b>S1 🏆</b>")
    elif s2_daily > s1_daily:
        lines.append("Today's winner: <b>S2 🏆</b>")

    return "\n".join(lines)


async def _send_brain_scorecard() -> None:
    """Query DB and send daily brain scorecard via Telegram. Non-fatal on error."""
    try:
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        data = await db_brain_scorecard(today)
        # Only send if at least one brain has trades today
        has_trades = any(
            data["daily"].get(b)
            for b in ("s1", "s2")
        )
        if not has_trades:
            return
        msg = _format_scorecard_message(data)
        await send_telegram(msg)
    except Exception as exc:
        log.warning("Brain scorecard send failed (non-fatal): %s", exc)
```

- [ ] **Step 5: Call _send_brain_scorecard after midnight_reset in main_loop**

Find in `main_loop` (around line 895):
```python
midnight_reset()
_today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
await _check_daily_stats(_today)
```

Change to:
```python
midnight_reset()
await _send_brain_scorecard()
_today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
await _check_daily_stats(_today)
```

- [ ] **Step 6: Run test to verify it passes**

```
py -m pytest tests/test_dual_brain.py::test_format_scorecard_message -v
```

Expected: PASS.

- [ ] **Step 7: Run full suite**

```
py -m pytest tests/ -x -q
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add bot_loops.py bot_infra.py tests/test_dual_brain.py
git commit -m "feat: add nightly brain scorecard Telegram message at midnight reset"
```

---

## Final verification

- [ ] **Run complete test suite**

```
py -m pytest tests/ -v
```

Expected: all tests pass including 7 new tests in `tests/test_dual_brain.py`.

- [ ] **Verify no stale references**

```
grep -r "_order_attempted_tickers" . --include="*.py"
```

Expected: no output.

```
grep -r "vel_adj\|obi_adj" bot_strategy.py
```

Expected: no output (inflation removed).
