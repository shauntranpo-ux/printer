# Profit Maximization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace flat $25 stakes with Kelly-fractional bankroll sizing (compounds $50 into $1000/week in ~7 weeks at 60% WR), add S2 continuation gate to cut bad trades, and unlock evening trading hours.

**Architecture:** Three independent changes — (1) Kelly sizing added to both `_execute_s1_trade` (bot_risk.py) and the S2 handler (bot_loops.py), (2) S2 reversal gate added to `strategy_brain_s2` (bot_strategy.py), (3) quiet hours migration removed from `read_config` and default reset to 22:00 ET. No new files except the test file for Kelly.

**Tech Stack:** Python 3.11, sqlite3, asyncio (existing stack)

---

## Context

**What exists now:**
- S1 (60s momentum + GBM certainty) and S2 (contract velocity + OBI + 3× conviction gate) — both working after the 2026-06-03 rewrite
- Flat `trade_amount_dollars=25` hardcoded in `_execute_s1_trade` (bot_risk.py:399) and `bot_loops.py:435`
- `quiet_start_et` migration at `bot_infra.py:76-77` forces value to 17 (5pm ET) on every config read — killing 5 profitable evening hours
- S2 has no continuation gate: it will trade velocity=YES even when asset price is below strike (reversal trade = low edge)

**Live config.json on Railway:** `quiet_start_et` is likely 17 due to migration. This plan's migration in `_init_config` will write 22 on next deploy.

**Math on Kelly at 60% WR, 45c entry:**
- Net odds b = (100-45)/45 = 1.222
- Full Kelly = (0.60×1.222 − 0.40) / 1.222 = 0.272
- At 25% fractional: trade size = 0.068 × bankroll
- $50 bankroll → $3.40/trade → expected profit $1.05/trade
- Compounding at 16 trades/week: $50 → ~$1000 by week 7

---

## File Structure

| File | Change |
|------|--------|
| `bot_infra.py` | Add `_get_current_bankroll()` (sync sqlite3). Add `bankroll_dollars=50`, `kelly_fraction=0.25` to `_init_config`. Remove 22→17 migration. Write 22 as quiet start on init. |
| `bot_strategy.py` | Add `_kelly_trade_amount()` helper. Add S2 continuation gate after direction determination. |
| `bot_risk.py` | Replace flat stake with Kelly in `_execute_s1_trade` (line 399). |
| `bot_loops.py` | Replace flat stake with Kelly in S2 handler (line 435). |
| `tests/test_kelly_sizing.py` | New: Kelly formula unit tests. |
| `tests/test_s2_fires.py` | Add reversal gate rejection test. |

---

### Task 1: Kelly trade amount helper + config defaults

**Files:**
- Modify: `bot_strategy.py` (add `_kelly_trade_amount` function near top, after `_realized_vol`)
- Modify: `bot_infra.py` (add `_get_current_bankroll`, add config defaults, fix quiet hours)
- Create: `tests/test_kelly_sizing.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_kelly_sizing.py
"""Unit tests for Kelly position sizing helper."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot_strategy import _kelly_trade_amount


def test_kelly_amount_scales_linearly_with_bankroll():
    """Kelly amount is proportional to bankroll — same fraction, 10× bank → 10× trade."""
    small = _kelly_trade_amount(50.0,  0.60, 45.0, 0.25, 1.0)
    large = _kelly_trade_amount(500.0, 0.60, 45.0, 0.25, 1.0)
    assert abs(large - small * 10) < 0.01, f"Expected 10× scaling, got {small:.4f} vs {large:.4f}"


def test_kelly_amount_zero_ev_returns_min_trade():
    """At 50% WR and 50c entry (zero EV), full Kelly = 0 → returns min_trade."""
    amount = _kelly_trade_amount(1000.0, 0.50, 50.0, 0.25, 1.0)
    assert amount == 1.0, f"Expected min_trade=1.0, got {amount}"


def test_kelly_amount_higher_win_prob_larger_stake():
    """Higher win probability → larger Kelly stake (more edge → bet more)."""
    low  = _kelly_trade_amount(100.0, 0.55, 45.0, 0.25, 1.0)
    high = _kelly_trade_amount(100.0, 0.75, 45.0, 0.25, 1.0)
    assert high > low, f"Expected high WR stake > low WR stake: {high:.2f} vs {low:.2f}"


def test_kelly_amount_realistic_range_at_50_bankroll():
    """At 60% WR, 45c entry, $50 bankroll, 25% Kelly → should be $3–$5."""
    amount = _kelly_trade_amount(50.0, 0.60, 45.0, 0.25, 1.0)
    assert 2.0 <= amount <= 6.0, f"Expected $2–6, got ${amount:.2f}"


def test_kelly_amount_never_negative():
    """Kelly fraction is always non-negative even at degenerate inputs."""
    amount = _kelly_trade_amount(100.0, 0.30, 80.0, 0.25, 1.0)
    assert amount >= 1.0, f"Got negative amount: {amount}"


def test_kelly_amount_respects_fraction():
    """Half Kelly fraction → half the trade size."""
    full  = _kelly_trade_amount(100.0, 0.65, 45.0, 1.00, 0.01)
    frac  = _kelly_trade_amount(100.0, 0.65, 45.0, 0.25, 0.01)
    assert abs(frac - full * 0.25) < 0.001, f"Expected 25% of {full:.4f}, got {frac:.4f}"
```

- [ ] **Step 2: Run tests — verify they fail**

```
cd C:\Users\alxnt\kalshi-bot
python -m pytest tests/test_kelly_sizing.py -v
```

Expected: 6 failures — `ImportError: cannot import name '_kelly_trade_amount'`

- [ ] **Step 3: Add `_kelly_trade_amount` to `bot_strategy.py`**

Add immediately after the `_realized_vol` function (around line 55), before `_make_skip`:

```python
def _kelly_trade_amount(
    bankroll: float,
    win_prob: float,
    entry_cents: float,
    kelly_fraction: float,
    min_trade: float,
) -> float:
    """
    Kelly-fractional position sizing.
    b = net odds (profit per dollar risked if win).
    full_kelly = fraction of bankroll that maximises log-wealth growth.
    Returns kelly_fraction * full_kelly * bankroll, floored at min_trade.
    """
    if entry_cents <= 0 or entry_cents >= 100:
        return min_trade
    b = (100.0 - entry_cents) / entry_cents
    q = 1.0 - win_prob
    full_kelly = max(0.0, (win_prob * b - q) / b)
    return max(min_trade, bankroll * kelly_fraction * full_kelly)
```

- [ ] **Step 4: Add `_get_current_bankroll` to `bot_infra.py`**

Add after `db_get_today_pnl` (around line 487), before the Notifications section:

```python
def _get_current_bankroll(mode: str, starting_bankroll: float) -> float:
    """
    Current bankroll = starting_bankroll + all-time realized PnL for mode.
    Uses synchronous sqlite3 — safe to call from async context (single fast read).
    Returns at least $1 to prevent divide-by-zero in Kelly.
    """
    try:
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(bot_state._DB_FILE)
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl_dollars), 0) FROM trades "
            "WHERE mode = ? AND outcome != 'pending'",
            (mode,),
        ).fetchone()
        conn.close()
        total_pnl = float(row[0]) if row else 0.0
        return max(1.0, starting_bankroll + total_pnl)
    except Exception as exc:
        log.warning("_get_current_bankroll error: %s", exc)
        return max(1.0, starting_bankroll)
```

- [ ] **Step 5: Add config defaults to `_init_config` in `bot_infra.py`**

In `_init_config`, add to the `defaults` dict:

```python
defaults = {
    "bot_enabled": False,
    "trade_amount_dollars": 25,
    "bankroll_dollars": 50,       # ADD THIS
    "kelly_fraction": 0.25,       # ADD THIS
    "min_trade_dollars": 1.0,     # ADD THIS
    "mode": "paper",
    ...
}
```

Then after the `for k, v in defaults.items(): cfg.setdefault(k, v)` block, explicitly set quiet hours (overwrites stale 17):

```python
# Reset quiet hours to evening cutoff (was force-migrated to 17 in earlier versions)
cfg["quiet_start_et"] = 22
cfg["quiet_end_et"]   = cfg.get("quiet_end_et", 7)
```

- [ ] **Step 6: Remove the 22→17 migration from `read_config` in `bot_infra.py`**

Remove these lines from `read_config`:

```python
        # Migrate old quiet_start_et values to 5pm ET (2pm PDT)
        if cfg.get("quiet_start_et") in (22, 1):
            cfg["quiet_start_et"] = 17
```

After removal, `read_config` should go directly from `cfg.setdefault(...)` to `bot_state._last_good_config = cfg`.

- [ ] **Step 7: Export `_get_current_bankroll` in `bot_infra.py` `__all__`**

Find the `__all__` list in bot_infra.py and add `"_get_current_bankroll"` to it.

- [ ] **Step 8: Run Kelly tests — verify they pass**

```
python -m pytest tests/test_kelly_sizing.py -v
```

Expected: 6/6 PASS

- [ ] **Step 9: Run full suite — verify no regressions**

```
python -m pytest tests/ -v --tb=short
```

Expected: all tests pass (382+)

- [ ] **Step 10: Commit**

```bash
git add bot_strategy.py bot_infra.py tests/test_kelly_sizing.py
git commit -m "feat(sizing): add Kelly-fractional position sizing + bankroll tracking

Full Kelly fraction computed from win_prob and entry_price per trade.
Starting bankroll stored in config (default $50, kelly_fraction=0.25).
Current bankroll derived from starting_bankroll + all-time realized PnL.
Also removes 22->17 quiet-hours migration: evening trading restored to 22:00 ET."
```

---

### Task 2: Wire Kelly sizing into S1 trade execution

**Files:**
- Modify: `bot_risk.py` lines 399 and the import block at top

- [ ] **Step 1: Write failing test**

```python
# Add to tests/test_kelly_sizing.py:

def test_s1_execute_uses_kelly_not_flat():
    """Verify _execute_s1_trade respects kelly_fraction and bankroll_dollars in config."""
    # We test this indirectly by checking that calculate_contracts is called with
    # a value proportional to bankroll rather than the flat trade_amount_dollars.
    # Inspect bot_risk._execute_s1_trade source for the config key it reads.
    import inspect
    import bot_risk
    src = inspect.getsource(bot_risk._execute_s1_trade)
    assert "kelly_fraction" in src or "_kelly_trade_amount" in src, \
        "_execute_s1_trade still uses flat trade_amount_dollars — Kelly not wired in"
    assert "bankroll_dollars" in src or "_get_current_bankroll" in src, \
        "_execute_s1_trade does not read bankroll_dollars — Kelly not wired in"
```

- [ ] **Step 2: Run test — verify it fails**

```
python -m pytest tests/test_kelly_sizing.py::test_s1_execute_uses_kelly_not_flat -v
```

Expected: FAIL — `_execute_s1_trade still uses flat trade_amount_dollars`

- [ ] **Step 3: Update imports in `bot_risk.py`**

At the top of `bot_risk.py`, add to the import from `bot_infra`:

```python
from bot_infra import (
    ...existing imports...,
    _get_current_bankroll,
)
```

And add to the import from `bot_strategy`:

```python
from bot_strategy import (
    ...existing imports...,
    _kelly_trade_amount,
)
```

- [ ] **Step 4: Replace flat stake in `_execute_s1_trade` (bot_risk.py:~399)**

Find this line in `_execute_s1_trade`:

```python
    trade_amount = float(config.get("trade_amount_dollars", 25))
```

Replace with:

```python
    _s1_bankroll    = _get_current_bankroll(mode, float(config.get("bankroll_dollars", 50)))
    _s1_kelly_frac  = float(config.get("kelly_fraction", 0.25))
    _s1_min_trade   = float(config.get("min_trade_dollars", 1.0))
    trade_amount    = _kelly_trade_amount(
        _s1_bankroll, brain_s1.get("win_prob", 0.60),
        float(entry_price_cents), _s1_kelly_frac, _s1_min_trade,
    )
```

- [ ] **Step 5: Run test — verify it passes**

```
python -m pytest tests/test_kelly_sizing.py::test_s1_execute_uses_kelly_not_flat -v
```

Expected: PASS

- [ ] **Step 6: Run full suite**

```
python -m pytest tests/ -v --tb=short
```

Expected: all passing

- [ ] **Step 7: Commit**

```bash
git add bot_risk.py
git commit -m "feat(sizing): wire Kelly sizing into S1 trade execution"
```

---

### Task 3: Wire Kelly sizing into S2 trade execution

**Files:**
- Modify: `bot_loops.py` line 435 and imports at top

- [ ] **Step 1: Write failing test**

```python
# Add to tests/test_kelly_sizing.py:

def test_s2_execute_uses_kelly_not_flat():
    """Verify bot_loops S2 handler uses Kelly sizing not flat trade_amount_dollars."""
    import inspect
    import bot_loops
    src = inspect.getsource(bot_loops)
    # Find the trade amount line after the "Position sizing" comment
    idx = src.find("Position sizing")
    assert idx != -1, "Could not find Position sizing comment in bot_loops.py"
    chunk = src[idx:idx+500]
    assert "_kelly_trade_amount" in chunk or "kelly_fraction" in chunk, \
        f"S2 handler still uses flat sizing:\n{chunk}"
```

- [ ] **Step 2: Run test — verify it fails**

```
python -m pytest tests/test_kelly_sizing.py::test_s2_execute_uses_kelly_not_flat -v
```

Expected: FAIL

- [ ] **Step 3: Update imports in `bot_loops.py`**

Add to the existing import line that imports from `bot_infra`:

```python
from bot_infra import (
    ...existing...,
    _get_current_bankroll,
)
```

Add to the existing import line from `bot_strategy`:

```python
from bot_strategy import (
    ...existing...,
    _kelly_trade_amount,
)
```

- [ ] **Step 4: Replace flat stake in S2 handler (`bot_loops.py:~435`)**

Find this block (around line 435):

```python
    # Position sizing — flat fixed amount
    # Reversal trades use 50% of configured amount (contrarian = smaller size)
    trade_amount = config.get("trade_amount_dollars", 25)
```

Replace with:

```python
    # Position sizing — Kelly-fractional based on bankroll + win_prob
    _s2_bankroll   = _get_current_bankroll(mode, float(config.get("bankroll_dollars", 50)))
    _s2_kelly_frac = float(config.get("kelly_fraction", 0.25))
    _s2_min_trade  = float(config.get("min_trade_dollars", 1.0))
    trade_amount   = _kelly_trade_amount(
        _s2_bankroll, float(ai_result.get("win_prob", 0.60)),
        float(entry_price_cents), _s2_kelly_frac, _s2_min_trade,
    )
```

- [ ] **Step 5: Run test — verify it passes**

```
python -m pytest tests/test_kelly_sizing.py -v
```

Expected: all Kelly tests pass

- [ ] **Step 6: Run full suite**

```
python -m pytest tests/ -v --tb=short
```

Expected: all passing

- [ ] **Step 7: Commit**

```bash
git add bot_loops.py
git commit -m "feat(sizing): wire Kelly sizing into S2 trade execution"
```

---

### Task 4: S2 continuation gate (reject reversal trades)

**Files:**
- Modify: `bot_strategy.py` — add continuation gate in `strategy_brain_s2`
- Modify: `tests/test_s2_fires.py` — add reversal rejection test

- [ ] **Step 1: Write failing test**

```python
# Add to tests/test_s2_fires.py, inside TestS2FiresETH class:

    def test_s2_skips_reversal_velocity_yes_but_price_below_strike(self):
        """S2 must reject: velocity=YES but asset price < strike (reversal trade, no edge)."""
        ticker = "KXETH-25MAY30-T2900G"
        _seed_velocity(ticker, "ETH", direction="yes")  # velocity says UP
        result = strategy_brain_s2(
            btc_price=2850.0,   # price BELOW strike (2850 < 2900)
            strike=2900.0,
            yes_ask=45.0,
            no_ask=55.0,
            elapsed_seconds=760.0,
            secs_left=240.0,
            ticker=ticker,
            asset="ETH",
        )
        assert result["action"] == "skip", \
            f"S2 should reject reversal trade but got: {result['reasoning']}"
        assert "s2_reversal_gate" in result["reasoning"], \
            f"Expected s2_reversal_gate, got: {result['reasoning']}"

    def test_s2_skips_reversal_velocity_no_but_price_above_strike(self):
        """S2 must reject: velocity=NO but asset price > strike (reversal trade, no edge)."""
        ticker = "KXETH-25MAY30-T2700H"
        _seed_velocity(ticker, "ETH", direction="no")  # velocity says DOWN
        result = strategy_brain_s2(
            btc_price=2850.0,   # price ABOVE strike (2850 > 2700)
            strike=2700.0,
            yes_ask=70.0,
            no_ask=30.0,
            elapsed_seconds=760.0,
            secs_left=240.0,
            ticker=ticker,
            asset="ETH",
        )
        assert result["action"] == "skip", \
            f"S2 should reject reversal trade but got: {result['reasoning']}"
        assert "s2_reversal_gate" in result["reasoning"], \
            f"Expected s2_reversal_gate, got: {result['reasoning']}"
```

- [ ] **Step 2: Run tests — verify they fail**

```
python -m pytest tests/test_s2_fires.py::TestS2FiresETH::test_s2_skips_reversal_velocity_yes_but_price_below_strike tests/test_s2_fires.py::TestS2FiresETH::test_s2_skips_reversal_velocity_no_but_price_above_strike -v
```

Expected: 2 FAIL (no reversal gate yet)

- [ ] **Step 3: Add S2 continuation gate in `bot_strategy.py`**

In `strategy_brain_s2`, find the line:

```python
    side = direction
    entry_price = yes_ask if side == "yes" else no_ask
```

Add the continuation gate between `side = direction` and `entry_price = ...`:

```python
    side = direction

    # Continuation-only: velocity direction must match asset price position vs strike.
    # If velocity says YES but price is below strike, the market is already bearish —
    # this is a reversal trade with no statistical edge.
    if side == "yes" and current_price < strike:
        return _make_skip(side, "s2_reversal_gate:vel=yes_price_below", abs_pct, mins_left, variant="strategy2")
    if side == "no" and current_price > strike:
        return _make_skip(side, "s2_reversal_gate:vel=no_price_above", abs_pct, mins_left, variant="strategy2")

    entry_price = yes_ask if side == "yes" else no_ask
```

- [ ] **Step 4: Run reversal tests — verify they pass**

```
python -m pytest tests/test_s2_fires.py::TestS2FiresETH::test_s2_skips_reversal_velocity_yes_but_price_below_strike tests/test_s2_fires.py::TestS2FiresETH::test_s2_skips_reversal_velocity_no_but_price_above_strike -v
```

Expected: 2 PASS

- [ ] **Step 5: Run full suite — no regressions**

```
python -m pytest tests/ -v --tb=short
```

Expected: all passing. Verify the existing S2 fire tests (`test_s2_fires_amm_obi_none`, `test_s2_fires_per_asset`) still pass — all those tests have price above strike and velocity=YES so the gate doesn't block them.

- [ ] **Step 6: Commit**

```bash
git add bot_strategy.py tests/test_s2_fires.py
git commit -m "feat(strategy): add S2 continuation gate — reject velocity-vs-price reversals

Mirrors the S1 reversal gate. S2 now skips when velocity direction contradicts
asset price position vs strike: vel=YES+price_below or vel=NO+price_above.
Filters counter-trend entries with no statistical edge."
```

---

## Self-Review

**Spec coverage:**
- Kelly sizing: Tasks 1-3 ✅
- S2 continuation gate: Task 4 ✅
- Quiet hours fix: Task 1 Step 5-6 ✅
- Tests for Kelly: Task 1 + tests in Tasks 2-3 ✅
- Tests for S2 gate: Task 4 ✅

**Placeholder scan:** None — all steps have concrete code.

**Type consistency:**
- `_kelly_trade_amount(bankroll, win_prob, entry_cents, kelly_fraction, min_trade)` → called identically in Tasks 2 and 3 ✅
- `_get_current_bankroll(mode, starting_bankroll)` → same signature in all call sites ✅
- `_make_skip` signature unchanged — S2 gate uses existing pattern ✅
- `ai_result.get("win_prob", 0.60)` in bot_loops.py Task 3 — verify `ai_result` is the brain output dict (it is — confirmed at line 435 context) ✅

**Edge cases:**
- `_kelly_trade_amount`: if entry_cents ≥ 100 or ≤ 0, returns min_trade (not crash) ✅
- `_get_current_bankroll`: returns max(1.0, ...) so Kelly never divides by zero ✅
- S2 gate: existing tests all use price > strike + vel=YES, so they pass unchanged ✅
- Quiet hours: migration removal means value 17 in existing config.json gets overwritten to 22 in `_init_config` on next deploy ✅
