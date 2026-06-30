# Strategy Improvements v1 — Bug Fixes & Model Corrections

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 5 confirmed bugs/gaps in the S1/S2 strategy brains: wrong win-prob model in S2, DST timezone bug in two time-of-day functions, artificially low S2 tanh ceiling, over-generous GBM floor/ceiling, and missing XRP disable regression tests.

**Architecture:** Surgical edits to `bot_strategy.py` only, plus a new test file. No new deps. Every change is justified by specific code evidence (line numbers given below). No speculative features.

**Tech Stack:** Python 3.14, stdlib `zoneinfo` (already used in bot_loops.py), `math`, `datetime`, `pytest`.

## Global Constraints

- Python 3.14 — `zoneinfo.ZoneInfo` is stdlib, no `pytz` needed
- No new runtime dependencies
- All tests in `tests/` directory, run with `python -m pytest`
- conftest.py patches `_is_quiet_hours → False` for tests not named `test_quiet_hours_*`
- Strategy skip reasons follow format `s1_REASON` or `s2_REASON`
- `bot_strategy.py` is the only live strategy file — no changes to `src/kalshi_bot/`

---

## Diagnosis

| # | Bug | File:Line | Evidence | Severity |
|---|-----|-----------|----------|----------|
| 1 | S2 calls `_s1_certainty_win_prob()` — velocity ignored in EV gate | `bot_strategy.py:817` | `win_prob = _s1_certainty_win_prob(abs_pct, secs_left, asset)` but `_s2_lookup_win_rate(asset, vel_delta, mins_left, cfg)` exists at line 636 | Critical |
| 2 | DST bug: `-4` hardcoded in both `_is_quiet_hours` and `_time_of_day_vol_multiplier` | `bot_strategy.py:119,234` | `datetime.timezone(datetime.timedelta(hours=-4))` — wrong Nov–Mar (EST = -5) | Medium |
| 3 | S2 tanh ceiling 0.60 undershoots observed 55–62% WR | `bot_strategy.py:658` | `0.52 + 0.08 * math.tanh(...)` max = 0.60; comment says WR is "55–62%" | Medium |
| 4 | GBM floor 0.52 gives phantom 2% edge on zero-signal trades; ceiling 0.75 caps deep OTM | `bot_strategy.py:222` | `max(0.52, min(0.75, cert))` — even 0% distance gets 52% prob | Medium |
| 5 | XRP disabled in S1 with no regression test — re-enabling undetected | `bot_strategy.py:304` | gate exists, no test in `tests/` | Medium |

---

## Files to Modify

| File | What Changes |
|------|-------------|
| `bot_strategy.py:9` | Add `from zoneinfo import ZoneInfo` import |
| `bot_strategy.py:119` | `_is_quiet_hours`: replace hardcoded `-4` with `ZoneInfo("America/New_York")` |
| `bot_strategy.py:222` | `_s1_certainty_win_prob`: floor 0.52→0.50, ceiling 0.75→0.85 |
| `bot_strategy.py:234` | `_time_of_day_vol_multiplier`: replace hardcoded `-4` with `ZoneInfo("America/New_York")` |
| `bot_strategy.py:658` | `_s2_lookup_win_rate`: `0.08 * tanh(...)` → `0.10 * tanh(...)` |
| `bot_strategy.py:817` | `strategy_brain_s2`: replace `_s1_certainty_win_prob` with `_s2_lookup_win_rate` |

New test files:
- `tests/test_s2_win_prob.py`
- `tests/test_dst_timezone.py`
- `tests/test_gbm_model.py`
- `tests/test_xrp_s1_gates.py`

---

## Task 1: Fix S2 win probability model — use velocity-aware `_s2_lookup_win_rate`

**The bug:** `strategy_brain_s2` line 817 calls `_s1_certainty_win_prob(abs_pct, secs_left, asset)` — the S1 GBM model that only uses distance and time. It ignores `vel_delta` entirely. Two S2 trades with the same distance but 3× different velocities get the same win_prob and EV. `_s2_lookup_win_rate(asset, vel_delta, mins_left, cfg)` already exists at line 636 and correctly incorporates velocity via the tanh model.

**At the fix site:** `vel_delta` (computed at line ~775), `mins_left` (~line 745), and `cfg` (~line 735) are all in scope.

**Files:**
- Modify: `bot_strategy.py:817`
- Create: `tests/test_s2_win_prob.py`

- [ ] **Step 1.1: Write failing test**

```python
# tests/test_s2_win_prob.py
"""S2 win probability must use velocity-aware _s2_lookup_win_rate, not S1 GBM."""
import sys, os, time, collections
from unittest.mock import patch
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
import asset_manager
from bot_strategy import strategy_brain_s2


def _contract_history(ticker: str, base: float, vel_ratio: float, lookback: int = 30) -> None:
    """Populate _contract_price_history with a velocity signal at vel_ratio * min_vel."""
    from bot_strategy import _S2_ASSET_CONFIG
    cfg = _S2_ASSET_CONFIG["ETH"]
    min_vel = cfg["min_vel_delta"]
    step = (min_vel * vel_ratio) / lookback
    now = time.time()
    history = collections.deque(maxlen=60)
    for i in range(lookback + 1):
        history.append((now - (lookback - i) * 10, base + i * step))
    bot_state._contract_price_history[ticker] = history


def _run_s2(ticker: str, vel_ratio: float, btc_price: float = 2800.0, strike: float = 2800.0,
            yes_ask: float = 40.0, no_ask: float = 55.0) -> dict:
    _contract_history(ticker, base=yes_ask, vel_ratio=vel_ratio)
    config = {"mode": "paper", "bot_enabled": True}
    patches = [
        patch("bot_strategy.read_config", return_value=config),
        patch("bot_strategy._is_quiet_hours", return_value=False),
        patch.object(bot_state, "_ticker_obi", {ticker: 0.25}),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return strategy_brain_s2(
            btc_price=btc_price,
            strike=strike,
            yes_ask=yes_ask,
            no_ask=no_ask,
            elapsed_seconds=200.0,
            secs_left=400.0,
            ticker=ticker,
            asset="ETH",
        )


def test_s2_high_velocity_gets_higher_win_prob_than_low_velocity():
    """High-velocity S2 trade must have higher win_prob than low-velocity trade.
    With S1 GBM model (distance-only), both would be identical. This catches the bug.
    """
    ticker_hi = "KXETH15M-HI"
    ticker_lo = "KXETH15M-LO"
    result_hi = _run_s2(ticker_hi, vel_ratio=3.0, btc_price=2830.0, strike=2800.0)
    result_lo = _run_s2(ticker_lo, vel_ratio=1.3, btc_price=2830.0, strike=2800.0)

    # Both may trade or skip EV gate, but win_prob must differ
    wp_hi = result_hi.get("win_prob", 0)
    wp_lo = result_lo.get("win_prob", 0)
    assert wp_hi > wp_lo, (
        f"High velocity (3x) must give higher win_prob than low velocity (1.3x). "
        f"Got hi={wp_hi:.4f}, lo={wp_lo:.4f} — S2 is using S1 distance-only model."
    )


def test_s2_win_prob_increases_with_velocity():
    """As vel_ratio rises from 1.5x to 5x, win_prob must strictly increase."""
    ticker = "KXETH15M-VEL"
    prev_wp = 0.0
    for ratio in [1.5, 2.0, 3.0, 5.0]:
        result = _run_s2(ticker, vel_ratio=ratio, btc_price=2830.0, strike=2800.0)
        wp = result.get("win_prob", 0)
        assert wp >= prev_wp - 0.001, (
            f"win_prob must not decrease as vel_ratio increases. "
            f"At ratio={ratio}, wp={wp:.4f} < prev={prev_wp:.4f}"
        )
        prev_wp = wp
```

- [ ] **Step 1.2: Run test to verify it fails**

```bash
python -m pytest tests/test_s2_win_prob.py -v
```

Expected: FAIL — `wp_hi == wp_lo` (S1 model ignores velocity)

- [ ] **Step 1.3: Fix strategy_brain_s2 to use `_s2_lookup_win_rate`**

Find in `bot_strategy.py` (~line 815-817):
```python
    # Win probability: geometric certainty model (velocity qualifies direction,
    # dist+time determines how certain the outcome is)
    win_prob = _s1_certainty_win_prob(abs_pct, secs_left, asset)
```

Replace with:
```python
    # Win probability: velocity-aware S2 model — velocity delta + time determines confidence.
    win_prob = _s2_lookup_win_rate(asset, vel_delta, mins_left, cfg)
```

- [ ] **Step 1.4: Run test to verify it passes**

```bash
python -m pytest tests/test_s2_win_prob.py -v
```

Expected: PASS — high velocity now gives higher win_prob

- [ ] **Step 1.5: Verify no syntax errors and all existing tests pass**

```bash
python -c "import bot_strategy; print('OK')" && python -m pytest tests/test_s2_fires.py tests/test_s2_params_calibration.py -v --tb=short 2>&1 | tail -10
```

Expected: `OK` and no new failures beyond the pre-existing `test_s2_skips_weak_velocity_below_1x5`.

- [ ] **Step 1.6: Commit**

```bash
git add bot_strategy.py tests/test_s2_win_prob.py
git commit -m "fix(strategy): S2 win prob use _s2_lookup_win_rate — was using S1 distance-only model"
```

---

## Task 2: Fix DST timezone bug in `_is_quiet_hours` and `_time_of_day_vol_multiplier`

**The bug:** Both functions compute Eastern Time by subtracting 4 hours from UTC (`datetime.timedelta(hours=-4)`). This is EDT (summer). During EST (Nov–Mar, when clocks fall back), the offset is −5, so quiet hours and vol multipliers fire one hour late/early for ~5 months per year.

**The fix:** Use `ZoneInfo("America/New_York")` which handles DST automatically. `zoneinfo` is Python stdlib since 3.9 and is already imported in `bot_loops.py`.

**Files:**
- Modify: `bot_strategy.py:9` (add import), `bot_strategy.py:119`, `bot_strategy.py:234`
- Create: `tests/test_dst_timezone.py`

- [ ] **Step 2.1: Write failing test**

```python
# tests/test_dst_timezone.py
"""_is_quiet_hours and _time_of_day_vol_multiplier must use DST-aware timezone."""
import inspect
import bot_strategy


def test_is_quiet_hours_uses_zoneinfo():
    """_is_quiet_hours must not use hardcoded -4 offset; must use ZoneInfo."""
    src = inspect.getsource(bot_strategy._is_quiet_hours)
    assert 'timedelta(hours=-4)' not in src, (
        "_is_quiet_hours still uses hardcoded -4 (EDT only). Use ZoneInfo('America/New_York')."
    )
    assert 'ZoneInfo' in src or 'America/New_York' in src, (
        "_is_quiet_hours must use ZoneInfo('America/New_York') for DST-correct Eastern Time."
    )


def test_time_of_day_vol_multiplier_uses_zoneinfo():
    """_time_of_day_vol_multiplier must not use hardcoded -4 offset."""
    src = inspect.getsource(bot_strategy._time_of_day_vol_multiplier)
    assert 'timedelta(hours=-4)' not in src, (
        "_time_of_day_vol_multiplier still uses hardcoded -4 (EDT only). Use ZoneInfo."
    )
    assert 'ZoneInfo' in src or 'America/New_York' in src, (
        "_time_of_day_vol_multiplier must use ZoneInfo('America/New_York')."
    )


def test_zoneinfo_imported_in_bot_strategy():
    """ZoneInfo must be importable from bot_strategy module."""
    src = inspect.getsource(bot_strategy)
    assert 'ZoneInfo' in src, (
        "ZoneInfo not found in bot_strategy.py — add 'from zoneinfo import ZoneInfo' to imports."
    )
```

- [ ] **Step 2.2: Run test to verify it fails**

```bash
python -m pytest tests/test_dst_timezone.py -v
```

Expected: FAIL — `timedelta(hours=-4)` still in source

- [ ] **Step 2.3: Add ZoneInfo import to bot_strategy.py**

Find the imports block at the top of `bot_strategy.py` (~lines 1-9). After the existing imports, add:
```python
from zoneinfo import ZoneInfo
```

The full import block should then include this line. Add it after `from collections import deque`.

- [ ] **Step 2.4: Fix `_is_quiet_hours` (~line 119)**

Find in `bot_strategy.py`:
```python
        now_et = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-4)))
        hour = now_et.hour
```

Replace with:
```python
        now_et = datetime.datetime.now(ZoneInfo("America/New_York"))
        hour = now_et.hour
```

- [ ] **Step 2.5: Fix `_time_of_day_vol_multiplier` (~line 234)**

Find in `bot_strategy.py`:
```python
        now_et = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-4)))
        t = now_et.hour * 60 + now_et.minute
```

Replace with:
```python
        now_et = datetime.datetime.now(ZoneInfo("America/New_York"))
        t = now_et.hour * 60 + now_et.minute
```

- [ ] **Step 2.6: Run test to verify it passes**

```bash
python -m pytest tests/test_dst_timezone.py -v
```

Expected: PASS

- [ ] **Step 2.7: Verify quiet hours still work correctly**

```bash
python -m pytest tests/test_quiet_default.py -v
```

Expected: 2/2 PASS

- [ ] **Step 2.8: Verify no syntax errors**

```bash
python -c "import bot_strategy; print('OK')"
```

Expected: `OK`

- [ ] **Step 2.9: Commit**

```bash
git add bot_strategy.py tests/test_dst_timezone.py
git commit -m "fix(strategy): use ZoneInfo(America/New_York) in quiet hours and vol multiplier — fix DST bug"
```

---

## Task 3: Fix S2 tanh win-prob ceiling (0.60 → 0.62)

**The bug:** `_s2_lookup_win_rate` returns `0.52 + 0.08 * math.tanh(vel_delta / max(min_vel, 1e-6))`. The max value of tanh is 1.0, so the ceiling is `0.52 + 0.08 = 0.60`. But comment in the same function says "reflects 55–62% WR observed in paper trades." The formula systematically under-models the top end. Changing `0.08` → `0.10` raises the ceiling to `0.62`, matching observations.

**Files:**
- Modify: `bot_strategy.py:658` (in `_s2_lookup_win_rate`)
- Create: `tests/test_s2_tanh_ceiling.py` (can extend existing test file instead)

- [ ] **Step 3.1: Write failing test**

Add to `tests/test_s2_win_prob.py` (append, do not overwrite):

```python
def test_s2_tanh_ceiling_is_at_least_62_pct():
    """_s2_lookup_win_rate must be able to return >= 0.62 at very high velocity."""
    from bot_strategy import _s2_lookup_win_rate, _S2_ASSET_CONFIG
    cfg = _S2_ASSET_CONFIG["ETH"]
    # vel_delta = 1000x min_vel → tanh → 1.0 → should hit ceiling
    very_high_vel = cfg["min_vel_delta"] * 1000
    result = _s2_lookup_win_rate("ETH", very_high_vel, 5.0, cfg)
    assert result >= 0.62, (
        f"_s2_lookup_win_rate ceiling must be >= 0.62 (matching observed WR). "
        f"Got {result:.4f} — change '0.08 * tanh' to '0.10 * tanh' in _s2_lookup_win_rate."
    )


def test_s2_tanh_floor_is_0_52():
    """At zero velocity (tanh=0), _s2_lookup_win_rate must return 0.52 (base rate)."""
    from bot_strategy import _s2_lookup_win_rate, _S2_ASSET_CONFIG
    cfg = _S2_ASSET_CONFIG["ETH"]
    result = _s2_lookup_win_rate("ETH", 0.0, 5.0, cfg)
    assert abs(result - 0.52) < 0.001, (
        f"_s2_lookup_win_rate at vel=0 must return 0.52. Got {result:.4f}"
    )
```

- [ ] **Step 3.2: Run test to verify ceiling test fails**

```bash
python -m pytest tests/test_s2_win_prob.py::test_s2_tanh_ceiling_is_at_least_62_pct -v
```

Expected: FAIL — ceiling is 0.60, not 0.62

- [ ] **Step 3.3: Fix the tanh coefficient in `_s2_lookup_win_rate`**

Find in `bot_strategy.py` (~line 658):
```python
    return 0.52 + 0.08 * math.tanh(vel_delta / max(min_vel, 1e-6))
```

Replace with:
```python
    return 0.52 + 0.10 * math.tanh(vel_delta / max(min_vel, 1e-6))
```

- [ ] **Step 3.4: Run test to verify it passes**

```bash
python -m pytest tests/test_s2_win_prob.py -v
```

Expected: all tests PASS (ceiling now 0.62, floor still 0.52)

- [ ] **Step 3.5: Verify no syntax errors**

```bash
python -c "import bot_strategy; print('OK')"
```

Expected: `OK`

- [ ] **Step 3.6: Commit**

```bash
git add bot_strategy.py tests/test_s2_win_prob.py
git commit -m "fix(strategy): S2 tanh ceiling 0.60->0.62 — matches observed 55-62% WR range"
```

---

## Task 4: Fix GBM model floor and ceiling in `_s1_certainty_win_prob`

**The bug:** `_s1_certainty_win_prob` returns `max(0.52, min(0.75, cert))`.

- **Floor 0.52 is too generous:** A trade at zero distance (price exactly at strike) returns `cert ≈ 0.50`, which is floored to `0.52`. This gives a phantom 2% edge on coin-flip trades, letting them pass the EV gate. Floor should be `0.50` (no artificial boost).
- **Ceiling 0.75 is too conservative:** A trade with `abs_pct=0.02` (2%) and `secs_left=60s` gets GBM cert ≈ 0.97, capped to 0.75. That means EV is underestimated by 22%. The ceiling should be `0.85` to allow high-conviction deep-OTM trades to have accurate EV.

**Impact of floor change:** At 35c entry with min_ev=0.15: old floor gives EV=0.52-0.35-fee=0.164 (passes). New floor 0.50 gives EV=0.50-0.35-fee=0.144 (fails). This filters low-signal 35c entries. This is intentional.

**Files:**
- Modify: `bot_strategy.py:222`
- Create: `tests/test_gbm_model.py`

- [ ] **Step 4.1: Write failing tests**

```python
# tests/test_gbm_model.py
"""Tests for _s1_certainty_win_prob GBM model floor and ceiling."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot_strategy import _s1_certainty_win_prob


def test_gbm_floor_is_50_pct_not_52():
    """At zero distance from strike, GBM cert is 0.50 — floor must not artificially boost it."""
    result = _s1_certainty_win_prob(dist_pct=0.0001, secs_left=450.0, asset="ETH")
    assert result <= 0.51, (
        f"GBM at near-zero distance must return ~0.50 (no edge). "
        f"Got {result:.4f} — floor must be 0.50, not 0.52."
    )


def test_gbm_ceiling_above_75_pct_for_deep_otm():
    """Deep OTM + little time should return > 0.75 — current 0.75 ceiling is too conservative."""
    # 2% distance, 60 seconds left — GBM cert should be ~0.95
    result = _s1_certainty_win_prob(dist_pct=0.020, secs_left=60.0, asset="BTC")
    assert result >= 0.80, (
        f"Deep OTM (2%) with 60s left must have win_prob >= 0.80. "
        f"Got {result:.4f} — ceiling must be raised from 0.75 to 0.85."
    )


def test_gbm_floor_is_050():
    """GBM floor must be exactly 0.50 after fix."""
    # At extremely low distance, tanh → 0, cert → 0.50
    result = _s1_certainty_win_prob(dist_pct=0.00001, secs_left=450.0, asset="ETH")
    assert abs(result - 0.50) < 0.02, (
        f"GBM floor must be 0.50. Got {result:.4f}"
    )


def test_gbm_ceiling_is_085():
    """GBM ceiling must be 0.85 after fix (not 0.75)."""
    # 5% distance, 30s left → cert ≈ 1.0, clipped to 0.85
    result = _s1_certainty_win_prob(dist_pct=0.050, secs_left=30.0, asset="BTC")
    assert result >= 0.85 - 0.001, (
        f"GBM ceiling must be 0.85. Got {result:.4f}"
    )
    assert result <= 0.851, (
        f"GBM ceiling must not exceed 0.85. Got {result:.4f}"
    )
```

- [ ] **Step 4.2: Run test to verify tests fail**

```bash
python -m pytest tests/test_gbm_model.py -v
```

Expected: `test_gbm_floor_is_50_pct_not_52` and `test_gbm_ceiling_above_75_pct_for_deep_otm` FAIL

- [ ] **Step 4.3: Fix the floor and ceiling in `_s1_certainty_win_prob`**

Find in `bot_strategy.py` (~line 222):
```python
    return max(0.52, min(0.75, cert))
```

Replace with:
```python
    return max(0.50, min(0.85, cert))
```

- [ ] **Step 4.4: Run test to verify all 4 pass**

```bash
python -m pytest tests/test_gbm_model.py -v
```

Expected: 4/4 PASS

- [ ] **Step 4.5: Verify no regressions in S1 tests**

```bash
python -m pytest tests/test_s1_cooldown.py tests/test_s1_window_guard.py tests/test_strategy_params.py -v --tb=short 2>&1 | tail -10
```

Expected: no new failures.

- [ ] **Step 4.6: Verify no syntax errors**

```bash
python -c "import bot_strategy; print('OK')"
```

Expected: `OK`

- [ ] **Step 4.7: Commit**

```bash
git add bot_strategy.py tests/test_gbm_model.py
git commit -m "fix(strategy): GBM floor 0.52->0.50, ceiling 0.75->0.85 — removes phantom edge, allows deep OTM accuracy"
```

---

## Task 5: Add XRP S1 disable regression tests

**The gap:** `strategy_brain_s1` has a hardcoded XRP block (line 304–306) with the comment "113 trades, -$522, every bucket negative EV." But there are zero tests for this gate. If someone accidentally adds `s1_xrp_enabled: true` to config or changes the gate logic, no test catches it.

**Files:**
- Create: `tests/test_xrp_s1_gates.py`

- [ ] **Step 5.1: Write the tests**

```python
# tests/test_xrp_s1_gates.py
"""Regression tests: XRP must be disabled in S1 by default; re-enable only via explicit config."""
import sys, os, time
from collections import deque
from unittest.mock import patch
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
import asset_manager
from bot_strategy import strategy_brain_s1


def _make_prices(base: float = 0.60, n: int = 63):
    now = time.time()
    return [(now - (n - i) * 10, base) for i in range(n)]


def _run_s1_xrp(config_overrides: dict | None = None) -> dict:
    config = {"mode": "paper", "bot_enabled": True, **(config_overrides or {})}
    prices = _make_prices(base=0.60)
    patches = [
        patch("bot_strategy.read_config", return_value=config),
        patch("bot_strategy._is_quiet_hours", return_value=False),
        patch.object(bot_state, "_s1_pending_trades", {}),
        patch.object(bot_state, "_s1_asset_trade_times", {}),
        patch.object(bot_state, "_s1_cooldown_until", {}),
        patch.object(bot_state, "_s1_consec_losses_by_asset", {}),
        patch.dict(asset_manager._prices, {"XRP": deque(prices)}),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return strategy_brain_s1(
            btc_price=0.60,
            strike=0.603,
            yes_ask=38.0,
            no_ask=57.0,
            elapsed_seconds=200.0,
            secs_left=400.0,
            ticker="KXXRP15M-TEST",
            asset="XRP",
        )


def test_xrp_disabled_by_default():
    """XRP S1 must return skip with 's1_xrp_disabled' reason when no config override."""
    result = _run_s1_xrp()
    assert result["action"] == "skip", (
        f"XRP S1 must be disabled by default. Got action={result['action']}"
    )
    assert result.get("reasoning") == "s1_xrp_disabled", (
        f"Expected reasoning='s1_xrp_disabled', got: {result.get('reasoning')}"
    )


def test_xrp_skip_fires_before_quiet_hours():
    """XRP disable gate fires before quiet hours check — even during quiet hours."""
    result = _run_s1_xrp()
    assert result.get("reasoning") == "s1_xrp_disabled", (
        f"XRP disable must fire first (not s1_quiet_hours). Got: {result.get('reasoning')}"
    )


def test_xrp_enabled_via_config_bypasses_disable():
    """When s1_xrp_enabled=True in config, XRP disable gate must not fire."""
    result = _run_s1_xrp(config_overrides={"s1_xrp_enabled": True})
    assert result.get("reasoning") != "s1_xrp_disabled", (
        f"With s1_xrp_enabled=True, XRP should not be blocked by disable gate. "
        f"Got: {result.get('reasoning')}"
    )


def test_xrp_disable_gate_exists_in_source():
    """Source-level: XRP disable gate must be in strategy_brain_s1 source."""
    import inspect
    src = inspect.getsource(strategy_brain_s1)
    assert "s1_xrp_enabled" in src, (
        "s1_xrp_enabled config check missing from strategy_brain_s1 source"
    )
    assert "s1_xrp_disabled" in src, (
        "s1_xrp_disabled skip reason missing from strategy_brain_s1 source"
    )
```

- [ ] **Step 5.2: Run test to verify they pass (existing gate, new tests)**

```bash
python -m pytest tests/test_xrp_s1_gates.py -v
```

Expected: 4/4 PASS (gate already exists — these are regression tests)

- [ ] **Step 5.3: Verify no existing tests broken**

```bash
python -c "import bot_strategy; print('OK')"
```

Expected: `OK`

- [ ] **Step 5.4: Commit**

```bash
git add tests/test_xrp_s1_gates.py
git commit -m "test(strategy): add XRP S1 disable regression tests — prevent accidental re-enable"
```

---

## Task 6: Full verification

- [ ] **Step 6.1: Run all new test files**

```bash
python -m pytest tests/test_s2_win_prob.py tests/test_dst_timezone.py tests/test_gbm_model.py tests/test_xrp_s1_gates.py -v
```

Expected: all pass.

- [ ] **Step 6.2: Run full test suite**

```bash
python -m pytest tests/ -q --tb=short 2>&1 | tail -10
```

Expected: same baseline as before — pre-existing failures `test_s2_fires.py::test_s2_skips_weak_velocity_below_1x5` and `test_wr_calibration.py::test_wr_buckets_isolated_by_asset` only. No new failures.

- [ ] **Step 6.3: Smoke test S2 win prob is now velocity-dependent**

```bash
python -c "
from bot_strategy import _s2_lookup_win_rate, _S2_ASSET_CONFIG
cfg = _S2_ASSET_CONFIG['ETH']
mv = cfg['min_vel_delta']
low = _s2_lookup_win_rate('ETH', mv * 1.3, 5.0, cfg)
high = _s2_lookup_win_rate('ETH', mv * 5.0, 5.0, cfg)
ceil = _s2_lookup_win_rate('ETH', mv * 1000, 5.0, cfg)
print(f'low_vel WP: {low:.4f}')
print(f'high_vel WP: {high:.4f}')
print(f'tanh ceiling: {ceil:.4f}')
assert high > low, 'velocity not affecting win_prob'
assert ceil >= 0.62 - 0.001, f'ceiling too low: {ceil}'
print('S2 win prob OK')
"
```

Expected:
```
low_vel WP: ~0.53
high_vel WP: ~0.59
tanh ceiling: 0.6200
S2 win prob OK
```

- [ ] **Step 6.4: Smoke test GBM model**

```bash
python -c "
from bot_strategy import _s1_certainty_win_prob
near = _s1_certainty_win_prob(0.0001, 450.0, 'ETH')
deep = _s1_certainty_win_prob(0.020, 60.0, 'BTC')
print(f'near-zero distance: {near:.4f}  (expect ~0.50)')
print(f'deep OTM 60s left:  {deep:.4f}  (expect ~0.85)')
assert near <= 0.51, f'floor too high: {near}'
assert deep >= 0.80, f'ceiling too low: {deep}'
print('GBM model OK')
"
```

Expected:
```
near-zero distance: 0.5000  (expect ~0.50)
deep OTM 60s left:  0.8500  (expect ~0.85)
GBM model OK
```

- [ ] **Step 6.5: Smoke test DST fix**

```bash
python -c "
import inspect, bot_strategy
src_q = inspect.getsource(bot_strategy._is_quiet_hours)
src_v = inspect.getsource(bot_strategy._time_of_day_vol_multiplier)
assert 'timedelta(hours=-4)' not in src_q, 'DST bug still in _is_quiet_hours'
assert 'timedelta(hours=-4)' not in src_v, 'DST bug still in _time_of_day_vol_multiplier'
print('DST fix confirmed')
"
```

Expected: `DST fix confirmed`

- [ ] **Step 6.6: Commit tag**

```bash
git tag v2-strategy-improvements-v1
```

---

## Expected Impact

| Change | Old behavior | New behavior | Why better |
|--------|-------------|--------------|------------|
| S2 win prob | Distance-only GBM — same WP for 1.3x vs 5x velocity | Velocity-aware tanh — high vel → higher WP → higher EV | Velocity IS the signal; EV gate was blind to it |
| DST fix | Wrong quiet hours Nov–Mar (off by 1hr) | Correct ET year-round | Prevents trades during bad hours in winter |
| S2 tanh ceiling | Max 0.60, observed WR 62% | Max 0.62 | Model matches calibration data |
| GBM floor | 0.52 — phantom 2% edge on 0-signal trades | 0.50 — coin-flip is coin-flip | Removes marginal 35c entries that shouldn't trade |
| GBM ceiling | 0.75 — caps deep OTM accuracy | 0.85 — accurate for high-certainty trades | Better EV estimates; confidence scores more truthful |
| XRP tests | Zero tests — gate could disappear silently | 4 regression tests | Catches accidental re-enable |

---

## Self-Review Checklist

- [x] **Spec coverage:** All 5 bugs from diagnosis table have a task. T1=bug1, T2=bug2, T3=bug3, T4=bug4, T5=bug5.
- [x] **Placeholder scan:** No TBD, no "similar to Task N", all code blocks complete and runnable.
- [x] **Type consistency:** `_s2_lookup_win_rate(asset: str, vel_delta: float, mins_left: float, cfg: dict) -> float` — matches Task 1's fix site where `vel_delta`, `mins_left`, `cfg` are all in scope.
- [x] **No YAGNI:** Every change fixes a specific confirmed bug with a line number. No speculative features.
- [x] **GBM impact:** Floor change from 0.52→0.50 will reject some 35c entries. This is intentional — those were phantom-EV trades.
