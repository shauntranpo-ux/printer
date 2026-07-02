# Window Guard, S1+S2 Dedup, EOD Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop correlated triple-asset fires, stop S1+S2 double-entering the same market, and send a full trading summary notification when quiet hours begin each evening.

**Architecture:** Three surgical additions. Window guard adds a `float` timestamp to `bot_state` and checks it in `strategy_brain_s1` + records it in `_execute_s1_trade`. S1+S2 dedup adds 3 lines in `handle_ready_phase`. EOD summary adds `_prev_quiet` tracking in both loops and imports `_is_quiet_hours` into `bot_loops.py`.

**Tech Stack:** Python 3.11, asyncio, pytest. `_check_daily_stats` (already exists, already guarded by `_last_stats_date`) handles the notification content — no new formatting needed.

---

## Files changed

| File | What |
|------|------|
| `bot_state.py:97` | Add `_s1_window_fired: float = 0.0` after `_s1_cooldown_until` |
| `bot_strategy.py:338` | Add window gate after per-asset cap skip |
| `bot_risk.py:474` | Record `_s1_window_fired` after fill |
| `bot_loops.py:27` | Add `_is_quiet_hours` to bot_strategy imports |
| `bot_loops.py:298` | Add S1+S2 dedup check after `skip_reason_ai` line |
| `bot_loops.py:1118` | Add `_prev_quiet_main = False` before main loop's `while True` |
| `bot_loops.py:1141` | Add EOD quiet-transition check after config read in main loop |
| `bot_loops.py:880` | Add `_prev_quiet_nb = False` before non-BTC loop's `while True` |
| `bot_loops.py:884` | Add EOD quiet-transition check after `config = read_config()` |
| `tests/test_s1_window_guard.py` | New — 5 tests |
| `tests/test_risk_dedup.py` | Append 1 test |
| `tests/test_eod_summary.py` | New — 3 tests |

---

## Task 1: Cross-asset S1 window guard

**Files:**
- Modify: `bot_state.py` (after line 97)
- Modify: `bot_strategy.py` (after line 337)
- Modify: `bot_risk.py` (after line 473)
- Create: `tests/test_s1_window_guard.py`

**Background:** `_non_btc_asset_loop` processes ETH → SOL → XRP sequentially. Each call to `_process_asset` fully completes (including the `await place_order` inside `_execute_s1_trade`) before the next asset starts. So: ETH fills, sets `_s1_window_fired`, SOL evaluates, sees `_s1_window_fired` is recent, skips. No async races.

- [ ] **Step 1: Write failing tests**

Create `tests/test_s1_window_guard.py`:

```python
"""Tests: cross-asset S1 window guard blocks second non-BTC fire within 5 minutes."""
import sys, os, time, inspect
from collections import deque
from unittest.mock import patch
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
import asset_manager
from bot_strategy import strategy_brain_s1


def _make_flat_prices(base=1600.0):
    now = time.time()
    return [(now - (620 - i) * 10, base) for i in range(63)]


def _run_s1(asset, prices, window_fired_ts):
    config = {"mode": "paper", "bot_enabled": True}
    patches = [
        patch("bot_strategy.read_config", return_value=config),
        patch("bot_strategy._is_quiet_hours", return_value=False),
        patch.object(bot_state, "_s1_pending_trades", {}),
        patch.object(bot_state, "_s1_asset_trade_times", {}),
        patch.object(bot_state, "_s1_window_fired", window_fired_ts),
        patch.object(bot_state, "_s1_cooldown_until", {}),
        patch.object(bot_state, "_s1_consec_losses_by_asset", {}),
        patch.dict(asset_manager._prices, {asset: deque(prices)}),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return strategy_brain_s1(
            prices[-1][1], prices[-1][1] * 1.005,
            38.0, 57.0,
            elapsed_seconds=200, secs_left=400,
            ticker=f"KX{asset}15M-TEST", asset=asset,
        )


def test_window_guard_blocks_second_asset_within_5min():
    """After ETH fires 30s ago, SOL S1 must be blocked."""
    prices = _make_flat_prices(base=60.0)
    result = _run_s1("SOL", prices, window_fired_ts=time.time() - 30)
    assert result["action"] == "skip", (
        f"Expected skip when window fired 30s ago, got action={result['action']}"
    )
    assert "s1_window_gate" in result.get("reasoning", ""), (
        f"Expected s1_window_gate in reasoning: {result.get('reasoning')}"
    )


def test_window_guard_allows_after_5min():
    """After 5+ minutes, next non-BTC asset may fire."""
    prices = _make_flat_prices(base=60.0)
    result = _run_s1("SOL", prices, window_fired_ts=time.time() - 400)
    assert "s1_window_gate" not in result.get("reasoning", ""), (
        f"Should not be blocked after 400s: {result.get('reasoning')}"
    )


def test_window_guard_no_block_when_never_fired():
    """With _s1_window_fired=0.0 (default), no asset is blocked."""
    prices = _make_flat_prices(base=60.0)
    result = _run_s1("SOL", prices, window_fired_ts=0.0)
    assert "s1_window_gate" not in result.get("reasoning", ""), (
        f"Default state (0.0) must not block: {result.get('reasoning')}"
    )


def test_window_guard_never_blocks_btc():
    """Window guard must not apply to BTC regardless of _s1_window_fired."""
    btc_prices = _make_flat_prices(base=60000.0)
    config = {"mode": "paper", "bot_enabled": True}
    with patch("bot_strategy.read_config", return_value=config), \
         patch("bot_strategy._is_quiet_hours", return_value=False), \
         patch.object(bot_state, "_s1_pending_trades", {}), \
         patch.object(bot_state, "_s1_asset_trade_times", {}), \
         patch.object(bot_state, "_s1_window_fired", time.time() - 30), \
         patch.object(bot_state, "_s1_cooldown_until", {}), \
         patch.object(bot_state, "_s1_consec_losses_by_asset", {}), \
         patch.object(bot_state, "btc_prices", deque(btc_prices)):
        result = strategy_brain_s1(
            btc_prices[-1][1], btc_prices[-1][1] * 1.005,
            38.0, 57.0,
            elapsed_seconds=200, secs_left=400,
            ticker="KXBTC15M-TEST", asset="BTC",
        )
    assert "s1_window_gate" not in result.get("reasoning", ""), (
        f"BTC must never be blocked by window gate: {result.get('reasoning')}"
    )


def test_window_fired_set_in_execute_s1_trade():
    """_execute_s1_trade source must update _s1_window_fired for non-BTC assets."""
    from bot_risk import _execute_s1_trade
    src = inspect.getsource(_execute_s1_trade)
    assert "_s1_window_fired" in src, (
        "_s1_window_fired not set in _execute_s1_trade — window guard won't reset after fill"
    )
```

- [ ] **Step 2: Run to confirm they fail**

```
cd C:\Users\alxnt\kalshi-bot && python -m pytest tests/test_s1_window_guard.py -v
```

Expected: 5 failures — `_s1_window_fired` does not exist yet.

- [ ] **Step 3: Add `_s1_window_fired` to bot_state.py**

In `bot_state.py`, find:
```python
_s1_cooldown_until: dict = {}          # asset → epoch timestamp when cooldown expires
```

Add immediately after:
```python
_s1_window_fired: float = 0.0          # epoch time of last non-BTC S1 fill (cross-asset window guard)
```

Also add `"_s1_window_fired"` to the `__all__` list (find the tuple/list containing `"_s1_cooldown_until"` and append alongside it).

- [ ] **Step 4: Add window gate to strategy_brain_s1 in bot_strategy.py**

Find lines 336-337:
```python
    if _s1_asset_count >= _s1_asset_cap:
        return _make_skip("yes", "s1_cap_asset", abs_pct, mins_left, variant="strategy1")
```

Add immediately after:
```python
    # Cross-asset window guard: block non-BTC assets within N seconds of any non-BTC S1 fill.
    # Sequential loop guarantees ETH fills and sets the timestamp before SOL evaluates.
    if asset != "BTC":
        _window_gap = time.time() - bot_state._s1_window_fired
        _window_secs = float(config.get("s1_window_guard_secs", 300))
        if 0 < _window_gap < _window_secs:
            return _make_skip(
                "yes", f"s1_window_gate:{_window_gap:.0f}s_ago",
                abs_pct, mins_left, variant="strategy1",
            )
```

- [ ] **Step 5: Record window fire in bot_risk.py after fill**

In `bot_risk.py`, find lines 471-473:
```python
    # Record fill time for per-asset fire rate guard
    if asset not in bot_state._s1_asset_trade_times:
        bot_state._s1_asset_trade_times[asset] = []
    bot_state._s1_asset_trade_times[asset].append(time.time())
```

Add immediately after:
```python
    # Record window fire time for cross-asset guard (non-BTC only)
    if asset != "BTC":
        bot_state._s1_window_fired = time.time()
```

- [ ] **Step 6: Run tests — confirm all 5 pass**

```
cd C:\Users\alxnt\kalshi-bot && python -m pytest tests/test_s1_window_guard.py -v
```

- [ ] **Step 7: Run full suite**

```
cd C:\Users\alxnt\kalshi-bot && python -m pytest --tb=short -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add bot_state.py bot_strategy.py bot_risk.py tests/test_s1_window_guard.py
git commit -m "fix(strategy): cross-asset S1 window guard — block 2nd non-BTC fire within 5min"
```

---

## Task 2: S1+S2 same-market dedup

**Files:**
- Modify: `bot_loops.py:298-300` (handle_ready_phase)
- Modify: `tests/test_risk_dedup.py` (append 1 test)

**Background:** `_execute_s1_trade` inserts `ticker` into `bot_state._s1_pending_trades` *before* awaiting `place_order` (existing race-condition-safe reservation). By the time the dedup check runs (same synchronous code block, no yield between), the slot is already claimed. Current code at lines 296-300:

```python
    side     = brain["side"]
    score    = brain["confidence"]
    do_trade = brain["action"] == "trade"
    skip_reason_ai = brain["reasoning"]
```

- [ ] **Step 1: Write failing test**

Append to `tests/test_risk_dedup.py`:

```python
def test_s2_skipped_when_s1_already_entered():
    """handle_ready_phase must check _s1_pending_trades before executing S2."""
    import inspect
    from bot_loops import handle_ready_phase
    src = inspect.getsource(handle_ready_phase)
    assert "_s1_pending_trades" in src, (
        "_s1_pending_trades not referenced in handle_ready_phase — S1/S2 dedup missing"
    )
    assert "s2_dedup" in src, (
        "s2_dedup skip reason not found in handle_ready_phase"
    )
```

- [ ] **Step 2: Run to confirm it fails**

```
cd C:\Users\alxnt\kalshi-bot && python -m pytest tests/test_risk_dedup.py::test_s2_skipped_when_s1_already_entered -v
```

Expected: FAIL — `s2_dedup not found in handle_ready_phase`.

- [ ] **Step 3: Add dedup check to bot_loops.py**

In `bot_loops.py`, find this exact block (around line 296-300):
```python
    side     = brain["side"]
    score    = brain["confidence"]
    do_trade = brain["action"] == "trade"
    skip_reason_ai = brain["reasoning"]
```

Add immediately after `skip_reason_ai = brain["reasoning"]`:
```python
    # S1/S2 dedup: if S1 just reserved this ticker, skip S2 on the same market.
    if do_trade and ticker in bot_state._s1_pending_trades:
        do_trade = False
        skip_reason_ai = "s2_dedup:s1_entered_same_market"
```

- [ ] **Step 4: Run tests — confirm they pass**

```
cd C:\Users\alxnt\kalshi-bot && python -m pytest tests/test_risk_dedup.py -v
```

- [ ] **Step 5: Run full suite**

```
cd C:\Users\alxnt\kalshi-bot && python -m pytest --tb=short -q
```

- [ ] **Step 6: Commit**

```bash
git add bot_loops.py tests/test_risk_dedup.py
git commit -m "fix(loops): block S2 from entering market already reserved by S1"
```

---

## Task 3: End-of-session summary notification

**Files:**
- Modify: `bot_loops.py:27-32` (add `_is_quiet_hours` to imports)
- Modify: `bot_loops.py:~1117-1141` (main_loop: `_prev_quiet_main` + EOD check)
- Modify: `bot_loops.py:~879-885` (_non_btc_asset_loop: `_prev_quiet_nb` + EOD check)
- Create: `tests/test_eod_summary.py`

**Background:** `_check_daily_stats(today)` is the existing function that sends the full `bot_stats.format_telegram` notification. It is already guarded by `_last_stats_date` (idempotent per calendar date). Both loops detecting the same transition is safe — the second call is a no-op. `_is_quiet_hours` is in `bot_strategy.py` and is not currently imported in `bot_loops.py`.

- [ ] **Step 1: Write failing tests**

Create `tests/test_eod_summary.py`:

```python
"""Tests: EOD session summary fires when quiet hours begin."""
import sys, os, asyncio
from unittest.mock import patch, AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_loops


def test_is_quiet_hours_importable_from_bot_loops():
    """bot_loops must import _is_quiet_hours from bot_strategy."""
    import inspect
    src = inspect.getsource(bot_loops)
    assert "_is_quiet_hours" in src, (
        "_is_quiet_hours not imported or referenced in bot_loops — EOD summary cannot fire"
    )


def test_prev_quiet_tracking_in_main_loop():
    """main_loop source must contain _prev_quiet_main tracking logic."""
    import inspect
    src = inspect.getsource(bot_loops.main_loop)
    assert "_prev_quiet_main" in src, (
        "_prev_quiet_main not found in main_loop — quiet hours transition not tracked"
    )
    assert "_is_quiet_hours" in src, (
        "_is_quiet_hours not called in main_loop — EOD transition not detectable"
    )


def test_prev_quiet_tracking_in_non_btc_loop():
    """_non_btc_asset_loop source must contain _prev_quiet_nb tracking logic."""
    import inspect
    src = inspect.getsource(bot_loops._non_btc_asset_loop)
    assert "_prev_quiet_nb" in src, (
        "_prev_quiet_nb not found in _non_btc_asset_loop — quiet transition not tracked"
    )
    assert "_is_quiet_hours" in src, (
        "_is_quiet_hours not called in _non_btc_asset_loop"
    )
```

- [ ] **Step 2: Run to confirm they fail**

```
cd C:\Users\alxnt\kalshi-bot && python -m pytest tests/test_eod_summary.py -v
```

Expected: all 3 fail.

- [ ] **Step 3: Add `_is_quiet_hours` to imports in bot_loops.py**

Find the existing `from bot_strategy import (` block (line 26-31):
```python
from bot_strategy import (
    strategy_brain_s1, strategy_brain_s2,
    track_contract_price,
    _s1_multitf_momentum, _S1_ASSET_CONFIG,
    _s2_contract_direction, _S2_ASSET_CONFIG,
)
```

Add `_is_quiet_hours` to the list:
```python
from bot_strategy import (
    strategy_brain_s1, strategy_brain_s2,
    track_contract_price,
    _s1_multitf_momentum, _S1_ASSET_CONFIG,
    _s2_contract_direction, _S2_ASSET_CONFIG,
    _is_quiet_hours,
)
```

- [ ] **Step 4: Add EOD tracking to main_loop in bot_loops.py**

Find the line `asyncio.create_task(_non_btc_asset_loop(session))` (around line 1116). The next line is `while True:`. Add `_prev_quiet_main = False` between them:

```python
        asyncio.create_task(_non_btc_asset_loop(session))
        _prev_quiet_main = False

        while True:
```

Inside the `while True:`, find the existing `_check_daily_stats` block:
```python
                _now_lv = datetime.now(_LV_TZ)
                if _now_lv.hour == 14:
                    await _check_daily_stats(_now_lv.strftime("%Y-%m-%d"))

                # Fresh config read
                try:
                    config = read_config()
                except Exception as exc:
                    log.error(f"Config read error: {exc}")
                    await asyncio.sleep(10)
                    continue
```

Add the EOD check immediately after the fresh config read block (after the `continue`):
```python
                # Fresh config read
                try:
                    config = read_config()
                except Exception as exc:
                    log.error(f"Config read error: {exc}")
                    await asyncio.sleep(10)
                    continue

                # EOD session-end summary: fire when quiet hours begin each evening.
                _now_quiet_main = _is_quiet_hours(config)
                if _now_quiet_main and not _prev_quiet_main:
                    await _check_daily_stats(datetime.now(_LV_TZ).strftime("%Y-%m-%d"))
                _prev_quiet_main = _now_quiet_main
```

- [ ] **Step 5: Add EOD tracking to _non_btc_asset_loop in bot_loops.py**

Find `async def _non_btc_asset_loop(session: aiohttp.ClientSession) -> None:` (line 876). The function body starts with `while True:`. Add `_prev_quiet_nb = False` between the function docstring and `while True:`:

```python
async def _non_btc_asset_loop(session: aiohttp.ClientSession) -> None:
    """
    Independent 10-second loop processing all non-BTC enabled assets.
    Runs as a background asyncio task alongside main_loop (which handles BTC).
    """
    _prev_quiet_nb = False
    while True:
```

Inside the `while True:`, the first line of the `try:` block is `config = read_config()`. Add the EOD check immediately after that line:

```python
        try:
            config = read_config()
            # EOD session-end summary: fire when quiet hours begin each evening.
            _now_quiet_nb = _is_quiet_hours(config)
            if _now_quiet_nb and not _prev_quiet_nb:
                await _check_daily_stats(datetime.now(_LV_TZ).strftime("%Y-%m-%d"))
            _prev_quiet_nb = _now_quiet_nb
            if not config.get("bot_enabled", False):
```

- [ ] **Step 6: Run tests — confirm all 3 pass**

```
cd C:\Users\alxnt\kalshi-bot && python -m pytest tests/test_eod_summary.py -v
```

- [ ] **Step 7: Run full suite**

```
cd C:\Users\alxnt\kalshi-bot && python -m pytest --tb=short -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add bot_loops.py tests/test_eod_summary.py
git commit -m "feat(notify): send full trading summary when quiet hours begin each evening"
```

---

## Self-Review

**Spec coverage:**
- Window guard (bot_state + strategy + risk): ✅ Task 1
- S1+S2 dedup (bot_loops handle_ready_phase): ✅ Task 2
- EOD summary on quiet-hours transition (both loops): ✅ Task 3
- `_is_quiet_hours` imported into bot_loops: ✅ Task 3 Step 3
- `_check_daily_stats` idempotency note (both loops safe): ✅ documented in Task 3 background
- Configurable window duration `s1_window_guard_secs`: ✅ Task 1 Step 4

**Placeholder scan:** None found. All code blocks complete.

**Type consistency:**
- `_s1_window_fired: float = 0.0` — bot_state.py Task 1 Step 3, gate reads `bot_state._s1_window_fired` Task 1 Step 4, recorder sets `bot_state._s1_window_fired = time.time()` Task 1 Step 5 ✅
- `_prev_quiet_main: bool` — initialized False Task 3 Step 4, updated each tick same step ✅
- `_prev_quiet_nb: bool` — initialized False Task 3 Step 5, updated each tick same step ✅
- `_check_daily_stats(today: str)` — existing function, called with `datetime.now(_LV_TZ).strftime("%Y-%m-%d")` in both locations ✅
