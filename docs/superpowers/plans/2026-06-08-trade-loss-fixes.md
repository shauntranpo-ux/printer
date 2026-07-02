# Trade Loss Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the four structural causes of the bot's loss clusters: dislocation trades bypassing the trend filter, correlated triple-asset simultaneous entries, S1+S2 double-entering the same market, and an over-permissive dislocation edge threshold.

**Architecture:** All four fixes are surgical additions to existing gate logic. Fix 1+4 modify the dislocation fast-path in `bot_strategy.py`. Fix 2 adds a cross-asset window guard via a new `bot_state` timestamp. Fix 3 adds a three-line dedup check in `bot_loops.py`. No new files. No new abstractions.

**Tech Stack:** Python 3.11, asyncio, pytest. All state is in `bot_state.py` module-level globals.

---

## Background

**Root cause of the Jun 7 01:43–01:53 4-trade wipeout:**

The dislocation fast-path in `strategy_brain_s1` (`bot_strategy.py:~358`) returns a `"trade"` action before Gate 3 (distance) and before the 10-minute trend gate. When crypto was trending up, the dislocation path fired NO bets on ETH, SOL, and XRP simultaneously — all three lost. The momentum path already has a trend gate; the dislocation path does not.

**Files touched:**
- Modify: `bot_strategy.py` — Fix 1 (dislocation trend gate) + Fix 4 (raise edge threshold)
- Modify: `bot_state.py` — Fix 2 (add `_s1_window_fired` timestamp)
- Modify: `bot_risk.py` — Fix 2 (record window fire after fill)
- Modify: `bot_loops.py` — Fix 3 (S1+S2 dedup)
- Create: `tests/test_disloc_trend_gate.py` — Fix 1 tests
- Create: `tests/test_s1_window_guard.py` — Fix 2 tests
- Modify: `tests/test_risk_dedup.py` — Fix 3 test addition

---

## Task 1: Dislocation trend gate + raise edge threshold (bot_strategy.py)

**Files:**
- Modify: `bot_strategy.py:~358-383` (dislocation fast-path)
- Create: `tests/test_disloc_trend_gate.py`

The dislocation block currently looks like (around line 358):
```python
if _disloc_edge >= cfg.get("min_dislocation_edge", 0.07):
    _min_p = float(get_asset_config(config, asset, "min_entry_price_cents", 20.0))
    _max_p = float(get_asset_config(config, asset, "max_entry_price_cents", 55.0))
    if _min_p <= _disloc_entry_price <= _max_p:
        brain_log.info(
            "S1 DISLOC %s %s | dist=%.4f fair_p=%.3f edge=%.3f ask=%.0fc mins=%.1f",
            asset, ticker, abs_pct, _disloc_fair_p, _disloc_edge, _disloc_entry_price, mins_left,
        )
        return {
            "action": "trade", "side": _disloc_side,
            ...
        }
```

- [ ] **Step 1: Write failing tests**

Create `tests/test_disloc_trend_gate.py`:

```python
"""Tests: dislocation fast-path respects 10-min trend filter and 0.10 edge floor."""
import sys, os, time
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
import asset_manager
from bot_strategy import strategy_brain_s1


def _make_prices(slope_per_sec: float, window_sec: float = 620.0, base: float = 1600.0) -> list:
    now = time.time()
    n = int(window_sec / 10)
    return [(now - (n - i) * 10, base + slope_per_sec * (i * 10)) for i in range(n)]


def _patch_all(asset: str, prices: list, config: dict):
    """Return a stack of patches needed to run strategy_brain_s1 in isolation."""
    from collections import deque
    import contextlib

    @contextlib.contextmanager
    def ctx():
        with patch("bot_strategy.read_config", return_value=config), \
             patch("bot_strategy._is_quiet_hours", return_value=False), \
             patch.object(bot_state, "_s1_pending_trades", {}), \
             patch.object(bot_state, "_s1_asset_trade_times", {}), \
             patch.object(bot_state, "_s1_window_fired", 0.0), \
             patch.dict(asset_manager._prices, {asset: deque(prices)}):
            yield

    return ctx()


def test_disloc_no_bet_blocked_when_uptrend():
    """Dislocation path must skip NO bet when 10-min trend is up."""
    prices = _make_prices(slope_per_sec=0.20, base=1600.0)
    # price is below strike → dislocation would normally bet NO
    current = prices[-1][1]   # ~1612
    strike  = current * 1.0003  # 0.03% above — tiny gap, triggers dislocation
    yes_ask = 55.0
    no_ask  = 40.0             # cheap NO = dislocation edge present

    config = {"mode": "paper", "bot_enabled": True}

    with _patch_all("ETH", prices, config):
        result = strategy_brain_s1(
            current, strike, yes_ask, no_ask,
            elapsed_seconds=200, secs_left=400, ticker="KXETH15M-TEST",
            asset="ETH",
        )

    assert result["action"] == "skip", (
        f"Expected skip for NO bet during uptrend, got action={result['action']} "
        f"reasoning={result.get('reasoning')}"
    )
    assert "disloc_trend_gate" in result.get("reasoning", ""), (
        f"Expected disloc_trend_gate in reasoning, got: {result.get('reasoning')}"
    )


def test_disloc_yes_bet_blocked_when_downtrend():
    """Dislocation path must skip YES bet when 10-min trend is down."""
    prices = _make_prices(slope_per_sec=-0.20, base=1600.0)
    current = prices[-1][1]
    strike  = current * 0.9997  # price is above strike → dislocation would bet YES
    yes_ask = 40.0              # cheap YES = dislocation edge present
    no_ask  = 55.0

    config = {"mode": "paper", "bot_enabled": True}

    with _patch_all("ETH", prices, config):
        result = strategy_brain_s1(
            current, strike, yes_ask, no_ask,
            elapsed_seconds=200, secs_left=400, ticker="KXETH15M-TEST",
            asset="ETH",
        )

    assert result["action"] == "skip", (
        f"Expected skip for YES bet during downtrend, got action={result['action']}"
    )
    assert "disloc_trend_gate" in result.get("reasoning", ""), (
        f"Expected disloc_trend_gate in reasoning, got: {result.get('reasoning')}"
    )


def test_disloc_fires_with_flat_trend_and_sufficient_edge():
    """Dislocation fires when trend is flat and edge >= 0.10."""
    prices = _make_prices(slope_per_sec=0.0, base=1600.0)
    current = prices[-1][1]
    # Put price well below strike: 0.5% gap generates large dislocation edge
    strike = current * 1.005
    yes_ask = 38.0   # cheap YES for dislocation — but current < strike so we bet NO
    no_ask  = 57.0

    config = {"mode": "paper", "bot_enabled": True}

    with _patch_all("ETH", prices, config):
        result = strategy_brain_s1(
            current, strike, yes_ask, no_ask,
            elapsed_seconds=200, secs_left=400, ticker="KXETH15M-TEST",
            asset="ETH",
        )

    # If edge is sufficient (≥0.10) and trend flat, should proceed past dislocation gate.
    # May still be skipped by later gates (time, momentum, etc.) but NOT by trend gate.
    assert "disloc_trend_gate" not in result.get("reasoning", ""), (
        f"Should not be blocked by trend gate when trend is flat: {result.get('reasoning')}"
    )


def test_disloc_edge_threshold_raised_to_010():
    """min_dislocation_edge default must be 0.10, not 0.07."""
    import inspect
    from bot_strategy import strategy_brain_s1 as s1
    src = inspect.getsource(s1)
    assert '"min_dislocation_edge", 0.10' in src or "'min_dislocation_edge', 0.10" in src, (
        "min_dislocation_edge default must be 0.10 — found 0.07 still in source"
    )
```

- [ ] **Step 2: Run tests to confirm they fail**

```
cd C:\Users\alxnt\kalshi-bot
python -m pytest tests/test_disloc_trend_gate.py -v
```

Expected: 3–4 failures (functions not yet implemented / threshold not yet changed).

- [ ] **Step 3: Implement Fix 1 + Fix 4 in bot_strategy.py**

In `bot_strategy.py`, find the dislocation block starting with:
```python
if _disloc_edge >= cfg.get("min_dislocation_edge", 0.07):
```

Replace the entire block with:

```python
if _disloc_edge >= cfg.get("min_dislocation_edge", 0.10):
    _min_p = float(get_asset_config(config, asset, "min_entry_price_cents", 20.0))
    _max_p = float(get_asset_config(config, asset, "max_entry_price_cents", 55.0))
    if _min_p <= _disloc_entry_price <= _max_p:
        _disloc_trend = _trend_direction(prices_list, window_seconds=600.0)
        if _disloc_trend == 1 and _disloc_side == "no":
            return _make_skip(
                _disloc_side, "s1_disloc_trend_gate:no_trend=up",
                abs_pct, mins_left, variant="strategy1",
            )
        if _disloc_trend == -1 and _disloc_side == "yes":
            return _make_skip(
                _disloc_side, "s1_disloc_trend_gate:yes_trend=down",
                abs_pct, mins_left, variant="strategy1",
            )
        brain_log.info(
            "S1 DISLOC %s %s | dist=%.4f fair_p=%.3f edge=%.3f ask=%.0fc mins=%.1f",
            asset, ticker, abs_pct, _disloc_fair_p, _disloc_edge, _disloc_entry_price, mins_left,
        )
        return {
            "action": "trade", "side": _disloc_side,
            "confidence": int(_disloc_fair_p * 100),
            "reasoning": f"s1_dislocation edge={_disloc_edge:.3f} fair_p={_disloc_fair_p:.3f} dist={abs_pct:.3%}",
            "key_signals": [f"disloc_edge:{_disloc_edge:.3f}", f"fair_p:{_disloc_fair_p:.3f}"],
            "signals": {"win_prob": _disloc_fair_p, "ev": _disloc_edge, "abs_pct": abs_pct},
            "win_prob": float(_disloc_fair_p), "mom_label": _disloc_side,
            "mom_pct": abs_pct, "vel_signal": "dislocation",
            "raw_p_yes": float(_disloc_fair_p) if _disloc_side == "yes" else float(1.0 - _disloc_fair_p),
            "mins_left": mins_left, "abs_pct": abs_pct, "above": _disloc_side == "yes",
            "_rv": None, "_vol_ratio": None, "price_filter_skip": False,
            "strategy_variant": "strategy1",
        }
```

- [ ] **Step 4: Run tests — confirm they pass**

```
python -m pytest tests/test_disloc_trend_gate.py -v
```

Expected: all 4 pass.

- [ ] **Step 5: Run full suite — confirm no regressions**

```
python -m pytest --tb=short -q
```

Expected: all tests pass (406+).

- [ ] **Step 6: Commit**

```bash
git add bot_strategy.py tests/test_disloc_trend_gate.py
git commit -m "fix(strategy): add trend gate to dislocation fast-path, raise min_disloc_edge 0.07→0.10"
```

---

## Task 2: Cross-asset per-window S1 fire guard

**Files:**
- Modify: `bot_state.py:~64` (add `_s1_window_fired`)
- Modify: `bot_strategy.py` (add window gate after per-asset cap)
- Modify: `bot_risk.py:~471` (record timestamp after fill)
- Create: `tests/test_s1_window_guard.py`

After any non-BTC S1 fill, block other non-BTC assets from S1 for 5 minutes. Prevents ETH+SOL+XRP from all firing on the same 15-minute window during a correlated crypto move.

- [ ] **Step 1: Write failing tests**

Create `tests/test_s1_window_guard.py`:

```python
"""Tests: cross-asset S1 window guard blocks second non-BTC fire within 5 minutes."""
import sys, os, time, inspect
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
import asset_manager
from bot_strategy import strategy_brain_s1


def _make_prices(slope: float = -0.05, base: float = 1600.0, window: float = 620.0) -> list:
    now = time.time()
    n = int(window / 10)
    return [(now - (n - i) * 10, base + slope * (i * 10)) for i in range(n)]


def test_window_guard_blocks_second_non_btc_asset():
    """After ETH S1 fires, SOL S1 should be blocked for 5 minutes."""
    from collections import deque

    eth_prices = _make_prices(slope=-0.05, base=1600.0)
    sol_prices = _make_prices(slope=-0.05, base=60.0)

    config = {"mode": "paper", "bot_enabled": True}

    with patch("bot_strategy.read_config", return_value=config), \
         patch("bot_strategy._is_quiet_hours", return_value=False), \
         patch.object(bot_state, "_s1_pending_trades", {}), \
         patch.object(bot_state, "_s1_asset_trade_times", {}), \
         patch.object(bot_state, "_s1_window_fired", time.time() - 30), \
         patch.dict(asset_manager._prices, {"SOL": deque(sol_prices)}):

        result = strategy_brain_s1(
            sol_prices[-1][1], sol_prices[-1][1] * 1.003,
            38.0, 57.0,
            elapsed_seconds=200, secs_left=400,
            ticker="KXSOL15M-TEST", asset="SOL",
        )

    assert result["action"] == "skip", (
        f"Expected skip when window fired 30s ago, got action={result['action']}"
    )
    assert "s1_window_gate" in result.get("reasoning", ""), (
        f"Expected s1_window_gate in reasoning, got: {result.get('reasoning')}"
    )


def test_window_guard_allows_after_5_minutes():
    """After 5+ minutes have passed, the next non-BTC asset can fire again."""
    from collections import deque
    sol_prices = _make_prices(slope=-0.05, base=60.0)

    config = {"mode": "paper", "bot_enabled": True}

    with patch("bot_strategy.read_config", return_value=config), \
         patch("bot_strategy._is_quiet_hours", return_value=False), \
         patch.object(bot_state, "_s1_pending_trades", {}), \
         patch.object(bot_state, "_s1_asset_trade_times", {}), \
         patch.object(bot_state, "_s1_window_fired", time.time() - 400), \
         patch.dict(asset_manager._prices, {"SOL": deque(sol_prices)}):

        result = strategy_brain_s1(
            sol_prices[-1][1], sol_prices[-1][1] * 1.003,
            38.0, 57.0,
            elapsed_seconds=200, secs_left=400,
            ticker="KXSOL15M-TEST", asset="SOL",
        )

    assert "s1_window_gate" not in result.get("reasoning", ""), (
        f"Should not be blocked by window gate after 400s: {result.get('reasoning')}"
    )


def test_window_guard_does_not_block_btc():
    """Window guard must never block BTC — only non-BTC assets."""
    config = {"mode": "paper", "bot_enabled": True}

    btc_prices = _make_prices(slope=-0.05, base=60000.0)
    from collections import deque

    with patch("bot_strategy.read_config", return_value=config), \
         patch("bot_strategy._is_quiet_hours", return_value=False), \
         patch.object(bot_state, "_s1_pending_trades", {}), \
         patch.object(bot_state, "_s1_asset_trade_times", {}), \
         patch.object(bot_state, "_s1_window_fired", time.time() - 30), \
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


def test_window_guard_state_exists_in_bot_state():
    """bot_state must export _s1_window_fired as a float."""
    import bot_state as bs
    assert hasattr(bs, "_s1_window_fired"), "_s1_window_fired not found in bot_state"
    assert isinstance(bs._s1_window_fired, float), (
        f"_s1_window_fired must be float, got {type(bs._s1_window_fired)}"
    )


def test_window_fired_recorded_after_fill():
    """_execute_s1_trade source must update _s1_window_fired after fill for non-BTC."""
    import inspect
    from bot_risk import _execute_s1_trade
    src = inspect.getsource(_execute_s1_trade)
    assert "_s1_window_fired" in src, (
        "_s1_window_fired not updated in _execute_s1_trade — window guard won't reset"
    )
```

- [ ] **Step 2: Run tests to confirm they fail**

```
python -m pytest tests/test_s1_window_guard.py -v
```

Expected: all 5 fail.

- [ ] **Step 3: Add `_s1_window_fired` to bot_state.py**

In `bot_state.py`, find the line:
```python
_s1_asset_trade_times: dict = {}  # asset → list of float timestamps of S1 fills
```

Add immediately after:
```python
_s1_window_fired: float = 0.0     # epoch time of last non-BTC S1 fill (cross-asset window guard)
```

- [ ] **Step 4: Add window gate to strategy_brain_s1 in bot_strategy.py**

In `bot_strategy.py`, find the per-asset cap block that ends with:
```python
    if _s1_asset_count >= _s1_asset_cap:
        return _make_skip("yes", "s1_cap_asset", abs_pct, mins_left, variant="strategy1")
```

Add immediately after that block (before the fire-rate guard):
```python
    # Cross-asset window guard: block non-BTC assets within 5 min of any non-BTC S1 fill.
    # Prevents ETH+SOL+XRP from tripling correlated exposure on the same 15-min window.
    if asset != "BTC":
        _window_gap = time.time() - bot_state._s1_window_fired
        if 0 < _window_gap < 300:
            return _make_skip(
                "yes", f"s1_window_gate:{_window_gap:.0f}s_ago",
                abs_pct, mins_left, variant="strategy1",
            )
```

- [ ] **Step 5: Record window fire time in bot_risk.py after fill**

In `bot_risk.py`, find the block at the end of `_execute_s1_trade`:
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

- [ ] **Step 6: Run tests — confirm they pass**

```
python -m pytest tests/test_s1_window_guard.py -v
```

Expected: all 5 pass.

- [ ] **Step 7: Run full suite — confirm no regressions**

```
python -m pytest --tb=short -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add bot_state.py bot_strategy.py bot_risk.py tests/test_s1_window_guard.py
git commit -m "fix(strategy): cross-asset S1 window guard — block 2nd non-BTC fire within 5min"
```

---

## Task 3: S1+S2 same-market dedup

**Files:**
- Modify: `bot_loops.py:~296-300` (handle_ready_phase after _execute_s1_trade)
- Modify: `tests/test_risk_dedup.py` (add one test)

When S1 fills on a ticker, S2 must not also fill on that same ticker.

- [ ] **Step 1: Write failing test**

In `tests/test_risk_dedup.py`, append:

```python
def test_s2_skipped_when_s1_already_entered():
    """handle_ready_phase source must check _s1_pending_trades before S2 execution."""
    import inspect
    from bot_loops import handle_ready_phase
    src = inspect.getsource(handle_ready_phase)
    assert "_s1_pending_trades" in src, (
        "_s1_pending_trades not referenced in handle_ready_phase — S1/S2 dedup missing"
    )
    # Verify the dedup check sets do_trade = False (not just logs)
    assert "s2_dedup" in src, (
        "s2_dedup skip reason not found in handle_ready_phase — dedup not wired"
    )
```

- [ ] **Step 2: Run test to confirm it fails**

```
python -m pytest tests/test_risk_dedup.py::test_s2_skipped_when_s1_already_entered -v
```

Expected: FAIL — "s2_dedup not found in handle_ready_phase".

- [ ] **Step 3: Add dedup check to bot_loops.py**

In `bot_loops.py`, find this block inside `handle_ready_phase`:
```python
    side     = brain["side"]
    score    = brain["confidence"]
    do_trade = brain["action"] == "trade"
    skip_reason_ai = brain["reasoning"]
```

Add immediately after `skip_reason_ai = brain["reasoning"]`:
```python
    # S1/S2 dedup: if S1 just filled on this ticker, don't double-enter with S2.
    if do_trade and ticker in bot_state._s1_pending_trades:
        do_trade = False
        skip_reason_ai = "s2_dedup:s1_entered_same_market"
```

- [ ] **Step 4: Run tests — confirm they pass**

```
python -m pytest tests/test_risk_dedup.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Run full suite — confirm no regressions**

```
python -m pytest --tb=short -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add bot_loops.py tests/test_risk_dedup.py
git commit -m "fix(loops): block S2 from entering market already taken by S1"
```

---

## Self-Review

**Spec coverage check:**
- Fix 1 (dislocation trend gate): ✅ Task 1
- Fix 4 (raise edge threshold 0.07→0.10): ✅ Task 1 Step 3 + test_disloc_edge_threshold_raised_to_010
- Fix 2 (per-window guard): ✅ Task 2
- Fix 3 (S1+S2 dedup): ✅ Task 3

**Placeholder scan:** No TBDs. All code blocks are complete.

**Type consistency:**
- `_s1_window_fired: float = 0.0` — defined Task 2 Step 3, used in Task 2 Step 4 as `bot_state._s1_window_fired` ✅
- `_make_skip` — existing function, unchanged signature ✅
- `_trend_direction(prices_list, window_seconds=600.0)` — existing function at `bot_strategy.py:132` ✅
- `bot_state._s1_pending_trades` — existing dict at `bot_state.py:64` ✅
