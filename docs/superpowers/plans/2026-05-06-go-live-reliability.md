# Go-Live Reliability Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix six bugs that could silently lose real money or leave positions untracked before switching from paper to live mode.

**Architecture:** Each task targets one bug in isolation. All six bugs are in the existing `bot_loops.py`, `bot_market.py`, `bot_risk.py`, and `bot_strategy.py` modules. No new modules - only targeted edits and one new test file. Every task starts from a passing 606-test baseline and ends with 606+ tests passing.

**Tech Stack:** Python 3.11, asyncio, aiohttp, aiosqlite, pytest, pytest-asyncio.

---

## Pre-flight

Run before starting **every** task:
```
py -3 -m pytest tests/ -x -q
```
All 606 tests must pass. If they don't, stop and investigate.

---

## Task 1: Remove `_session_ev_adjustment` dead stub

**Spec ref:** Bug 4 - defined in `bot_strategy.py:37-38`, imported but never meaningfully called, appears in `bot_risk.py:191` where it contributes 0.0 (no effect).

**Files:**
- Modify: `bot_strategy.py:37-38`
- Modify: `bot_risk.py:31` (remove from import)
- Modify: `bot_risk.py:191` (inline the 0.0 away)
- Modify: `bot_loops.py:25` (remove from import)
- Create: `tests/test_go_live_reliability.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_go_live_reliability.py`:

```python
"""Tests for go-live reliability fixes."""
import pathlib
import pytest


def test_session_ev_adjustment_removed():
    """_session_ev_adjustment must not exist in any bot module - it was a dead stub."""
    for fname in ["bot_strategy.py", "bot_loops.py", "bot_risk.py"]:
        src = pathlib.Path(fname).read_text(encoding="utf-8")
        assert "_session_ev_adjustment" not in src, (
            f"Found '_session_ev_adjustment' in {fname} - remove it"
        )
```

- [ ] **Step 2: Run to confirm it fails**

```
py -3 -m pytest tests/test_go_live_reliability.py::test_session_ev_adjustment_removed -v
```

Expected: FAIL - `_session_ev_adjustment` currently exists in all three files.

- [ ] **Step 3: Remove the function from `bot_strategy.py`**

In `bot_strategy.py`, delete lines 37-38 entirely (the function definition):
```python
# REMOVE these two lines:
def _session_ev_adjustment() -> float:
    return 0.0
```

Also delete the blank lines that follow (lines 39-41 are blank; remove them).

- [ ] **Step 4: Update `bot_risk.py` import (line 31)**

Change:
```python
from bot_strategy import _session_ev_adjustment, _strategy_name_for
```
To:
```python
from bot_strategy import _strategy_name_for
```

- [ ] **Step 5: Update `bot_risk.py` write_state_file line 191**

Change:
```python
            "min_ev_pct": round((config.get("min_ev_base", 3.0) / 100.0 + _session_ev_adjustment()) * 100),
```
To:
```python
            "min_ev_pct": round(config.get("min_ev_base", 3.0)),
```

(The formula `(x / 100.0 + 0.0) * 100` simplifies to `x`.)

- [ ] **Step 6: Update `bot_loops.py` import (line 25)**

Change:
```python
    track_contract_price, _session_ev_adjustment, _strategy_name_for,
```
To:
```python
    track_contract_price, _strategy_name_for,
```

- [ ] **Step 7: Run the new test**

```
py -3 -m pytest tests/test_go_live_reliability.py::test_session_ev_adjustment_removed -v
```

Expected: PASS.

- [ ] **Step 8: Run full suite**

```
py -3 -m pytest tests/ -x -q
```

Expected: 606+ passed.

- [ ] **Step 9: Commit**

```
git add bot_strategy.py bot_risk.py bot_loops.py tests/test_go_live_reliability.py
git commit -m "fix: remove _session_ev_adjustment dead stub"
```

---

## Task 2: Fix daily limit state reset on mode switch

**Spec ref:** Bug 5 - if `limit_triggered=True` from a previous mode (e.g., demo), and the mode is switched to live, the first call to `check_daily_limits` sees a stale trigger and may prematurely return `(True, ...)` before evaluating today's actual live P&L.

**Files:**
- Modify: `bot_risk.py:58` (add mode-mismatch reset at top of `check_daily_limits`)
- Modify: `tests/test_go_live_reliability.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_go_live_reliability.py`:

```python
import pytest
import pytest_asyncio


@pytest.mark.asyncio
async def test_daily_limit_resets_when_mode_changes(monkeypatch):
    """
    If limit was triggered in demo mode but we're now checking in live mode,
    check_daily_limits must reset limit_triggered before evaluating live P&L.
    """
    import bot_state
    from bot_risk import check_daily_limits

    # Simulate: limit triggered during an earlier demo session
    bot_state.limit_triggered = True
    bot_state.limit_reason = "daily loss limit reached"
    bot_state.pre_limit_mode = "demo"

    # Mock db_get_today_pnl so no real DB is hit; live P&L is $0 (no new trigger)
    async def _fake_pnl(mode):
        return 0.0
    monkeypatch.setattr("bot_risk.db_get_today_pnl", _fake_pnl)

    config = {
        "mode": "live",
        "daily_loss_limit_dollars": 20,
        "daily_profit_target_dollars": 50,
    }

    triggered, reason = await check_daily_limits(config)

    assert not bot_state.limit_triggered, "limit_triggered must be reset when mode changed"
    assert triggered is False, "no pnl → no new trigger after reset"

    # Cleanup
    bot_state.limit_triggered = False
    bot_state.limit_reason = ""
    bot_state.pre_limit_mode = None
```

- [ ] **Step 2: Run to confirm it fails**

```
py -3 -m pytest tests/test_go_live_reliability.py::test_daily_limit_resets_when_mode_changes -v
```

Expected: FAIL - currently `check_daily_limits` returns `(True, "daily loss limit reached")` without resetting.

- [ ] **Step 3: Add reset logic at the top of `check_daily_limits` in `bot_risk.py`**

Find `check_daily_limits` (line 58). After `mode = config.get("mode", "paper")` and the early `if mode == "paper": return False, ""` guard, add:

```python
    # If the mode changed since the limit was triggered (e.g., demo → live),
    # reset so the new mode starts with a fresh daily count.
    if (
        bot_state.limit_triggered
        and bot_state.pre_limit_mode
        and bot_state.pre_limit_mode != mode
    ):
        bot_state.limit_triggered = False
        bot_state.limit_reason = ""
        bot_state.pre_limit_mode = None
        log.info(f"Mode changed to '{mode}' - resetting daily limit state.")
```

The full top of the function becomes:

```python
async def check_daily_limits(config: dict) -> tuple[bool, str]:
    """..."""
    mode = config.get("mode", "paper")
    if mode == "paper":
        return False, ""

    # If the mode changed since the limit was triggered (e.g., demo → live),
    # reset so the new mode starts with a fresh daily count.
    if (
        bot_state.limit_triggered
        and bot_state.pre_limit_mode
        and bot_state.pre_limit_mode != mode
    ):
        bot_state.limit_triggered = False
        bot_state.limit_reason = ""
        bot_state.pre_limit_mode = None
        log.info(f"Mode changed to '{mode}' - resetting daily limit state.")

    pnl = await db_get_today_pnl(mode)
    ...
```

- [ ] **Step 4: Run the new test**

```
py -3 -m pytest tests/test_go_live_reliability.py::test_daily_limit_resets_when_mode_changes -v
```

Expected: PASS.

- [ ] **Step 5: Run full suite**

```
py -3 -m pytest tests/ -x -q
```

Expected: 606+ passed.

- [ ] **Step 6: Commit**

```
git add bot_risk.py tests/test_go_live_reliability.py
git commit -m "fix: reset daily limit state when mode changes (paper→live transition)"
```

---

## Task 3: Write state file immediately when BTC position transitions to LOCKED

**Spec ref:** Bug 6 - after a live order fills, `current_phase = "LOCKED"` is set in memory (bot_loops.py:467-468) but the state file isn't written until the next main loop iteration (~10 seconds later). A crash in that window means on restart the phase appears as WATCH and the bot could attempt a second trade on the same market.

**Files:**
- Modify: `bot_loops.py:463-468` (add `write_state_file` call right after LOCKED transition)
- Modify: `tests/test_go_live_reliability.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_go_live_reliability.py`:

```python
def test_write_state_called_on_locked_transition():
    """
    After handle_ready_phase transitions to LOCKED, the state file must be
    written in the same function call - not deferred to the next loop tick.
    Verify by grepping the source for write_state_file inside handle_ready_phase.
    """
    import ast
    src = pathlib.Path("bot_loops.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # Find handle_ready_phase function body
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_ready_phase":
            fn_src = ast.get_source_segment(src, node)
            assert fn_src is not None
            # It must call write_state_file after setting phase=LOCKED
            locked_pos = fn_src.find('"LOCKED"')
            write_pos = fn_src.rfind("write_state_file")
            assert write_pos > locked_pos, (
                "write_state_file must be called AFTER setting phase='LOCKED' in handle_ready_phase"
            )
            return
    pytest.fail("handle_ready_phase not found in bot_loops.py")
```

- [ ] **Step 2: Run to confirm it fails**

```
py -3 -m pytest tests/test_go_live_reliability.py::test_write_state_called_on_locked_transition -v
```

Expected: FAIL - currently `write_state_file` is not called anywhere inside `handle_ready_phase`.

- [ ] **Step 3: Add the state file write after LOCKED transition in `bot_loops.py`**

Find the block at approximately line 463 that reads:

```python
    if _use_state:
        state["position"] = _new_position
        state["phase"] = "LOCKED"
    else:
        bot_state.current_position = _new_position
        bot_state.current_phase = "LOCKED"
```

Immediately after that block (after the `else:` branch closes), add:

```python
    # Write state file immediately for BTC so crash recovery sees LOCKED phase.
    # Non-BTC positions are in _asset_states which write_state_file reads normally.
    if not _use_state:
        await write_state_file(
            config, market, "LOCKED", secs_left, btc_price,
            score, bot_state.last_confidence_breakdown, "trade", "",
        )
```

`score` and `bot_state.last_confidence_breakdown` are both in scope at this point (set at lines 258 and 343 respectively, before the order is placed).

- [ ] **Step 4: Run the new test**

```
py -3 -m pytest tests/test_go_live_reliability.py::test_write_state_called_on_locked_transition -v
```

Expected: PASS.

- [ ] **Step 5: Run full suite**

```
py -3 -m pytest tests/ -x -q
```

Expected: 606+ passed.

- [ ] **Step 6: Commit**

```
git add bot_loops.py tests/test_go_live_reliability.py
git commit -m "fix: write state file immediately when BTC transitions to LOCKED"
```

---

## Task 4: Portfolio fallback when fill verification fails in live mode

**Spec ref:** Bug 1 - `_verify_order_fill` returns `False` on network exception (bot_market.py:813-814). In `handle_ready_phase`, `fill_confirmed=False` immediately sets `phase=DONE` without checking if the order actually went through. In live mode, the order was already placed and contracts may be sitting open on Kalshi with no bot record.

**Files:**
- Modify: `bot_loops.py:17-22` (add `_portfolio_has_position` to import)
- Modify: `bot_loops.py:388-396` (add portfolio check before declaring DONE)
- Modify: `tests/test_go_live_reliability.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_go_live_reliability.py`:

```python
def test_portfolio_fallback_wired_in_handle_ready_phase():
    """
    When fill_confirmed=False, handle_ready_phase must call _portfolio_has_position
    in live/demo mode before setting phase=DONE - not immediately fall through.
    """
    src = pathlib.Path("bot_loops.py").read_text(encoding="utf-8")
    # _portfolio_has_position must be imported
    assert "_portfolio_has_position" in src, (
        "_portfolio_has_position must be imported in bot_loops.py"
    )
    # It must appear inside handle_ready_phase (not just in imports)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_ready_phase":
            fn_src = ast.get_source_segment(src, node)
            assert "_portfolio_has_position" in fn_src, (
                "_portfolio_has_position must be called inside handle_ready_phase"
            )
            return
    pytest.fail("handle_ready_phase not found")
```

- [ ] **Step 2: Run to confirm it fails**

```
py -3 -m pytest tests/test_go_live_reliability.py::test_portfolio_fallback_wired_in_handle_ready_phase -v
```

Expected: FAIL - `_portfolio_has_position` is not imported in bot_loops.py.

- [ ] **Step 3: Add `_portfolio_has_position` to the import in `bot_loops.py`**

Find the `from bot_market import (` block (lines 17-22). Add `_portfolio_has_position` to it:

```python
from bot_market import (
    fetch_current_market, fetch_market_for_asset, fetch_orderbook,
    seconds_remaining, seconds_elapsed, parse_strike, get_btc_price,
    kalshi_headers, _simulated_amm_midpoint, _log_price_validation,
    calculate_contracts, implied_prob, place_order, _portfolio_has_position,
)
```

- [ ] **Step 4: Replace the `if not fill_confirmed:` block in `handle_ready_phase`**

Find the block starting at approximately line 388:

```python
    if not fill_confirmed:
        if _use_state: state["phase"] = "DONE"
        else: bot_state.current_phase = "DONE"
        _eval_snap.update({"status": "SKIPPED", "skip_reason": "order not filled"})
        if _use_state: state["eval"] = dict(_eval_snap)
        else: bot_state._asset_eval[asset] = dict(_eval_snap)
        bot_state.last_action, bot_state.last_skip_reason = "skip", "order not filled"
        log.info(f"{ticker}: order not filled. Moving to DONE.")
        return
```

Replace with:

```python
    if not fill_confirmed and mode != "paper":
        # In live/demo mode, a network error in _verify_order_fill could mask a real fill.
        # Check the portfolio directly before declaring the order unfilled.
        try:
            if await _portfolio_has_position(session, ticker, side):
                fill_price = int(entry_price_cents)
                fill_confirmed = True
                log.warning(
                    f"{ticker}: fill_confirmed=False but portfolio shows open position "
                    f"- treating as filled at {fill_price}c"
                )
        except Exception as _pf_exc:
            log.warning(f"{ticker}: portfolio fallback check failed: {_pf_exc}")

    if not fill_confirmed:
        if _use_state: state["phase"] = "DONE"
        else: bot_state.current_phase = "DONE"
        _eval_snap.update({"status": "SKIPPED", "skip_reason": "order not filled"})
        if _use_state: state["eval"] = dict(_eval_snap)
        else: bot_state._asset_eval[asset] = dict(_eval_snap)
        bot_state.last_action, bot_state.last_skip_reason = "skip", "order not filled"
        log.info(f"{ticker}: order not filled. Moving to DONE.")
        return
```

- [ ] **Step 5: Run the new test**

```
py -3 -m pytest tests/test_go_live_reliability.py::test_portfolio_fallback_wired_in_handle_ready_phase -v
```

Expected: PASS.

- [ ] **Step 6: Run full suite**

```
py -3 -m pytest tests/ -x -q
```

Expected: 606+ passed.

- [ ] **Step 7: Commit**

```
git add bot_loops.py tests/test_go_live_reliability.py
git commit -m "fix: portfolio fallback check before declaring fill unconfirmed in live/demo mode"
```

---

## Task 5: Non-BTC LOCKED position recovery on restart

**Spec ref:** Bug 3 - `bot_state._asset_states` (the per-asset phase/position dict for ETH/SOL/XRP/DOGE) is in-memory only. A crash while a non-BTC asset is in LOCKED phase means the position is forgotten on restart - the bot restarts in WATCH phase and could re-trade. BTC positions already have state file recovery (bot_loops.py:846-864); this task extends that to non-BTC.

**Files:**
- Modify: `bot_risk.py:286` (add `non_btc_positions` to state dict in `write_state_file`)
- Modify: `bot_loops.py:863` (add non-BTC recovery block after existing BTC recovery)
- Modify: `tests/test_go_live_reliability.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_go_live_reliability.py`:

```python
def test_non_btc_positions_persisted_in_state_file():
    """write_state_file must include a non_btc_positions key so non-BTC LOCKED
    positions survive a restart."""
    src = pathlib.Path("bot_risk.py").read_text(encoding="utf-8")
    assert '"non_btc_positions"' in src, (
        'write_state_file must write a "non_btc_positions" key to the state JSON'
    )


def test_non_btc_positions_recovered_on_startup():
    """main_loop startup must read non_btc_positions from the state file and
    restore them into bot_state._asset_states."""
    src = pathlib.Path("bot_loops.py").read_text(encoding="utf-8")
    assert "non_btc_positions" in src, (
        "main_loop must read non_btc_positions from the state file on startup"
    )
```

- [ ] **Step 2: Run to confirm both fail**

```
py -3 -m pytest tests/test_go_live_reliability.py::test_non_btc_positions_persisted_in_state_file tests/test_go_live_reliability.py::test_non_btc_positions_recovered_on_startup -v
```

Expected: both FAIL.

- [ ] **Step 3: Add `non_btc_positions` to `write_state_file` in `bot_risk.py`**

Find the line `state["assets"] = assets_snap` (approximately line 286). Immediately after it, add:

```python
    non_btc_locked: dict = {}
    for _a, _st in bot_state._asset_states.items():
        if _st.get("phase") == "LOCKED" and _st.get("position"):
            non_btc_locked[_a] = {
                "phase": "LOCKED",
                "position": _st["position"],
            }
    state["non_btc_positions"] = non_btc_locked
```

- [ ] **Step 4: Add non-BTC recovery block in `main_loop` in `bot_loops.py`**

Find the existing BTC recovery block that ends with `except Exception: pass  # fresh start, no state to recover` (approximately line 863-864). Immediately after that `except` block (still before the connector/session creation), add:

```python
    # Recover non-BTC LOCKED positions from state file
    try:
        with open(bot_state._STATE_FILE, "r") as _sf_nbtc:
            _saved_nbtc = json.load(_sf_nbtc)
        for _a, _apos in _saved_nbtc.get("non_btc_positions", {}).items():
            if _apos.get("phase") == "LOCKED" and _apos.get("position"):
                if _a not in bot_state._asset_states:
                    bot_state._asset_states[_a] = {}
                bot_state._asset_states[_a]["phase"] = "LOCKED"
                bot_state._asset_states[_a]["position"] = _apos["position"]
                bot_state._asset_states[_a].setdefault("order_attempted", set())
                bot_state._asset_states[_a].setdefault("eval", {})
                log.warning(
                    "Recovered LOCKED position for %s from state file: trade_id=%s",
                    _a, _apos["position"].get("trade_id"),
                )
    except Exception:
        pass  # no state file yet or key missing - fresh start
```

- [ ] **Step 5: Run the new tests**

```
py -3 -m pytest tests/test_go_live_reliability.py::test_non_btc_positions_persisted_in_state_file tests/test_go_live_reliability.py::test_non_btc_positions_recovered_on_startup -v
```

Expected: both PASS.

- [ ] **Step 6: Run full suite**

```
py -3 -m pytest tests/ -x -q
```

Expected: 606+ passed.

- [ ] **Step 7: Commit**

```
git add bot_risk.py bot_loops.py tests/test_go_live_reliability.py
git commit -m "fix: persist and recover non-BTC LOCKED positions across restarts"
```

---

## Task 6: Auto-settle S1 orphan positions on startup

**Spec ref:** Bug 2 - S1 positions that were open when the bot last stopped are logged as warnings but not settled. Those contracts are live on Kalshi and will resolve, but the bot's DB never records the outcome. In live mode, the P&L from those trades is permanently lost from accounting.

**Files:**
- Modify: `bot_risk.py` (add `_settle_s1_orphans` function; add it to `__all__`)
- Modify: `bot_loops.py:29` (import `_settle_s1_orphans` from bot_risk)
- Modify: `bot_loops.py:869-884` (replace warning-only block with call to `_settle_s1_orphans`)
- Modify: `bot_loops.py:889` (call `_settle_s1_orphans` after session is created)
- Modify: `tests/test_go_live_reliability.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_go_live_reliability.py`:

```python
def test_s1_orphan_auto_settlement_wired():
    """_settle_s1_orphans must be imported and called in main_loop - not just warn."""
    import ast
    src = pathlib.Path("bot_loops.py").read_text(encoding="utf-8")
    assert "_settle_s1_orphans" in src, (
        "_settle_s1_orphans must be imported and called in bot_loops.py main_loop"
    )
    # The warning-only orphan block must be gone
    assert "check Kalshi fills manually" not in src, (
        "The warning-only S1 orphan block must be replaced with _settle_s1_orphans"
    )
```

- [ ] **Step 2: Run to confirm it fails**

```
py -3 -m pytest tests/test_go_live_reliability.py::test_s1_orphan_auto_settlement_wired -v
```

Expected: FAIL.

- [ ] **Step 3: Add `_settle_s1_orphans` to `bot_risk.py`**

Find the `__all__` list in `bot_risk.py` (approximately line 43). Add `"_settle_s1_orphans"` to the list under Trade execution:

```python
    # Trade execution (absorbed from bot_trade)
    "_execute_s1_trade", "_settle_s1_trade", "_try_settle_orphaned_s1",
    "_settle_s1_orphans",
```

Then add this function just before `_try_settle_orphaned_s1` (approximately line 495):

```python
async def _settle_s1_orphans(
    session: "aiohttp.ClientSession",
    config: dict,
) -> None:
    """
    On startup, find all pending S1 DB records and settle any that resolved
    while the bot was offline. Re-adds still-open trades to _s1_pending_trades
    so the live loop can settle them when they expire.
    """
    try:
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(bot_state._DB_FILE)
        rows = conn.execute(
            "SELECT id, market_id, side, contracts, entry_price_cents, mode, asset, "
            "COALESCE(entry_signals, '{}') AS entry_signals "
            "FROM trades WHERE strategy_variant='strategy1' AND outcome='pending'"
        ).fetchall()
        conn.close()
    except Exception as exc:
        log.warning("S1 orphan query failed on startup: %s", exc)
        return

    if not rows:
        return

    btc_price = bot_state.btc_prices[-1][1] if bot_state.btc_prices else 0.0

    for row in rows:
        trade_id, ticker, side, contracts, entry_price_cents, mode, asset, signals_json = row
        try:
            signals = json.loads(signals_json) if signals_json else {}
            strike = float(signals.get("strike", 0) or 0)
        except Exception:
            strike = 0.0

        bot_state._s1_pending_trades[ticker] = {
            "trade_id": trade_id,
            "ticker": ticker,
            "side": side,
            "contracts": contracts,
            "entry_price_cents": entry_price_cents,
            "mode": mode,
            "strike": strike,
            "entry_ts": 0.0,
            "asset": asset,
        }

        market_result = None
        try:
            _path = f"/markets/{ticker}"
            async with session.get(
                bot_state.KALSHI_BASE_URL + _path,
                headers=kalshi_headers("GET", _path),
                timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
            ) as _resp:
                _mdata = await _resp.json()
            market_result = (_mdata.get("market") or _mdata).get("result")
        except Exception as exc:
            log.warning("S1 orphan: Kalshi fetch failed for %s: %s", ticker, exc)

        if market_result in ("yes", "no"):
            log.info("S1 orphan %s: resolved (%s) - settling now", ticker, market_result)
            await _settle_s1_trade(ticker, market_result, btc_price, config, asset)
        else:
            log.info(
                "S1 orphan %s: market still open or unknown result - re-added to pending",
                ticker,
            )
```

- [ ] **Step 4: Update `bot_loops.py` imports (line 29)**

Change:
```python
from bot_risk import (
    check_daily_limits, midnight_reset, write_state_file, _log_entry,
    _execute_s1_trade, _settle_s1_trade, _try_settle_orphaned_s1,
)
```
To:
```python
from bot_risk import (
    check_daily_limits, midnight_reset, write_state_file, _log_entry,
    _execute_s1_trade, _settle_s1_trade, _try_settle_orphaned_s1,
    _settle_s1_orphans,
)
```

- [ ] **Step 5: Replace warning-only orphan block in `main_loop`**

Find the block in `main_loop` that starts with `# Warn about S1 positions that were open when the bot last stopped.` (approximately lines 866-884). Replace the entire block (from the comment through `except Exception as _s1_chk_exc: log.debug(...)`) with a single comment:

```python
    # S1 orphan settlement happens after the aiohttp session is created below.
```

- [ ] **Step 6: Call `_settle_s1_orphans` after session creation**

Find the line `async with aiohttp.ClientSession(connector=connector) as session:` (approximately line 889). The next two lines create the non-BTC background task. Insert the orphan settlement call and a fresh config read between them:

```python
    async with aiohttp.ClientSession(connector=connector) as session:
        # Settle any S1 positions that resolved while the bot was offline.
        _startup_config = read_config()
        await _settle_s1_orphans(session, _startup_config)

        # Non-BTC assets run in a separate background task so they aren't
        # gated by the BTC state machine's continue/sleep cycle.
        asyncio.create_task(_non_btc_asset_loop(session))
```

- [ ] **Step 7: Run the new test**

```
py -3 -m pytest tests/test_go_live_reliability.py::test_s1_orphan_auto_settlement_wired -v
```

Expected: PASS.

- [ ] **Step 8: Run full suite**

```
py -3 -m pytest tests/ -x -q
```

Expected: 606+ passed.

- [ ] **Step 9: Commit**

```
git add bot_risk.py bot_loops.py tests/test_go_live_reliability.py
git commit -m "fix: auto-settle S1 orphan positions on startup instead of warn-only"
```

---

## Final verification

- [ ] Run full test suite one more time:

```
py -3 -m pytest tests/ -q
```

Expected: 612 passed (606 original + 6 new tests), 0 failed.

- [ ] Confirm all 6 bugs are addressed:

```
py -3 -m pytest tests/test_go_live_reliability.py -v
```

Expected: 6 tests, all PASS.

- [ ] Push to GitHub:

```
git push origin main
```
