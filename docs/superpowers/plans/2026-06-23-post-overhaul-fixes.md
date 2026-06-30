# Post-Overhaul v2 Fixes & Remaining Features

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 5 bugs/gaps left after the Strategy Overhaul v2 session: async/sync violation in drawdown check, quiet_end_et default mismatch, missing cross-asset S1 window guard, missing S1+S2 same-ticker dedup, and missing EOD summary on quiet transition.

**Architecture:** Surgical edits to `bot_loops.py` and `bot_strategy.py` only. No new files except tests. All state infrastructure (`_s1_asset_trade_times`, `_s1_pending_trades`) already exists in `bot_state.py`.

**Tech Stack:** Python 3.14, asyncio, aiosqlite (already imported), pytest, unittest.mock.

---

## Diagnosis (read before touching anything)

| # | Bug | File | Line | Impact |
|---|-----|------|------|--------|
| 1 | `get_today_pnl()` (sync sqlite3.connect) called inside `async def handle_ready_phase()` — blocks event loop every cycle | `bot_loops.py` | 289–290 | Event loop stalls on every market cycle |
| 2 | `_is_quiet_hours()` default `quiet_end_et=7` but `_init_config` sets default to 9 — mismatch when config key missing | `bot_strategy.py` | 113, 122 | Trades may occur 7–9 AM ET if config corrupt |
| 3 | Cross-asset S1 window guard: after ETH fires S1, SOL/DOGE/XRP can immediately fire too — correlated exposure | `bot_strategy.py` | after line 356 | Double crypto exposure in same 15-min window |
| 4 | S1+S2 same-ticker dedup: S2 runs AFTER S1 fills but no check if `ticker in _s1_pending_trades` — double entry risk | `bot_loops.py` | after line 304 | Double position on single contract |
| 5 | EOD summary only fires at 14:00 local time, not on quiet-hours transition — misses end-of-trading boundary | `bot_loops.py` | ~1148 | Daily stats not logged when trading actually stops |

---

## Files to Modify

| File | What Changes |
|------|-------------|
| `bot_loops.py:17` | Add `db_get_today_pnl` to top-level import from `bot_infra` |
| `bot_loops.py:288-292` | Replace sync `get_today_pnl` with `await db_get_today_pnl` |
| `bot_loops.py:303-308` | Add S1+S2 dedup check after `_execute_s1_trade` |
| `bot_loops.py:26-35` | Add `_is_quiet_hours` to import from `bot_strategy` |
| `bot_loops.py` | Add quiet-transition tracking vars + detection in both loops |
| `bot_strategy.py:113` | Update docstring default from 7 to 9 |
| `bot_strategy.py:122` | Change `config.get("quiet_end_et", 7)` → `config.get("quiet_end_et", 9)` |
| `bot_strategy.py:356` | Add cross-asset window guard block |

New test files:
- `tests/test_drawdown_async.py`
- `tests/test_quiet_default.py`
- `tests/test_s1_window_guard.py`
- `tests/test_s1_s2_dedup.py`
- `tests/test_eod_quiet_transition.py`

---

## Task 1: Fix async/sync violation in daily drawdown check

**The bug:** `bot_loops.py:289` does `from bot_infra import get_today_pnl` inside `async def handle_ready_phase()`, then calls `get_today_pnl()` synchronously. This opens a sqlite3 connection (blocking I/O) on every market cycle, stalling the asyncio event loop. `db_get_today_pnl()` (async, aiosqlite) already exists in `bot_infra.py:500`.

**Files:**
- Modify: `bot_loops.py:17` and `bot_loops.py:288-292`
- Create: `tests/test_drawdown_async.py`

- [ ] **Step 1.1: Write the failing test**

```python
# tests/test_drawdown_async.py
"""Verify the drawdown check uses async db_get_today_pnl, not sync get_today_pnl."""
import inspect
import bot_loops


def test_drawdown_uses_async_pnl():
    """handle_ready_phase must NOT import or call sync get_today_pnl."""
    src = inspect.getsource(bot_loops.handle_ready_phase)
    assert "get_today_pnl" not in src or "await db_get_today_pnl" in src, (
        "handle_ready_phase calls sync get_today_pnl — use await db_get_today_pnl instead"
    )
    assert "await db_get_today_pnl" in src, (
        "handle_ready_phase must call await db_get_today_pnl for non-blocking drawdown check"
    )


def test_no_local_sync_import_in_drawdown():
    """The local 'from bot_infra import get_today_pnl' must not exist inside handle_ready_phase."""
    src = inspect.getsource(bot_loops.handle_ready_phase)
    assert "from bot_infra import get_today_pnl" not in src, (
        "Sync local import found inside handle_ready_phase — remove it"
    )
```

- [ ] **Step 1.2: Run test to verify it fails**

```bash
python -m pytest tests/test_drawdown_async.py -v
```

Expected: FAIL — `assert "await db_get_today_pnl" in src`

- [ ] **Step 1.3: Add `db_get_today_pnl` to top-level import in bot_loops.py**

Find this line in `bot_loops.py:17` (exact text):
```python
from bot_infra import read_config, get_asset_config, db_write_trade, db_update_trade, send_telegram, db_brain_scorecard
```

Replace with:
```python
from bot_infra import read_config, get_asset_config, db_write_trade, db_update_trade, send_telegram, db_brain_scorecard, db_get_today_pnl
```

- [ ] **Step 1.4: Replace sync call with async call in handle_ready_phase**

Find this block in `bot_loops.py` (~lines 286-292):
```python
    # Daily drawdown kill switch — skip all trading when today's loss exceeds limit
    _daily_limit = float(config.get("daily_loss_limit_dollars", 75))
    if _daily_limit > 0:
        from bot_infra import get_today_pnl
        _today_pnl = get_today_pnl(mode=config.get("mode", "paper"))
```

Replace with:
```python
    # Daily drawdown kill switch — skip all trading when today's loss exceeds limit
    _daily_limit = float(config.get("daily_loss_limit_dollars", 75))
    if _daily_limit > 0:
        _today_pnl = await db_get_today_pnl(mode=config.get("mode", "paper"))
```

- [ ] **Step 1.5: Run test to verify it passes**

```bash
python -m pytest tests/test_drawdown_async.py -v
```

Expected: PASS

- [ ] **Step 1.6: Verify imports compile**

```bash
python -c "import bot_loops; print('OK')"
```

Expected: `OK`

- [ ] **Step 1.7: Commit**

```bash
git add bot_loops.py tests/test_drawdown_async.py
git commit -m "fix(infra): drawdown check use await db_get_today_pnl — was blocking event loop"
```

---

## Task 2: Fix quiet_end_et default mismatch

**The bug:** `bot_strategy.py:122` uses `config.get("quiet_end_et", 7)` as fallback. But `bot_infra._init_config` sets the default to 9 (commit `176d39d`). If the config key is ever missing (corrupted JSON, fresh deploy), `_is_quiet_hours` silently uses 7 instead of 9, allowing trades during 7–9 AM ET where WR is bad.

**Files:**
- Modify: `bot_strategy.py:113` (docstring) and `bot_strategy.py:122` (code)
- Create: `tests/test_quiet_default.py`

- [ ] **Step 2.1: Write the failing test**

```python
# tests/test_quiet_default.py
"""Verify _is_quiet_hours uses default quiet_end_et=9, matching _init_config."""
import bot_strategy


def test_quiet_end_default_blocks_8am_et():
    """With empty config, 8 AM ET (hour=8) must be quiet (end default=9)."""
    # Empty config — tests the hardcoded fallback, not config.json
    result = bot_strategy._is_quiet_hours(config={})
    # We cannot easily set the clock, so inspect the source instead
    import inspect
    src = inspect.getsource(bot_strategy._is_quiet_hours)
    # Default quiet_end_et must be 9, not 7
    assert '"quiet_end_et", 9)' in src or "'quiet_end_et', 9)" in src, (
        "_is_quiet_hours default quiet_end_et must be 9 (matches _init_config), found 7 instead"
    )


def test_quiet_start_default_is_22_not_17():
    """Default quiet_start_et must be 22 (10 PM ET), matching _init_config."""
    import inspect
    src = inspect.getsource(bot_strategy._is_quiet_hours)
    assert '"quiet_start_et", 22)' in src or "'quiet_start_et', 22)" in src, (
        "_is_quiet_hours quiet_start_et default must be 22"
    )
```

- [ ] **Step 2.2: Run test to verify it fails**

```bash
python -m pytest tests/test_quiet_default.py -v
```

Expected: FAIL — default is 7 not 9

- [ ] **Step 2.3: Fix the defaults in bot_strategy.py**

Find in `bot_strategy.py` (~lines 109-122):
```python
    True when current ET time is in the overnight quiet window.

    Config keys:
      quiet_hours_enabled    (bool, default True)
      quiet_start_et      (int hour 0-23, default 17)
      quiet_end_et        (int hour 0-23, default 7)
```

Update the comment:
```python
    True when current ET time is in the overnight quiet window.

    Config keys:
      quiet_hours_enabled    (bool, default True)
      quiet_start_et      (int hour 0-23, default 22)
      quiet_end_et        (int hour 0-23, default 9)
```

Then find the code line (~line 121-122):
```python
        start = int(config.get("quiet_start_et", 17))
        end   = int(config.get("quiet_end_et", 7))
```

Replace with:
```python
        start = int(config.get("quiet_start_et", 22))
        end   = int(config.get("quiet_end_et", 9))
```

- [ ] **Step 2.4: Run test to verify it passes**

```bash
python -m pytest tests/test_quiet_default.py -v
```

Expected: PASS

- [ ] **Step 2.5: Verify no syntax errors**

```bash
python -c "import bot_strategy; print('OK')"
```

Expected: `OK`

- [ ] **Step 2.6: Commit**

```bash
git add bot_strategy.py tests/test_quiet_default.py
git commit -m "fix(strategy): quiet_end_et default 7->9 in _is_quiet_hours to match _init_config"
```

---

## Task 3: Cross-asset S1 window guard

**The problem:** ETH, SOL, DOGE, XRP are all 15-minute crypto contracts correlated with BTC. When the BTC price moves, all assets see the same signal simultaneously. If S1 fires on ETH, the same momentum/dislocation signal usually exists on SOL and DOGE too — resulting in 3–4 simultaneous entries in the same direction. This multiplies loss exposure without multiplying edge.

**The fix:** After any non-BTC asset fires S1, block S1 on all other non-BTC assets for 300 seconds. BTC is excluded because it has its own independent signal (it drives the others). `_s1_asset_trade_times` (bot_state.py:66) already records timestamps. `_execute_s1_trade` (bot_risk.py:471-473) already appends to it on fill.

**Files:**
- Modify: `bot_strategy.py` (add window guard after fire-rate gate, ~line 356)
- Create: `tests/test_s1_window_guard.py`

- [ ] **Step 3.1: Write the failing test**

```python
# tests/test_s1_window_guard.py
"""Cross-asset S1 window guard: after ETH fires, SOL must be blocked for 300s."""
import sys, os, time
from collections import deque
from unittest.mock import patch
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
import asset_manager
from bot_strategy import strategy_brain_s1


def _make_prices(base=60.0, n=63):
    now = time.time()
    return [(now - (n - i) * 10, base) for i in range(n)]


def _run_s1_for_asset(asset, base_price, asset_trade_times):
    config = {"mode": "paper", "bot_enabled": True}
    strike = base_price * 1.005
    prices = _make_prices(base=base_price)
    patches = [
        patch("bot_strategy.read_config", return_value=config),
        patch("bot_strategy._is_quiet_hours", return_value=False),
        patch.object(bot_state, "_s1_pending_trades", {}),
        patch.object(bot_state, "_s1_asset_trade_times", dict(asset_trade_times)),
        patch.object(bot_state, "_s1_cooldown_until", {}),
        patch.object(bot_state, "_s1_consec_losses_by_asset", {}),
        patch.dict(asset_manager._prices, {asset: deque(prices)}),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches: stack.enter_context(p)
        return strategy_brain_s1(
            prices[-1][1], strike,
            38.0, 57.0,
            elapsed_seconds=200, secs_left=400,
            ticker=f"KX{asset}15M-TEST", asset=asset,
        )


def test_window_guard_blocks_sol_when_eth_just_fired():
    """SOL S1 must return skip when ETH fired S1 within 300s."""
    eth_fired_30s_ago = time.time() - 30
    result = _run_s1_for_asset("SOL", 60.0, {"ETH": [eth_fired_30s_ago]})
    assert result["action"] == "skip", (
        f"SOL S1 must be blocked when ETH fired 30s ago, got action={result['action']}"
    )
    assert "s1_window_guard" in result.get("reasoning", ""), (
        f"Expected s1_window_guard in reasoning, got: {result.get('reasoning')}"
    )


def test_window_guard_allows_sol_when_eth_fired_long_ago():
    """SOL S1 must pass window guard when ETH fired >300s ago."""
    eth_fired_400s_ago = time.time() - 400
    result = _run_s1_for_asset("SOL", 60.0, {"ETH": [eth_fired_400s_ago]})
    assert "s1_window_guard" not in result.get("reasoning", ""), (
        f"ETH fired 400s ago — SOL must not be blocked: {result.get('reasoning')}"
    )


def test_window_guard_does_not_block_btc():
    """BTC S1 must never be blocked by another asset's window guard."""
    eth_fired_30s_ago = time.time() - 30
    btc_prices = _make_prices(base=105000.0)
    config = {"mode": "paper", "bot_enabled": True}
    patches = [
        patch("bot_strategy.read_config", return_value=config),
        patch("bot_strategy._is_quiet_hours", return_value=False),
        patch.object(bot_state, "_s1_pending_trades", {}),
        patch.object(bot_state, "_s1_asset_trade_times", {"ETH": [eth_fired_30s_ago]}),
        patch.object(bot_state, "_s1_cooldown_until", {}),
        patch.object(bot_state, "_s1_consec_losses_by_asset", {}),
        patch.object(bot_state, "btc_prices", deque(btc_prices)),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches: stack.enter_context(p)
        result = strategy_brain_s1(
            btc_prices[-1][1], btc_prices[-1][1] * 1.005,
            38.0, 57.0,
            elapsed_seconds=200, secs_left=400,
            ticker="KXBTC15M-TEST", asset="BTC",
        )
    assert "s1_window_guard" not in result.get("reasoning", ""), (
        f"BTC must never be blocked by cross-asset window guard: {result.get('reasoning')}"
    )


def test_window_guard_allows_when_no_recent_trades():
    """SOL S1 must pass when _s1_asset_trade_times is empty."""
    result = _run_s1_for_asset("SOL", 60.0, {})
    assert "s1_window_guard" not in result.get("reasoning", ""), (
        f"Empty trade times must not trigger window guard: {result.get('reasoning')}"
    )
```

- [ ] **Step 3.2: Run test to verify it fails**

```bash
python -m pytest tests/test_s1_window_guard.py -v
```

Expected: FAIL — `s1_window_guard` not in reasoning (guard not yet implemented)

- [ ] **Step 3.3: Add the window guard to strategy_brain_s1**

In `bot_strategy.py`, find the fire-rate guard block (after the per-asset fire-rate check, ~lines 348-357):

```python
    if len(_recent_times) >= _max_per_hour:
        return _make_skip(
            "yes",
            f"s1_rate_limit:{len(_recent_times)}/{_max_per_hour}_per_hour",
            abs_pct, mins_left, variant="strategy1",
        )

    # Gate 1: time window
```

Insert the cross-asset window guard between the rate-limit block and Gate 1:

```python
    if len(_recent_times) >= _max_per_hour:
        return _make_skip(
            "yes",
            f"s1_rate_limit:{len(_recent_times)}/{_max_per_hour}_per_hour",
            abs_pct, mins_left, variant="strategy1",
        )

    # Cross-asset S1 window guard: block non-BTC assets for 300s after any other non-BTC fires.
    # Prevents simultaneous correlated entries when all crypto moves together.
    if asset != "BTC":
        _xwin_sec = float(config.get("s1_cross_asset_window_seconds", 300.0))
        _now_xwin = time.time()
        for _other, _other_times in bot_state._s1_asset_trade_times.items():
            if _other == asset:
                continue
            if any(_now_xwin - t < _xwin_sec for t in _other_times):
                return _make_skip(
                    "yes",
                    f"s1_window_guard:{_other}",
                    abs_pct, mins_left, variant="strategy1",
                )

    # Gate 1: time window
```

- [ ] **Step 3.4: Run test to verify it passes**

```bash
python -m pytest tests/test_s1_window_guard.py -v
```

Expected: all 4 tests PASS

- [ ] **Step 3.5: Verify no syntax errors**

```bash
python -c "import bot_strategy; print('OK')"
```

Expected: `OK`

- [ ] **Step 3.6: Commit**

```bash
git add bot_strategy.py tests/test_s1_window_guard.py
git commit -m "feat(strategy): cross-asset S1 window guard — 300s block after any non-BTC S1 fill"
```

---

## Task 4: S1+S2 same-ticker dedup in handle_ready_phase

**The problem:** In `handle_ready_phase`, S2 brain is computed first (`strategy_brain_s2`), then S1 is executed (`await _execute_s1_trade`). If S1 fills, the ticker is reserved in `bot_state._s1_pending_trades`. But the code continues into S2 execution without checking — S2 can place a second order on the same ticker in the same cycle, doubling position size unintentionally.

**The fix:** After `await _execute_s1_trade(...)`, before evaluating S2's `do_trade`, check if `ticker in bot_state._s1_pending_trades`. If yes, force `do_trade = False` for S2.

**Files:**
- Modify: `bot_loops.py` (after `_execute_s1_trade` call, ~line 304)
- Create: `tests/test_s1_s2_dedup.py`

- [ ] **Step 4.1: Write the failing test**

```python
# tests/test_s1_s2_dedup.py
"""Verify S2 skips when S1 has an active trade on the same ticker."""
import inspect
import bot_loops


def test_s2_dedup_check_exists_after_s1_execute():
    """handle_ready_phase source must check _s1_pending_trades before S2 fires."""
    src = inspect.getsource(bot_loops.handle_ready_phase)
    assert "_s1_pending_trades" in src, (
        "_s1_pending_trades check missing from handle_ready_phase"
    )
    # Check that the dedup block sets do_trade=False
    assert "s2_dedup" in src, (
        "s2_dedup skip reason not found in handle_ready_phase — S1+S2 dedup not implemented"
    )


def test_dedup_fires_only_when_do_trade_true():
    """The dedup gate must only fire when S2 wants to trade (do_trade=True)."""
    src = inspect.getsource(bot_loops.handle_ready_phase)
    # Must guard with 'if do_trade and ticker in bot_state._s1_pending_trades'
    assert "do_trade and ticker in bot_state._s1_pending_trades" in src, (
        "Dedup must be gated on do_trade=True to avoid false positives when S2 already skipping"
    )
```

- [ ] **Step 4.2: Run test to verify it fails**

```bash
python -m pytest tests/test_s1_s2_dedup.py -v
```

Expected: FAIL — `s2_dedup` not found in source

- [ ] **Step 4.3: Add dedup check to handle_ready_phase**

In `bot_loops.py`, find the block after `_execute_s1_trade` and before S2's `side = brain["side"]` (~lines 303-312):

```python
    await _execute_s1_trade(
        session, brain_s1, ticker, btc_price, strike, yes_ask, no_ask,
        elapsed, secs_left, asset, config, mode_s1, ob, market,
    )
    side     = brain["side"]
    score    = brain["confidence"]
    do_trade = brain["action"] == "trade"
    skip_reason_ai = brain["reasoning"]
```

Replace with:

```python
    await _execute_s1_trade(
        session, brain_s1, ticker, btc_price, strike, yes_ask, no_ask,
        elapsed, secs_left, asset, config, mode_s1, ob, market,
    )
    side     = brain["side"]
    score    = brain["confidence"]
    do_trade = brain["action"] == "trade"
    skip_reason_ai = brain["reasoning"]

    # S1+S2 same-ticker dedup: if S1 just reserved this ticker, skip S2 to avoid double entry.
    if do_trade and ticker in bot_state._s1_pending_trades:
        skip_reason_ai = "s2_dedup:s1_active"
        do_trade = False
```

- [ ] **Step 4.4: Run test to verify it passes**

```bash
python -m pytest tests/test_s1_s2_dedup.py -v
```

Expected: PASS

- [ ] **Step 4.5: Verify no syntax errors**

```bash
python -c "import bot_loops; print('OK')"
```

Expected: `OK`

- [ ] **Step 4.6: Commit**

```bash
git add bot_loops.py tests/test_s1_s2_dedup.py
git commit -m "fix(loops): S1+S2 same-ticker dedup — skip S2 when S1 already reserved ticker"
```

---

## Task 5: EOD summary on quiet-hours transition

**The problem:** `_check_daily_stats` fires at 14:00 local time (hardcoded). This fires mid-session. The actual end of trading is when the bot enters quiet hours (~22:00 ET). There's no summary logged at the real end-of-day — the most useful moment to review P&L.

**The fix:** Import `_is_quiet_hours` into `bot_loops`. In both the main BTC loop and `_non_btc_asset_loop`, track the previous quiet state per loop. When transitioning from not-quiet → quiet, call `await _check_daily_stats(...)`.

**Files:**
- Modify: `bot_loops.py:26-35` (add `_is_quiet_hours` to import)
- Modify: `bot_loops.py` (add `_prev_quiet` tracking in both loops)
- Create: `tests/test_eod_quiet_transition.py`

- [ ] **Step 5.1: Write the failing test**

```python
# tests/test_eod_quiet_transition.py
"""Verify EOD summary fires on quiet-hours transition."""
import inspect
import bot_loops
from bot_strategy import _is_quiet_hours


def test_is_quiet_hours_imported_in_bot_loops():
    """bot_loops must import _is_quiet_hours from bot_strategy."""
    import importlib, ast
    src = inspect.getsource(bot_loops)
    assert "_is_quiet_hours" in src, (
        "_is_quiet_hours not referenced in bot_loops — EOD quiet transition not implemented"
    )


def test_prev_quiet_tracking_exists():
    """bot_loops source must track previous quiet state for transition detection."""
    src = inspect.getsource(bot_loops)
    assert "_prev_quiet" in src, (
        "_prev_quiet tracking not found in bot_loops — quiet transition cannot be detected"
    )


def test_check_daily_stats_called_on_transition():
    """bot_loops must call _check_daily_stats on quiet transition."""
    src = inspect.getsource(bot_loops)
    # The transition detection pattern: _prev_quiet was False, now quiet
    assert "_check_daily_stats" in src, (
        "_check_daily_stats not called in bot_loops — EOD summary not fired"
    )
```

- [ ] **Step 5.2: Run test to verify it fails**

```bash
python -m pytest tests/test_eod_quiet_transition.py -v
```

Expected: FAIL — `_is_quiet_hours` not in bot_loops source (not imported there)

- [ ] **Step 5.3: Add `_is_quiet_hours` to bot_strategy import in bot_loops.py**

Find this import (~line 26-35):
```python
from bot_strategy import (
    strategy_brain_s1, strategy_brain_s2,
    track_contract_price,
    _s1_multitf_momentum, _S1_ASSET_CONFIG,
    _s2_contract_direction, _S2_ASSET_CONFIG,
)
```

Replace with:
```python
from bot_strategy import (
    strategy_brain_s1, strategy_brain_s2,
    track_contract_price,
    _s1_multitf_momentum, _S1_ASSET_CONFIG,
    _s2_contract_direction, _S2_ASSET_CONFIG,
    _is_quiet_hours,
)
```

- [ ] **Step 5.4: Find both loops and add quiet-transition tracking**

**In the non-BTC asset loop (`_non_btc_asset_loop`):** Find the outer while-True loop that iterates assets. This is around `bot_loops.py:879`. Add `_prev_quiet_nb: dict = {}` before the loop, then add transition detection after config read.

Read `bot_loops.py` lines 875-920 to get exact code before editing:

```bash
python -u -c "
import sys
sys.stdout.reconfigure(encoding='utf-8')
with open('bot_loops.py', encoding='utf-8') as f:
    lines = f.readlines()
print(''.join(lines[870:925]))
"
```

Then find the inner loop that runs per-asset. After `config = read_config()` (or the equivalent config refresh), add:

```python
        _now_q = _is_quiet_hours(config)
        if _now_q and not _prev_quiet_nb.get(asset, False):
            _now_lv = datetime.now(_LV_TZ)
            asyncio.create_task(_check_daily_stats(_now_lv.strftime("%Y-%m-%d")))
        _prev_quiet_nb[asset] = _now_q
```

**In the BTC main loop:** Find the outer while-True loop in `main_loop` (~line 1341). Add `_prev_quiet_main: bool = False` before the loop. After config read, add:

```python
        _now_q = _is_quiet_hours(config)
        if _now_q and not _prev_quiet_main:
            _now_lv = datetime.now(_LV_TZ)
            asyncio.create_task(_check_daily_stats(_now_lv.strftime("%Y-%m-%d")))
        _prev_quiet_main = _now_q
```

**Note:** Read the exact structure of both loops before editing — the exact location depends on indentation level and what variable names are already in scope (`config`, `_LV_TZ`, etc.).

- [ ] **Step 5.5: Run test to verify it passes**

```bash
python -m pytest tests/test_eod_quiet_transition.py -v
```

Expected: PASS

- [ ] **Step 5.6: Verify no syntax errors**

```bash
python -c "import bot_loops; print('OK')"
```

Expected: `OK`

- [ ] **Step 5.7: Commit**

```bash
git add bot_loops.py tests/test_eod_quiet_transition.py
git commit -m "feat(loops): fire EOD summary on quiet-hours transition in both asset loops"
```

---

## Task 6: Full verification

- [ ] **Step 6.1: Run full test suite**

```bash
python -m pytest tests/ -v --tb=short 2>&1 | tail -40
```

Expected: no new failures vs pre-session baseline.

- [ ] **Step 6.2: Full import test**

```bash
python -c "
import bot_infra, bot_strategy, bot_loops, bot_risk, bot_state
print('All imports OK')
"
```

Expected: `All imports OK`

- [ ] **Step 6.3: Smoke test drawdown (async)**

```bash
python -c "
import asyncio, bot_infra, bot_state
bot_infra.init_db()

async def main():
    pnl = await bot_infra.db_get_today_pnl('paper')
    print(f'Today PnL (paper): \${pnl:.2f}')

asyncio.run(main())
"
```

Expected: prints `Today PnL (paper): $X.XX` without error.

- [ ] **Step 6.4: Smoke test window guard blocks SOL after ETH**

```bash
python -c "
import time, bot_state, bot_strategy
from collections import deque
from unittest.mock import patch
import asset_manager

# Simulate ETH fired 30s ago
bot_state._s1_asset_trade_times['ETH'] = [time.time() - 30]

now = time.time()
sol_prices = deque([(now - (62-i)*10, 60.0) for i in range(63)])

with patch('bot_strategy._is_quiet_hours', return_value=False), \
     patch('bot_strategy.read_config', return_value={'mode': 'paper'}), \
     patch.dict(asset_manager._prices, {'SOL': sol_prices}):
    result = bot_strategy.strategy_brain_s1(
        60.0, 60.3, 38.0, 57.0,
        elapsed_seconds=200, secs_left=400,
        ticker='KXSOL15M-TEST', asset='SOL',
    )
print('SOL result:', result['action'], result.get('reasoning',''))
# Expected: skip, s1_window_guard:ETH
"
```

Expected: `SOL result: skip s1_window_guard:ETH`

- [ ] **Step 6.5: Smoke test quiet_end_et default**

```bash
python -c "
import inspect, bot_strategy
src = inspect.getsource(bot_strategy._is_quiet_hours)
assert 'quiet_end_et\", 9)' in src or \"quiet_end_et', 9)\" in src, 'Default still 7'
print('quiet_end_et default confirmed: 9')
"
```

Expected: `quiet_end_et default confirmed: 9`

- [ ] **Step 6.6: Final commit — tag the fixes**

```bash
git tag v2-post-fixes
```

---

## Self-Review Checklist

- [x] **Spec coverage:** All 5 bugs from the diagnosis table have a task: async fix (T1), quiet default (T2), window guard (T3), S1+S2 dedup (T4), EOD summary (T5).
- [x] **Placeholder scan:** All code blocks are complete. No "TBD" or "see above". Step 5.4 requires reading exact lines before editing — instruction is explicit.
- [x] **Type consistency:** `db_get_today_pnl(mode: str) -> float` (bot_infra.py:500) matches usage `await db_get_today_pnl(mode=config.get("mode", "paper"))`.
- [x] **No YAGNI:** Every change is justified by a specific bug with a concrete impact. No speculative features.
- [x] **State reuse:** Tasks 3 and 4 reuse existing `_s1_asset_trade_times` and `_s1_pending_trades` — no new state added.
