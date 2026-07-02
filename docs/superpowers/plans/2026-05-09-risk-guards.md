# Risk Guards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix four execution/risk bugs: S2 OBI fails open, S1 has no position cap, S1 consecutive-loss counter is never persisted or acted on, and S1 session gate is not configurable per-asset.

**Architecture:** Four independent edits across `bot_strategy.py`, `bot_risk.py`, `bot_loops.py`, and `config.json`. Each fix is tested first (TDD). All tests go in the new file `tests/test_risk_guards.py`. Commit after each fix.

**Tech Stack:** Python 3.11, pytest, aiosqlite, bot_state (shared module-level globals), bot_strategy (strategy brains), bot_risk (trade execution/settlement), bot_loops (main loop + startup recovery).

---

## File Map

| File | Role |
|------|------|
| `bot_strategy.py` | Fix 1 (`_s2_obi_gate`), Fix 2 (S1 cap gates in `strategy_brain_s1`), Fix 4 (session gate) |
| `bot_risk.py` | Fix 3a (`write_state_file` state dict), Fix 3c (`_settle_s1_trade` alert) |
| `bot_loops.py` | Fix 3b (startup recovery restores `_s1_consecutive_losses`) |
| `config.json` | Fix 2: add `max_s1_positions`, `max_s1_positions_per_asset` defaults |
| `tests/test_risk_guards.py` | New test file - 7 tests covering all 4 fixes |

---

### Task 1: Fix 1 - S2 OBI Fail-Closed

**Files:**
- Modify: `bot_strategy.py:386-387`
- Create: `tests/test_risk_guards.py`

**Context:** `_s2_obi_gate` (bot_strategy.py:378-393) currently returns `True, None` when `_ticker_obi[ticker]` is absent - S2 enters without OBI confirmation. Fix: return `False, None` instead.

- [ ] **Step 1: Create test file with two OBI tests**

Create `tests/test_risk_guards.py`:

```python
"""Tests for risk guard fixes."""
import os
import sys
import inspect

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot_state
import bot_strategy
import bot_risk
import bot_loops


def test_s2_obi_gate_fails_closed_on_none():
    """OBI gate must block (not pass) when no OBI data exists for ticker."""
    bot_state._ticker_obi.clear()
    confirmed, val = bot_strategy._s2_obi_gate("TEST-TICK", "yes", 0.20)
    assert not confirmed, "OBI gate should fail-closed when obi_val is None"
    assert val is None


def test_s2_obi_gate_passes_with_data():
    """OBI gate must pass when obi_val exceeds min_obi threshold."""
    bot_state._ticker_obi["TEST-TICK"] = 0.40
    try:
        confirmed, val = bot_strategy._s2_obi_gate("TEST-TICK", "yes", 0.20)
        assert confirmed, f"OBI gate should pass when obi=0.40 > min=0.20; got confirmed={confirmed}"
        assert abs(val - 0.40) < 0.001
    finally:
        bot_state._ticker_obi.pop("TEST-TICK", None)
```

- [ ] **Step 2: Run tests to verify they fail**

```
py -m pytest tests/test_risk_guards.py -v
```

Expected: `test_s2_obi_gate_fails_closed_on_none` FAILS (gate currently returns True), `test_s2_obi_gate_passes_with_data` PASSES.

- [ ] **Step 3: Fix `_s2_obi_gate` in bot_strategy.py:386-387**

Find this block (lines 385-387):
```python
    obi_val = bot_state._ticker_obi.get(ticker)
    if obi_val is None:
        return True, None
```

Replace with:
```python
    obi_val = bot_state._ticker_obi.get(ticker)
    if obi_val is None:
        return False, None
```

- [ ] **Step 4: Run tests to verify both pass**

```
py -m pytest tests/test_risk_guards.py::test_s2_obi_gate_fails_closed_on_none tests/test_risk_guards.py::test_s2_obi_gate_passes_with_data -v
```

Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_risk_guards.py bot_strategy.py
git commit -m "fix: S2 OBI gate fails-closed when obi data absent"
```

---

### Task 2: Fix 2 - S1 Global + Per-Asset Position Cap

**Files:**
- Modify: `bot_strategy.py:156-158` (insert cap gates before Gate 1 session check)
- Modify: `config.json` (add two keys)
- Modify: `tests/test_risk_guards.py` (add 2 tests)

**Context:** `strategy_brain_s1` (bot_strategy.py:130) has no limit on concurrent open positions. `_s1_pending_trades` is a dict keyed by ticker. Cap gates check both global count and per-asset count. Config keys `max_s1_positions` (default 3) and `max_s1_positions_per_asset` (default 1).

Insertion point: **after** line 156 (`abs_pct = ...`), **before** line 158 (`# Gate 1: session`).

- [ ] **Step 1: Add cap tests to `tests/test_risk_guards.py`**

Append to the file:

```python
def test_s1_cap_global_source_check():
    """strategy_brain_s1 must have s1_cap_global skip reason."""
    src = inspect.getsource(bot_strategy.strategy_brain_s1)
    assert "s1_cap_global" in src, "strategy_brain_s1 missing s1_cap_global skip reason"


def test_s1_cap_asset_source_check():
    """strategy_brain_s1 must have s1_cap_asset skip reason."""
    src = inspect.getsource(bot_strategy.strategy_brain_s1)
    assert "s1_cap_asset" in src, "strategy_brain_s1 missing s1_cap_asset skip reason"
```

- [ ] **Step 2: Run to verify they fail**

```
py -m pytest tests/test_risk_guards.py::test_s1_cap_global_source_check tests/test_risk_guards.py::test_s1_cap_asset_source_check -v
```

Expected: both FAIL (`s1_cap_global`/`s1_cap_asset` not in source yet).

- [ ] **Step 3: Add cap gates to `strategy_brain_s1` in `bot_strategy.py`**

Find this exact block (lines 156-158):
```python
    abs_pct = abs(current_price - strike) / strike if strike > 0 else 0.0

    # Gate 1: session (BTC only)
```

Replace with:
```python
    abs_pct = abs(current_price - strike) / strike if strike > 0 else 0.0

    # Cap gate: global S1 position limit
    _s1_global_cap = config.get("max_s1_positions", 3)
    if len(bot_state._s1_pending_trades) >= _s1_global_cap:
        return _make_skip("yes", "s1_cap_global", abs_pct, mins_left, variant="strategy1")

    # Cap gate: per-asset S1 position limit
    _s1_asset_cap = config.get("max_s1_positions_per_asset", 1)
    _s1_asset_count = sum(
        1 for t in bot_state._s1_pending_trades.values() if t.get("asset") == asset
    )
    if _s1_asset_count >= _s1_asset_cap:
        return _make_skip("yes", "s1_cap_asset", abs_pct, mins_left, variant="strategy1")

    # Gate 1: session (BTC only)
```

- [ ] **Step 4: Add config defaults to `config.json`**

Read `config.json`, find any existing key (e.g., `"max_consecutive_losses"`), and add alongside it:
```json
"max_s1_positions": 3,
"max_s1_positions_per_asset": 1,
```

- [ ] **Step 5: Run tests to verify they pass**

```
py -m pytest tests/test_risk_guards.py::test_s1_cap_global_source_check tests/test_risk_guards.py::test_s1_cap_asset_source_check -v
```

Expected: both PASS.

- [ ] **Step 6: Commit**

```bash
git add bot_strategy.py config.json tests/test_risk_guards.py
git commit -m "fix: add S1 global and per-asset position caps"
```

---

### Task 3: Fix 3 - S1 Consecutive-Loss Persistence + Alert

**Files:**
- Modify: `bot_risk.py:209` (`write_state_file` state dict - add `s1_consecutive_losses` key)
- Modify: `bot_risk.py:508-511` (`_settle_s1_trade` - add alert after threshold crossed)
- Modify: `bot_loops.py:963-965` (startup recovery - restore `_s1_consecutive_losses`)
- Modify: `tests/test_risk_guards.py` (add 3 tests)

**Context:**
- `write_state_file` (bot_risk.py:183): state dict at line 209 has `"consecutive_losses": bot_state._s2_consecutive_losses` but no S1 key.
- `_settle_s1_trade` (bot_risk.py:462): increments `_s1_consecutive_losses` at line 511 but never alerts.
- Startup recovery (bot_loops.py:963): reads `_saved.get("consecutive_losses", 0)` into `_s2_consecutive_losses` but has no S1 restore.

- [ ] **Step 1: Add 3 persistence tests to `tests/test_risk_guards.py`**

Append to the file:

```python
def test_s1_consecutive_loss_persisted():
    """write_state_file must include s1_consecutive_losses in its state dict."""
    src = inspect.getsource(bot_risk.write_state_file)
    assert "s1_consecutive_losses" in src, \
        "write_state_file not persisting s1_consecutive_losses"


def test_s1_consecutive_loss_restored():
    """bot_loops startup recovery must read s1_consecutive_losses from saved state."""
    import importlib, types
    src = inspect.getsource(bot_loops)
    assert "s1_consecutive_losses" in src, \
        "bot_loops startup not restoring s1_consecutive_losses"


def test_s1_cl_alert_source_check():
    """_settle_s1_trade must check max_consecutive_losses and send alert for S1."""
    src = inspect.getsource(bot_risk._settle_s1_trade)
    assert "max_consecutive_losses" in src or "max_cl" in src, \
        "_settle_s1_trade not checking max_consecutive_losses for S1 alert"
    assert "[S1]" in src and "consecutive losses" in src, \
        "_settle_s1_trade missing [S1] consecutive losses Telegram text"
```

- [ ] **Step 2: Run to verify they fail**

```
py -m pytest tests/test_risk_guards.py::test_s1_consecutive_loss_persisted tests/test_risk_guards.py::test_s1_consecutive_loss_restored tests/test_risk_guards.py::test_s1_cl_alert_source_check -v
```

Expected: all three FAIL.

- [ ] **Step 3: Add `s1_consecutive_losses` to `write_state_file` state dict in `bot_risk.py:209`**

Find this line (bot_risk.py:209):
```python
        "consecutive_losses": bot_state._s2_consecutive_losses,
```

Replace with:
```python
        "consecutive_losses": bot_state._s2_consecutive_losses,
        "s1_consecutive_losses": bot_state._s1_consecutive_losses,
```

- [ ] **Step 4: Add S1 alert after `_s1_consecutive_losses` increment in `bot_risk.py:508-511`**

Find this block (bot_risk.py:508-511):
```python
    if outcome == "win":
        bot_state._s1_consecutive_losses = 0
    else:
        bot_state._s1_consecutive_losses += 1
```

Replace with:
```python
    if outcome == "win":
        bot_state._s1_consecutive_losses = 0
    else:
        bot_state._s1_consecutive_losses += 1
        max_cl = config.get("max_consecutive_losses", 5)
        if bot_state._s1_consecutive_losses >= max_cl:
            await send_telegram(
                f"<b>🔵 [S1] {bot_state._s1_consecutive_losses} consecutive losses</b>"
            )
```

- [ ] **Step 5: Add S1 restore to startup recovery in `bot_loops.py:963-965`**

Find this block (bot_loops.py:963-965):
```python
        saved_cl = _saved.get("consecutive_losses", 0)
        if isinstance(saved_cl, int) and saved_cl > 0:
            bot_state._s2_consecutive_losses = saved_cl
```

Replace with:
```python
        saved_cl = _saved.get("consecutive_losses", 0)
        if isinstance(saved_cl, int) and saved_cl > 0:
            bot_state._s2_consecutive_losses = saved_cl
        saved_cl_s1 = _saved.get("s1_consecutive_losses", 0)
        if isinstance(saved_cl_s1, int) and saved_cl_s1 > 0:
            bot_state._s1_consecutive_losses = saved_cl_s1
```

- [ ] **Step 6: Run tests to verify all three pass**

```
py -m pytest tests/test_risk_guards.py::test_s1_consecutive_loss_persisted tests/test_risk_guards.py::test_s1_consecutive_loss_restored tests/test_risk_guards.py::test_s1_cl_alert_source_check -v
```

Expected: all three PASS.

- [ ] **Step 7: Commit**

```bash
git add bot_risk.py bot_loops.py tests/test_risk_guards.py
git commit -m "fix: persist and restore S1 consecutive losses; alert on threshold"
```

---

### Task 4: Fix 4 - S1 Session Gate Configurable for Alts

**Files:**
- Modify: `bot_strategy.py:158-160` (replace hardcoded `cfg["session_gate"]` with `get_asset_config` lookup)
- Modify: `tests/test_risk_guards.py` (add 1 test)

**Context:** `strategy_brain_s1` line 158-160:
```python
    # Gate 1: session (BTC only)
    if cfg["session_gate"] and not _s1_is_us_session():
        return _make_skip("yes", "s1_session_gate", abs_pct, mins_left, variant="strategy1")
```
`get_asset_config` is already imported (bot_strategy.py:9). The fix reads `s1_session_gate` from per-asset config in `config.json`, falling back to `cfg["session_gate"]`. Operators enable ETH gating with `"asset_config": {"ETH": {"s1_session_gate": true}}`.

- [ ] **Step 1: Add session gate test to `tests/test_risk_guards.py`**

Append to the file:

```python
def test_s1_session_gate_source_check():
    """strategy_brain_s1 must use get_asset_config for s1_session_gate and _effective_gate."""
    src = inspect.getsource(bot_strategy.strategy_brain_s1)
    assert "s1_session_gate" in src, \
        "strategy_brain_s1 not using s1_session_gate config key"
    assert "_effective_gate" in src, \
        "strategy_brain_s1 not using _effective_gate variable"
```

- [ ] **Step 2: Run to verify it fails**

```
py -m pytest tests/test_risk_guards.py::test_s1_session_gate_source_check -v
```

Expected: FAIL (`s1_session_gate` not in source yet).

- [ ] **Step 3: Fix session gate check in `bot_strategy.py:158-160`**

Find this block (after the cap gates added in Task 2, at the line with `# Gate 1: session`):
```python
    # Gate 1: session (BTC only)
    if cfg["session_gate"] and not _s1_is_us_session():
        return _make_skip("yes", "s1_session_gate", abs_pct, mins_left, variant="strategy1")
```

Replace with:
```python
    # Gate 1: session (BTC always; alts if s1_session_gate set in per-asset config)
    _effective_gate = get_asset_config(config, asset, "s1_session_gate", cfg["session_gate"])
    if _effective_gate and not _s1_is_us_session():
        return _make_skip("yes", "s1_session_gate", abs_pct, mins_left, variant="strategy1")
```

- [ ] **Step 4: Run test to verify it passes**

```
py -m pytest tests/test_risk_guards.py::test_s1_session_gate_source_check -v
```

Expected: PASS.

- [ ] **Step 5: Run full test suite**

```
py -m pytest tests/test_risk_guards.py tests/test_dual_brain.py -v
```

Expected: all 7 + 14 = 21 tests PASS, 0 failures.

- [ ] **Step 6: Commit**

```bash
git add bot_strategy.py tests/test_risk_guards.py
git commit -m "fix: S1 session gate configurable per-asset via get_asset_config"
```
