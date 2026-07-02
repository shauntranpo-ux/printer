# S2 AMM OBI Gate Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the S2 strategy so it actually fires on Kalshi AMM crypto markets, where OBI is always None because the orderbook endpoint returns empty arrays for AMM contracts.

**Architecture:** One-line fix in `_s2_obi_gate` (bot_strategy.py:421): change `return False, None` to `return True, None`. The S2 win rate tables were calibrated without an OBI gate; the gate should pass (fail-open) when OBI data is unavailable, not block all AMM trades forever. Add an end-to-end test proving S2 fires with None OBI + sufficient velocity.

**Tech Stack:** Python 3.11, asyncio, pytest. No new dependencies.

---

## Background

### What is broken and why

Kalshi 15-minute crypto contracts are AMM (automated market maker) markets. The `/markets/{ticker}/orderbook` endpoint returns empty `yes` and `no` arrays for these. `_kalshi_obi([], [])` returns `None`. `_s2_obi_gate` fails closed on `None` (`return False, None`), so S2 never reaches the EV gate.

```
AMM orderbook  ->  yes_arr=[]  no_arr=[]  ->  _kalshi_obi=None
->  _ticker_obi[ticker]=None
->  _s2_obi_gate returns (False, None)
->  strategy_brain_s2 returns skip/s2_obi_gate
->  S2 never trades
```

The May 8 OBI plan originally designed the gate to fail OPEN (`return True, None`). When implemented, the behaviour was intentionally changed to fail CLOSED with the comment "never allow trades on missing data." This is correct reasoning for traditional markets but wrong for AMM crypto contracts where missing OBI is structural, not a data error.

The S2 win rate tables in `_S2_WIN_RATE` were calibrated via `scripts/calibrate_winrates.py` which does not apply an OBI gate (see `simulate_s2_window` - no OBI filter). So the tables measure velocity-only signal quality. The OBI gate is an EXTRA filter not present in calibration.

### What is NOT broken

- `_kalshi_obi()` computation is correct.
- `ob["obi"]` is computed and stored to `bot_state._ticker_obi[ticker]` after each fetch.
- Velocity signal, bucketing, and win rate lookup are all correct (fixed last session).
- S1 (EMA momentum) fires independently and is unaffected.
- The gate logic is correct for non-None OBI (traditional markets) - only the None branch is wrong.

### Confirmed: S2 does fire when OBI is present

```python
# With bullish OBI (0.15) and velocity delta 1.0c > ETH threshold 0.70c:
strategy_brain_s2(2850, 2800, 72, 28, 760, 240, ticker, asset="ETH")
# -> action=trade, ev=0.241  (4-min window, time_idx=0, WP=0.975)

# With None OBI (AMM reality), same params:
# -> skip s2_obi_gate:obi=None_side=yes_min=0.02
```

---

## File Map

| File | Change |
|------|--------|
| `bot_strategy.py` | `_s2_obi_gate`: change `return False, None` -> `return True, None`; update docstring |
| `tests/test_obi_fix.py` | Rename `test_s2_obi_gate_none_fails_closed` -> `test_s2_obi_gate_none_amm_passes`; flip assertion |
| `tests/test_s2_fires.py` | **Create** - end-to-end test proving S2 fires on AMM market conditions |

---

## Task 1: Flip the failing test (TDD red phase)

**Files:**
- Modify: `tests/test_obi_fix.py`

The existing test `test_s2_obi_gate_none_fails_closed` asserts the broken behaviour. Rename it and flip the assertion first - this creates the failing test we then fix.

- [ ] **Step 1: Read the current test at the bottom of `tests/test_obi_fix.py`**

```python
def test_s2_obi_gate_none_fails_closed():
    """Missing ticker in _ticker_obi -> gate fails closed (False, None) - never trade without OBI data."""
    from bot_strategy import _s2_obi_gate
    ticker = "KXBTC-25MAY15-T99999"
    confirmed, val = _s2_obi_gate(ticker, "yes", 0.20)
    assert confirmed is False
    assert val is None
```

- [ ] **Step 2: Replace it with the correct assertion (AMM passes)**

Find and replace the entire `test_s2_obi_gate_none_fails_closed` function:

```python
def test_s2_obi_gate_none_amm_passes():
    """Missing ticker OBI (AMM market) -> gate passes (True, None) - AMM has no real order depth."""
    from bot_strategy import _s2_obi_gate
    ticker = "KXBTC-25MAY15-T99999"
    confirmed, val = _s2_obi_gate(ticker, "yes", 0.20)
    assert confirmed is True
    assert val is None
```

- [ ] **Step 3: Run the updated test to confirm it fails**

```
python -m pytest tests/test_obi_fix.py::test_s2_obi_gate_none_amm_passes -v
```

Expected: FAIL with `AssertionError: assert False is True`.

---

## Task 2: Fix `_s2_obi_gate` in `bot_strategy.py`

**Files:**
- Modify: `bot_strategy.py`

One-line change in `_s2_obi_gate`. The function is at line 413.

- [ ] **Step 1: Replace the function docstring and None branch**

Current code (lines 413-427):

```python
def _s2_obi_gate(ticker: str, side: str, min_obi: float):
    """
    OBI confirmation gate for S2.
    Returns (confirmed, obi_val).
    Fails closed (False) when no OBI data for this ticker - never allow trades on missing data.
    Positive OBI = no_depth > yes_depth = bullish for YES.
    """
    obi_val = bot_state._ticker_obi.get(ticker)
    if obi_val is None:
        return False, None
    if side == "yes" and obi_val <= min_obi:
        return False, obi_val
    if side == "no"  and obi_val >= -min_obi:
        return False, obi_val
    return True, obi_val
```

Replace with:

```python
def _s2_obi_gate(ticker: str, side: str, min_obi: float):
    """
    OBI confirmation gate for S2.
    Returns (confirmed, obi_val).
    Passes (True, None) when no OBI data - Kalshi AMM markets always return empty orderbook arrays,
    so None OBI is structural, not a data error. S2 win rate tables were calibrated without OBI gate.
    Positive OBI = no_depth > yes_depth = bullish for YES.
    """
    obi_val = bot_state._ticker_obi.get(ticker)
    if obi_val is None:
        return True, None
    if side == "yes" and obi_val <= min_obi:
        return False, obi_val
    if side == "no"  and obi_val >= -min_obi:
        return False, obi_val
    return True, obi_val
```

- [ ] **Step 2: Run the now-fixed test**

```
python -m pytest tests/test_obi_fix.py -v
```

Expected: 8 PASS (all OBI fix tests). If `test_s2_obi_gate_bullish_ticker` or `test_s2_obi_gate_bearish_ticker` fail, verify the non-None OBI logic is unchanged.

- [ ] **Step 3: Run the full suite**

```
python -m pytest tests/ -q --tb=short
```

Expected: 341 passed, 0 failures (same as pre-change baseline). No other test references `_s2_obi_gate` behaviour for None OBI.

- [ ] **Step 4: Commit**

```bash
git add bot_strategy.py tests/test_obi_fix.py
git commit -m "fix(strategy): S2 OBI gate fails open for AMM markets -- None OBI no longer blocks S2"
```

---

## Task 3: Add end-to-end test - S2 fires on AMM conditions

**Files:**
- Create: `tests/test_s2_fires.py`

This test proves the full pipeline: with AMM OBI (None), sufficient velocity, and correct params, `strategy_brain_s2` returns `action=trade`.

- [ ] **Step 1: Create `tests/test_s2_fires.py`**

```python
"""
End-to-end tests: strategy_brain_s2 fires on realistic AMM market conditions.

These tests prove the full signal pipeline - velocity accumulation, OBI handling,
win rate lookup, EV gate - without mocking the brain logic itself.
"""
import time
import collections
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
from bot_strategy import strategy_brain_s2, _S2_ASSET_CONFIG


@pytest.fixture(autouse=True)
def _clean_state():
    """Wipe velocity history and OBI state between tests."""
    bot_state._contract_price_history.clear()
    bot_state._ticker_obi.clear()
    yield
    bot_state._contract_price_history.clear()
    bot_state._ticker_obi.clear()


def _seed_velocity(ticker: str, asset: str, direction: str = "yes"):
    """
    Seed contract price history with a strong enough velocity signal.

    ETH min_vel_delta=0.70c, lookback=4 -> needs 5 data points.
    We use 0.40c per step: first_avg of [70, 70.4, 70.8] = 70.4,
    second_avg of [71.2, 71.6] = 71.4 -> delta = 1.0c > 0.70 threshold.

    For 'no' direction, prices fall: 72 -> 70.
    """
    cfg = _S2_ASSET_CONFIG[asset]
    lookback = cfg["vel_lookback"]
    min_vel  = cfg["min_vel_delta"]
    # step size: 1.5x min_vel_delta spread over lookback, guarantees ratio > 1.0
    step = (min_vel * 1.5) / max(lookback, 1)
    base = 70.0
    history = collections.deque(maxlen=60)
    if direction == "yes":
        prices = [base + i * step for i in range(lookback + 1)]
    else:
        prices = [base + lookback * step - i * step for i in range(lookback + 1)]
    now = time.time()
    for i, p in enumerate(prices):
        history.append((now - (lookback - i) * 10, p))
    bot_state._contract_price_history[ticker] = history


class TestS2FiresETH:
    """S2 fires for ETH - 4-minute window, above-strike continuation."""

    def test_s2_fires_amm_obi_none(self):
        """S2 fires when OBI=None (AMM market), velocity strong enough."""
        ticker = "KXETH-25MAY30-T2800"
        _seed_velocity(ticker, "ETH", direction="yes")
        # AMM: OBI not stored -> defaults to None
        result = strategy_brain_s2(
            btc_price=2850.0,    # ETH above strike
            strike=2800.0,
            yes_ask=72.0,        # in [20, 76] range
            no_ask=28.0,
            elapsed_seconds=760.0,
            secs_left=240.0,     # 4 min -> time_idx=0, WP~0.975
            ticker=ticker,
            asset="ETH",
        )
        assert result["action"] == "trade", (
            f"S2 should trade but got: {result['reasoning']}\n"
            "Likely OBI gate still fails closed - check _s2_obi_gate None branch."
        )
        assert result["side"] == "yes"
        assert result["win_prob"] > 0.90

    def test_s2_fires_with_explicit_none_obi(self):
        """S2 fires when _ticker_obi[ticker] is explicitly set to None (AMM fetch path)."""
        ticker = "KXETH-25MAY30-T2800B"
        _seed_velocity(ticker, "ETH", direction="yes")
        bot_state._ticker_obi[ticker] = None  # simulates AMM fetch writing None
        result = strategy_brain_s2(
            btc_price=2850.0,
            strike=2800.0,
            yes_ask=72.0,
            no_ask=28.0,
            elapsed_seconds=760.0,
            secs_left=240.0,
            ticker=ticker,
            asset="ETH",
        )
        assert result["action"] == "trade", (
            f"S2 should trade with explicit None OBI: {result['reasoning']}"
        )

    def test_s2_skips_when_velocity_below_threshold(self):
        """S2 skips when velocity delta < min_vel_delta (not enough momentum)."""
        ticker = "KXETH-25MAY30-T2800C"
        cfg = _S2_ASSET_CONFIG["ETH"]
        # Tiny velocity: 0.05c per step, well below min_vel_delta=0.70
        history = collections.deque(maxlen=60)
        now = time.time()
        for i in range(6):
            history.append((now - (5 - i) * 10, 70.0 + i * 0.05))
        bot_state._contract_price_history[ticker] = history
        result = strategy_brain_s2(
            btc_price=2850.0,
            strike=2800.0,
            yes_ask=72.0,
            no_ask=28.0,
            elapsed_seconds=760.0,
            secs_left=240.0,
            ticker=ticker,
            asset="ETH",
        )
        assert result["action"] == "skip"
        assert "s2_vel_flat" in result["reasoning"] or "s2_no_velocity_data" in result["reasoning"]

    def test_s2_skips_reversal_price_below_strike(self):
        """S2 velocity=yes but price below strike -> reversal gate skips."""
        ticker = "KXETH-25MAY30-T2900D"
        _seed_velocity(ticker, "ETH", direction="yes")
        result = strategy_brain_s2(
            btc_price=2850.0,    # ETH BELOW strike 2900
            strike=2900.0,
            yes_ask=38.0,
            no_ask=62.0,
            elapsed_seconds=760.0,
            secs_left=240.0,
            ticker=ticker,
            asset="ETH",
        )
        assert result["action"] == "skip"
        assert "s2_reversal_gate" in result["reasoning"]

    def test_s2_skips_when_ev_negative(self):
        """S2 skips when win_prob - entry/100 - fee < min_ev."""
        ticker = "KXETH-25MAY30-T2800E"
        _seed_velocity(ticker, "ETH", direction="yes")
        # High entry price (late time = low WP) -> negative EV
        result = strategy_brain_s2(
            btc_price=2850.0,
            strike=2800.0,
            yes_ask=75.0,        # near ceiling, WP in time_idx=2 bucket ~0.63 -> EV negative
            no_ask=25.0,
            elapsed_seconds=150.0,
            secs_left=600.0,     # 10 min -> time_idx=2
            ticker=ticker,
            asset="ETH",
        )
        assert result["action"] == "skip"
        assert "s2_ev_gate" in result["reasoning"] or "s2_vel_flat" in result["reasoning"]


class TestS2FiresMultiAsset:
    """S2 fires for all enabled assets with their calibrated thresholds."""

    @pytest.mark.parametrize("asset,strike,price,yes_ask", [
        ("ETH",  2800.0, 2850.0, 72.0),
        ("SOL",  145.0,  148.0,  72.0),
        ("XRP",  2.30,   2.35,   72.0),
    ])
    def test_s2_fires_per_asset(self, asset, strike, price, yes_ask):
        """Each enabled asset's S2 fires with 4-min window and strong velocity."""
        ticker = f"KXMOCK-{asset}-T{int(strike)}"
        _seed_velocity(ticker, asset, direction="yes")
        result = strategy_brain_s2(
            btc_price=price,
            strike=strike,
            yes_ask=yes_ask,
            no_ask=100 - yes_ask,
            elapsed_seconds=760.0,
            secs_left=240.0,    # 4 min -> time_idx=0
            ticker=ticker,
            asset=asset,
        )
        assert result["action"] == "trade", (
            f"S2 failed to fire for {asset}: {result['reasoning']}"
        )
```

- [ ] **Step 2: Run the new tests to verify they all pass**

```
python -m pytest tests/test_s2_fires.py -v
```

Expected: 8 PASS. If any test fails:
- `test_s2_fires_amm_obi_none` fails -> Task 2 fix not applied
- `test_s2_skips_when_velocity_below_threshold` fails -> velocity seed logic broken, re-check `_seed_velocity` step calculation
- `test_s2_fires_per_asset[SOL]` fails -> check SOL `min_vel_delta=1.20`, step may need to be larger

- [ ] **Step 3: Run the full suite**

```
python -m pytest tests/ -q --tb=short
```

Expected: all tests pass (341 + 8 new = 349 total).

- [ ] **Step 4: Commit**

```bash
git add tests/test_s2_fires.py
git commit -m "test(strategy): prove S2 fires on AMM conditions + velocity/reversal/ev gate coverage"
```

---

## Task 4: Verify and push

- [ ] **Step 1: Run full suite one more time**

```
python -m pytest tests/ -q
```

Expected: 349+ passed, 0 failures.

- [ ] **Step 2: Push to GitHub**

```bash
git push origin main
```

Expected: Railway auto-deploys. Monitor Telegram for first S2 trade notification after deployment.

---

## Self-Review

**Spec coverage:**

| Requirement | Task |
|---|---|
| OBI gate passes for AMM markets (None OBI) | Task 2 |
| OBI gate still blocks non-confirming non-None OBI | Task 2 (existing tests preserved) |
| Test proves S2 fires end-to-end with None OBI | Task 3 |
| Test proves S2 skips on weak velocity | Task 3 |
| Test proves S2 skips on reversal | Task 3 |
| Test proves S2 skips on negative EV | Task 3 |
| Test proves multi-asset S2 fires (ETH/SOL/XRP) | Task 3 |

**Placeholder scan:** No TBD, TODO, or "similar to Task N" in this plan. All code blocks are complete.

**Type consistency:** `_s2_obi_gate(ticker: str, side: str, min_obi: float)` - signature unchanged. Call site in `strategy_brain_s2` already uses `ticker`. No signature drift.

**What did NOT change:**
- S2 calibration tables (`_S2_WIN_RATE`) - already valid, calibrated without OBI gate
- S2 velocity logic (`_s2_contract_direction`) - correct
- S1 strategy - unaffected
- All other S2 gates (dist, time, reversal, EV) - unchanged
