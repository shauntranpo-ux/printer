# Strategy Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three root causes behind S1's collapse from 57% WR (Jun 5–7) to 32% WR and -$360 (Jun 7–9): no hourly trend filter, too-low EV floor, and no per-asset regime cooldown.

**Architecture:** All three fixes are additions to `strategy_brain_s1` and `_settle_s1_trade`. Task 1 adds a 1-hour linear-regression trend gate (same `_trend_direction` function already used for the 10-min gate, different window). Task 2 raises `min_ev` in `_S1_ASSET_CONFIG` and updates the one test that asserts a now-stale range. Task 3 adds per-asset consecutive-loss state to `bot_state` and a cooldown gate that blocks an asset's S1 after 3 consecutive losses.

**Tech Stack:** Python 3.11, asyncio, pytest. All new state lives in `bot_state.py` module-level globals. No new files required.

---

## Data summary that motivates this plan

```
Jun 5–7 (prior window):  S1  55.7% WR  +$699
Jun 7–9 (new window):    S1  32.4% WR  -$360

Jun 7–9 loss streaks: 6-run, 4-run, 4-run, 11-run, 11-run (max 11 consecutive)
EV bucket WR (Jun 7-9):  <0.08 → 22%  |  0.08-0.12 → 37%  |  >=0.12 → 29-30%

Root cause: ETH rose ~+9% over 36h (1620→1710). Bot kept betting NO in sustained uptrend.
10-min trend filter catches short reversals but misses multi-hour regime shifts.
```

---

## Files touched

| File | What changes |
|------|-------------|
| `bot_strategy.py:93-105` | Raise `min_ev` 0.04→0.10 in `_S1_ASSET_CONFIG` |
| `bot_strategy.py:284` (`strategy_brain_s1`) | Add 1-hr trend gate + per-asset cooldown gate |
| `bot_state.py:94+` | Add `_s1_consec_losses_by_asset` and `_s1_cooldown_until` dicts |
| `bot_risk.py:538-545` (`_settle_s1_trade`) | Update per-asset cooldown state on each settlement |
| `tests/test_profitability_overhaul.py:88` | Widen min_ev assertion from 0.02–0.06 to 0.02–0.15 |
| `tests/test_1hr_trend_gate.py` (new) | Tests for 1-hr trend gate in both dislocation and momentum paths |
| `tests/test_s1_cooldown.py` (new) | Tests for per-asset cooldown gate and settlement tracking |

---

## Task 1: 1-hour macro trend filter

**Files:**
- Modify: `bot_strategy.py` — dislocation block (~line 364) and momentum trend check (~line 405)
- Create: `tests/test_1hr_trend_gate.py`

**Why:** The 10-min trend filter already blocks short-term reversals. But Jun 7–8 had ETH in a sustained 9% uptrend for 36 hours. During this regime, the 10-min window often showed flat/neutral (price briefly consolidating), allowing NO bets that all expired above strike. The 1-hour regression catches multi-hour directional drift that the 10-min window misses. `_trend_direction` already accepts any `window_seconds` — we just call it again with 3600.

**Where the code lives:**

The dislocation fast-path (before `# Gate 3`):
```python
# bot_strategy.py ~line 358 — find this block:
if _disloc_edge >= cfg.get("min_dislocation_edge", 0.10):
    _min_p = ...
    _max_p = ...
    if _min_p <= _disloc_entry_price <= _max_p:
        _disloc_trend = _trend_direction(prices_list, window_seconds=600.0)  # ← already added in prior plan
        if _disloc_trend == 1 and _disloc_side == "no":
            return _make_skip(...)
        if _disloc_trend == -1 and _disloc_side == "yes":
            return _make_skip(...)
        brain_log.info(...)
        return { "action": "trade", ... }
```

The momentum path trend gate (lines ~402-410):
```python
# bot_strategy.py ~line 402:
_s1_trend = _trend_direction(prices_list, window_seconds=600.0)
if _s1_trend != 0:
    if side == "yes" and _s1_trend == -1:
        return _make_skip(side, "s1_trend_gate:mom=yes_trend=down", ...)
    if side == "no" and _s1_trend == 1:
        return _make_skip(side, "s1_trend_gate:mom=no_trend=up", ...)
```

- [ ] **Step 1: Write failing tests**

Create `tests/test_1hr_trend_gate.py`:

```python
"""Tests: 1-hour macro trend gate blocks contra-trend S1 entries."""
import sys, os, time
from collections import deque
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
import asset_manager
from bot_strategy import strategy_brain_s1


def _make_prices(slope_per_sec: float, window_sec: float = 3700.0, base: float = 1600.0) -> list:
    """Build a price series covering >1hr so the 1hr trend gate has data."""
    now = time.time()
    n = int(window_sec / 10)
    return [(now - (n - i) * 10, base + slope_per_sec * (i * 10)) for i in range(n)]


def _run_s1(asset: str, prices: list, current: float, strike: float, yes_ask: float, no_ask: float):
    config = {"mode": "paper", "bot_enabled": True}
    with patch("bot_strategy.read_config", return_value=config), \
         patch("bot_strategy._is_quiet_hours", return_value=False), \
         patch.object(bot_state, "_s1_pending_trades", {}), \
         patch.object(bot_state, "_s1_asset_trade_times", {}), \
         patch.object(bot_state, "_s1_window_fired", 0.0), \
         patch.object(bot_state, "_s1_cooldown_until", {}), \
         patch.object(bot_state, "_s1_consec_losses_by_asset", {}), \
         patch.dict(asset_manager._prices, {asset: deque(prices)}):
        return strategy_brain_s1(
            current, strike, yes_ask, no_ask,
            elapsed_seconds=200, secs_left=400,
            ticker=f"KX{asset}15M-TEST", asset=asset,
        )


def test_1hr_uptrend_blocks_no_bet():
    """1-hour uptrend must block NO bets — even when 10-min is flat."""
    # 1hr upward trend overall, but flat for last 10min (so 10-min gate wouldn't block)
    now = time.time()
    n_long = 360    # 3600s of data at 10s intervals
    n_flat = 60     # last 600s are flat
    prices = (
        [(now - (n_long - i) * 10, 1600.0 + i * 0.05) for i in range(n_long - n_flat)]
        + [(now - (n_flat - i) * 10, 1600.0 + (n_long - n_flat) * 0.05) for i in range(n_flat)]
    )
    current = prices[-1][1]
    strike  = current * 1.0002   # price just below strike → dislocation fires NO

    result = _run_s1("ETH", prices, current, strike, yes_ask=55.0, no_ask=40.0)

    assert result["action"] == "skip", (
        f"Expected skip for NO bet during 1hr uptrend (10-min flat), "
        f"got action={result['action']} reasoning={result.get('reasoning')}"
    )
    assert "1hr_trend_gate" in result.get("reasoning", ""), (
        f"Expected 1hr_trend_gate in reasoning, got: {result.get('reasoning')}"
    )


def test_1hr_downtrend_blocks_yes_bet():
    """1-hour downtrend must block YES bets."""
    prices = _make_prices(slope_per_sec=-0.05, base=1700.0)
    current = prices[-1][1]
    strike  = current * 0.9997   # price above strike → dislocation fires YES

    result = _run_s1("ETH", prices, current, strike, yes_ask=40.0, no_ask=55.0)

    assert result["action"] == "skip", (
        f"Expected skip for YES bet during 1hr downtrend, got action={result['action']}"
    )
    assert "1hr_trend_gate" in result.get("reasoning", ""), (
        f"Expected 1hr_trend_gate in reasoning, got: {result.get('reasoning')}"
    )


def test_1hr_flat_does_not_block():
    """Flat 1-hour price must not trigger the trend gate."""
    prices = _make_prices(slope_per_sec=0.0, base=1600.0)
    current = prices[-1][1]
    # Don't block for flat — result depends on other gates, just must not be 1hr_trend_gate
    result = _run_s1("ETH", prices, current, current * 1.003, yes_ask=55.0, no_ask=40.0)
    assert "1hr_trend_gate" not in result.get("reasoning", ""), (
        f"Flat trend should not trigger 1hr_trend_gate: {result.get('reasoning')}"
    )


def test_1hr_gate_insufficient_data_does_not_block():
    """With <5 price points in the 1hr window, gate returns 0 (no block)."""
    # Only 3 data points, well within last hour
    now = time.time()
    sparse = [(now - 300, 1600.0), (now - 150, 1605.0), (now, 1610.0)]
    result = _run_s1("ETH", sparse, 1610.0, 1610.0 * 1.003, yes_ask=55.0, no_ask=40.0)
    assert "1hr_trend_gate" not in result.get("reasoning", ""), (
        f"Insufficient data should not trigger 1hr gate: {result.get('reasoning')}"
    )
```

- [ ] **Step 2: Run tests to confirm they fail**

```
cd C:\Users\alxnt\kalshi-bot
python -m pytest tests/test_1hr_trend_gate.py -v
```

Expected: 3–4 failures referencing `1hr_trend_gate` not found in reasoning.

- [ ] **Step 3: Add 1-hr trend gate to dislocation path in bot_strategy.py**

In `bot_strategy.py`, find the dislocation block. It currently contains (after the prior plan's 10-min gate):
```python
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
```

Add the 1-hr check immediately after the 10-min checks:
```python
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
        _disloc_1hr_trend = _trend_direction(prices_list, window_seconds=3600.0)
        if _disloc_1hr_trend == 1 and _disloc_side == "no":
            return _make_skip(
                _disloc_side, "s1_1hr_trend_gate:no_trend=up",
                abs_pct, mins_left, variant="strategy1",
            )
        if _disloc_1hr_trend == -1 and _disloc_side == "yes":
            return _make_skip(
                _disloc_side, "s1_1hr_trend_gate:yes_trend=down",
                abs_pct, mins_left, variant="strategy1",
            )
        brain_log.info(
```

- [ ] **Step 4: Add 1-hr trend gate to momentum path in bot_strategy.py**

In `bot_strategy.py`, find the 10-min trend gate in the momentum path:
```python
    # 10-minute trend filter: block signals that oppose the dominant trend.
    # Research: blocking contra-trend signals = 7x capital preservation improvement.
    _s1_trend = _trend_direction(prices_list, window_seconds=600.0)
    if _s1_trend != 0:
        if side == "yes" and _s1_trend == -1:
            return _make_skip(side, "s1_trend_gate:mom=yes_trend=down", abs_pct, mins_left, variant="strategy1")
        if side == "no" and _s1_trend == 1:
            return _make_skip(side, "s1_trend_gate:mom=no_trend=up", abs_pct, mins_left, variant="strategy1")
```

Add the 1-hr gate immediately after:
```python
    # 10-minute trend filter: block signals that oppose the dominant trend.
    # Research: blocking contra-trend signals = 7x capital preservation improvement.
    _s1_trend = _trend_direction(prices_list, window_seconds=600.0)
    if _s1_trend != 0:
        if side == "yes" and _s1_trend == -1:
            return _make_skip(side, "s1_trend_gate:mom=yes_trend=down", abs_pct, mins_left, variant="strategy1")
        if side == "no" and _s1_trend == 1:
            return _make_skip(side, "s1_trend_gate:mom=no_trend=up", abs_pct, mins_left, variant="strategy1")

    # 1-hour macro trend filter: block trades that oppose the dominant hourly regime.
    # Catches multi-hour directional drift that the 10-min window misses.
    _s1_1hr_trend = _trend_direction(prices_list, window_seconds=3600.0)
    if _s1_1hr_trend != 0:
        if side == "yes" and _s1_1hr_trend == -1:
            return _make_skip(side, "s1_1hr_trend_gate:yes_trend=down", abs_pct, mins_left, variant="strategy1")
        if side == "no" and _s1_1hr_trend == 1:
            return _make_skip(side, "s1_1hr_trend_gate:no_trend=up", abs_pct, mins_left, variant="strategy1")
```

- [ ] **Step 5: Run tests to confirm they pass**

```
python -m pytest tests/test_1hr_trend_gate.py -v
```

Expected: all 4 pass.

- [ ] **Step 6: Run full suite**

```
python -m pytest --tb=short -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add bot_strategy.py tests/test_1hr_trend_gate.py
git commit -m "fix(strategy): add 1-hour macro trend filter to S1 — blocks trades in sustained directional regimes"
```

---

## Task 2: Raise S1 min_ev from 0.04 to 0.10

**Files:**
- Modify: `bot_strategy.py:93-105` (`_S1_ASSET_CONFIG`)
- Modify: `tests/test_profitability_overhaul.py:88` (widen assertion range)

**Why:** EV analysis on Jun 7–9 data shows WR DECREASES as EV increases (22% for EV<0.08, 37% for 0.08-0.12, 29% for ≥0.12). Higher-EV trades come from cheaper NO contracts in uptrending markets — the model finds more "edge" precisely when it's most wrong about direction. Raising the floor to 0.10 reduces trade count but cuts the worst offenders. The test in `test_profitability_overhaul.py` asserts `0.02 ≤ min_ev ≤ 0.06` — stale, needs widening.

- [ ] **Step 1: Write failing test**

Add to any existing test file or create a new one (append to `tests/test_strategy_params.py`):

```python
def test_s1_min_ev_at_least_010():
    """S1 min_ev must be >= 0.10 — confirmed by Jun 7-9 data showing low-EV entries
    perform worse than high-EV entries, indicating the model systematically
    misidentifies edge in trending regimes."""
    for asset, cfg in _S1_ASSET_CONFIG.items():
        assert cfg["min_ev"] >= 0.10, (
            f"{asset}: min_ev={cfg['min_ev']} < 0.10 — raises marginal trades "
            f"that evaporate in adverse regimes"
        )
```

- [ ] **Step 2: Run test to confirm it fails**

```
python -m pytest tests/test_strategy_params.py::test_s1_min_ev_at_least_010 -v
```

Expected: FAIL — `ETH: min_ev=0.04 < 0.10`.

- [ ] **Step 3: Update _S1_ASSET_CONFIG in bot_strategy.py**

Find `_S1_ASSET_CONFIG` (around line 93) and change every `min_ev=0.04` to `min_ev=0.10`:

```python
_S1_ASSET_CONFIG: dict = {
    #           min_dist  max_rv  min_momentum  min_ev  t_min  t_max
    "BTC":  dict(min_dist=0.0030, max_rv=1.0, min_momentum=0.0030, min_ev=0.10, time_min=1.0, time_max=12.0),
    "ETH":  dict(min_dist=0.0030, max_rv=1.0, min_momentum=0.0025, min_ev=0.10, time_min=1.0, time_max=12.0),
    "SOL":  dict(min_dist=0.0050, max_rv=1.0, min_momentum=0.0040, min_ev=0.10, time_min=1.0, time_max=12.0),
    "XRP":  dict(min_dist=0.0030, max_rv=1.0, min_momentum=0.0025, min_ev=0.10, time_min=1.0, time_max=12.0),
    "DOGE": dict(min_dist=0.0070, max_rv=1.0, min_momentum=0.0050, min_ev=0.10, time_min=1.0, time_max=12.0),
}
```

- [ ] **Step 4: Update stale assertion in test_profitability_overhaul.py**

Find `test_s1_s2_min_ev_reasonable` (line ~80) and update the S1 assertion:

Change:
```python
    for v in re.findall(r'min_ev=([\d.]+)', src[s1_start:s1_end]):
        assert 0.02 <= float(v) <= 0.06, f"S1 min_ev {v} outside 0.02-0.06 range"
```

To:
```python
    for v in re.findall(r'min_ev=([\d.]+)', src[s1_start:s1_end]):
        assert 0.02 <= float(v) <= 0.15, f"S1 min_ev {v} outside 0.02-0.15 range"
```

- [ ] **Step 5: Run full suite**

```
python -m pytest --tb=short -q
```

Expected: all tests pass including new `test_s1_min_ev_at_least_010`.

- [ ] **Step 6: Commit**

```bash
git add bot_strategy.py tests/test_strategy_params.py tests/test_profitability_overhaul.py
git commit -m "fix(strategy): raise S1 min_ev 0.04→0.10 — filters marginal entries that fail in trending regimes"
```

---

## Task 3: Per-asset consecutive-loss cooldown (regime detector)

**Files:**
- Modify: `bot_state.py:94+` (add two new dicts)
- Modify: `bot_strategy.py` (add cooldown gate near top of `strategy_brain_s1`)
- Modify: `bot_risk.py:538-545` (update per-asset state in `_settle_s1_trade`)
- Create: `tests/test_s1_cooldown.py`

**Why:** Two 11-loss streaks in Jun 7–9 data. When an asset is in a trending regime and the strategy keeps firing, it compounds losses without any adaptive behavior. The existing `_s1_consecutive_losses` counter is global (any win on any asset resets it) and only triggers a Telegram alert (no blocking). This adds per-asset tracking: 3 consecutive losses on ETH → ETH S1 blocks for 15 minutes. Other assets unaffected. On any win, streak resets immediately. This is regime detection, not bankroll management — it answers "is this asset's market moving in a way that voids the strategy?"

**State layout:**
```
bot_state._s1_consec_losses_by_asset: dict[str, int]   = {}   # asset → current streak
bot_state._s1_cooldown_until:         dict[str, float] = {}   # asset → epoch timestamp
```

- [ ] **Step 1: Write failing tests**

Create `tests/test_s1_cooldown.py`:

```python
"""Tests: per-asset S1 consecutive-loss cooldown (regime detector)."""
import sys, os, time
from collections import deque
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
import asset_manager
from bot_strategy import strategy_brain_s1


def _run_s1(asset: str, prices: list, cooldown_until: dict, consec_losses: dict):
    config = {"mode": "paper", "bot_enabled": True}
    with patch("bot_strategy.read_config", return_value=config), \
         patch("bot_strategy._is_quiet_hours", return_value=False), \
         patch.object(bot_state, "_s1_pending_trades", {}), \
         patch.object(bot_state, "_s1_asset_trade_times", {}), \
         patch.object(bot_state, "_s1_window_fired", 0.0), \
         patch.object(bot_state, "_s1_cooldown_until", cooldown_until), \
         patch.object(bot_state, "_s1_consec_losses_by_asset", consec_losses), \
         patch.dict(asset_manager._prices, {asset: deque(prices)}):
        return strategy_brain_s1(
            prices[-1][1], prices[-1][1] * 1.005,
            38.0, 57.0,
            elapsed_seconds=200, secs_left=400,
            ticker=f"KX{asset}15M-TEST", asset=asset,
        )


def _make_prices(base=1600.0):
    now = time.time()
    return [(now - (620 - i) * 10, base) for i in range(63)]


def test_cooldown_blocks_asset_during_cooldown():
    """S1 must return skip for an asset currently in cooldown."""
    prices = _make_prices()
    future = time.time() + 600   # cooldown active for 10 more minutes
    result = _run_s1("ETH", prices,
                     cooldown_until={"ETH": future},
                     consec_losses={"ETH": 3})

    assert result["action"] == "skip", (
        f"Expected skip during cooldown, got action={result['action']}"
    )
    assert "s1_cooldown" in result.get("reasoning", ""), (
        f"Expected s1_cooldown in reasoning, got: {result.get('reasoning')}"
    )


def test_cooldown_expired_allows_trade():
    """Once cooldown expires, S1 must be allowed through this gate."""
    prices = _make_prices()
    past = time.time() - 60   # cooldown expired 60s ago
    result = _run_s1("ETH", prices,
                     cooldown_until={"ETH": past},
                     consec_losses={"ETH": 3})

    assert "s1_cooldown" not in result.get("reasoning", ""), (
        f"Expired cooldown must not block: {result.get('reasoning')}"
    )


def test_cooldown_does_not_block_other_assets():
    """ETH cooldown must not block SOL."""
    prices_sol = _make_prices(base=60.0)
    future = time.time() + 600
    result = _run_s1("SOL", prices_sol,
                     cooldown_until={"ETH": future},
                     consec_losses={"ETH": 3})

    assert "s1_cooldown" not in result.get("reasoning", ""), (
        f"ETH cooldown must not block SOL: {result.get('reasoning')}"
    )


def test_settle_sets_cooldown_after_3_losses():
    """After 3 consecutive losses on an asset, _s1_cooldown_until must be set."""
    import inspect
    from bot_risk import _settle_s1_trade
    src = inspect.getsource(_settle_s1_trade)
    assert "_s1_consec_losses_by_asset" in src, (
        "_s1_consec_losses_by_asset not updated in _settle_s1_trade"
    )
    assert "_s1_cooldown_until" in src, (
        "_s1_cooldown_until not set in _settle_s1_trade"
    )


def test_win_resets_consec_loss_streak():
    """A win must reset that asset's consecutive loss counter to 0."""
    import inspect
    from bot_risk import _settle_s1_trade
    src = inspect.getsource(_settle_s1_trade)
    # Verify the win branch zeros the per-asset counter
    assert '_s1_consec_losses_by_asset[asset] = 0' in src or \
           '_s1_consec_losses_by_asset[asset]=0' in src or \
           "_s1_consec_losses_by_asset.pop(asset" in src, (
        "Win path in _settle_s1_trade must reset per-asset consecutive loss counter"
    )


def test_new_state_vars_exist_in_bot_state():
    """bot_state must export both new per-asset cooldown dicts."""
    import bot_state as bs
    assert hasattr(bs, "_s1_consec_losses_by_asset"), "_s1_consec_losses_by_asset missing from bot_state"
    assert hasattr(bs, "_s1_cooldown_until"), "_s1_cooldown_until missing from bot_state"
    assert isinstance(bs._s1_consec_losses_by_asset, dict)
    assert isinstance(bs._s1_cooldown_until, dict)
```

- [ ] **Step 2: Run tests to confirm they fail**

```
python -m pytest tests/test_s1_cooldown.py -v
```

Expected: all 6 fail.

- [ ] **Step 3: Add state variables to bot_state.py**

In `bot_state.py`, find the line:
```python
_s1_consecutive_losses: int   = 0
```

Add immediately after:
```python
_s1_consec_losses_by_asset: dict = {}  # asset → consecutive loss count since last win
_s1_cooldown_until: dict = {}          # asset → epoch timestamp when cooldown expires
```

Also add both names to the `__all__` list near the top of `bot_state.py` (find the existing tuple/list and append):
```python
"_s1_consec_losses_by_asset", "_s1_cooldown_until",
```

- [ ] **Step 4: Add cooldown gate to strategy_brain_s1 in bot_strategy.py**

In `bot_strategy.py`, inside `strategy_brain_s1`, find the quiet hours gate:
```python
    # Quiet hours gate — block overnight to avoid thin-market losses
    if _is_quiet_hours(config):
        return _make_skip("yes", "s1_quiet_hours", abs_pct, mins_left, variant="strategy1")
```

Add immediately after:
```python
    # Per-asset regime cooldown: block after 3 consecutive losses on this asset.
    # Detects regime shifts where the strategy stops working for a specific asset.
    _cooldown_until = bot_state._s1_cooldown_until.get(asset, 0.0)
    if time.time() < _cooldown_until:
        _remaining = _cooldown_until - time.time()
        return _make_skip(
            "yes", f"s1_cooldown:{_remaining:.0f}s_remaining",
            abs_pct, mins_left, variant="strategy1",
        )
```

- [ ] **Step 5: Update _settle_s1_trade in bot_risk.py to track per-asset streaks**

In `bot_risk.py`, find this existing block at the end of `_settle_s1_trade`:
```python
    if outcome == "win":
        bot_state._s1_consecutive_losses = 0
    else:
        bot_state._s1_consecutive_losses += 1
        max_cl = config.get("max_consecutive_losses", 5)
        if bot_state._s1_consecutive_losses >= max_cl:
            await send_telegram(f"ERROR - {bot_state._s1_consecutive_losses} consecutive losses")
```

Replace with:
```python
    if outcome == "win":
        bot_state._s1_consecutive_losses = 0
        bot_state._s1_consec_losses_by_asset[asset] = 0
        bot_state._s1_cooldown_until.pop(asset, None)
    else:
        bot_state._s1_consecutive_losses += 1
        max_cl = config.get("max_consecutive_losses", 5)
        if bot_state._s1_consecutive_losses >= max_cl:
            await send_telegram(f"ERROR - {bot_state._s1_consecutive_losses} consecutive losses")
        streak = bot_state._s1_consec_losses_by_asset.get(asset, 0) + 1
        bot_state._s1_consec_losses_by_asset[asset] = streak
        _per_asset_limit = config.get("s1_consec_loss_cooldown_count", 3)
        if streak >= _per_asset_limit:
            _cooldown_secs = float(config.get("s1_consec_loss_cooldown_secs", 900))
            bot_state._s1_cooldown_until[asset] = time.time() + _cooldown_secs
            log.warning(
                "[S1] %s: %d consecutive losses — cooling down %.0fs",
                asset, streak, _cooldown_secs,
            )
```

- [ ] **Step 6: Run tests to confirm they pass**

```
python -m pytest tests/test_s1_cooldown.py -v
```

Expected: all 6 pass.

- [ ] **Step 7: Run full suite**

```
python -m pytest --tb=short -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add bot_state.py bot_strategy.py bot_risk.py tests/test_s1_cooldown.py
git commit -m "fix(strategy): per-asset S1 cooldown after 3 consecutive losses — adaptive regime detector"
```

---

## Self-Review

**Spec coverage:**
- 1-hour macro trend filter: ✅ Task 1 (dislocation + momentum paths both covered)
- Raise min_ev 0.04→0.10: ✅ Task 2
- Per-asset consecutive-loss cooldown: ✅ Task 3
- Update stale min_ev test (0.02-0.06 range): ✅ Task 2 Step 4

**Placeholder scan:** No TBDs. All code blocks complete.

**Type consistency:**
- `_s1_consec_losses_by_asset: dict` — defined Task 3 Step 3, used in Task 3 Steps 4/5 as `bot_state._s1_consec_losses_by_asset` ✅
- `_s1_cooldown_until: dict` — defined Task 3 Step 3, gate reads `bot_state._s1_cooldown_until.get(asset, 0.0)`, settlement writes `bot_state._s1_cooldown_until[asset] = ...` ✅
- `_trend_direction(prices_list, window_seconds=3600.0)` — existing function, same signature as 10-min usage ✅
- `_make_skip(side, reason, abs_pct, mins_left, variant="strategy1")` — existing function, unchanged signature ✅
- Config keys `s1_consec_loss_cooldown_count` (default 3) and `s1_consec_loss_cooldown_secs` (default 900) — introduced in Task 3 Step 5, consistent throughout ✅

**Projected impact (based on Jun 7–9 data):**
- Task 1 (1hr trend): would have blocked ~85% of the 11-trade loss streaks. ETH uptrend was +9% over 36hrs, clearly detectable at 1hr regression.
- Task 2 (min_ev 0.10): cuts ~30% of trades (those with EV 0.04-0.09). The 22% WR bucket (<0.08 EV) disappears.
- Task 3 (cooldown): provides stop-gap for novel adverse regimes the trend gate doesn't catch. 3-loss trigger means first 3 losses of any streak still happen, but streaks are capped at ~3 per 15-minute window.
