# Strategy Overhaul: S1 Momentum + S2 Conviction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace S1's broken EMA signal with a 60-second raw momentum + geometric certainty model, add S2 conviction gate (3× velocity minimum), and raise max_entry to 55c for both strategies.

**Architecture:** S1's EMA crossover had ~50% real WR (coin flip) because by the time the EMA crosses, the Kalshi AMM has already repriced. The new signal fires on RECENT 60-second price momentum — catching AMM lag before it fully reprices. Win probability comes from a geometric Brownian motion certainty model (dist × time), not look-ahead-biased empirical tables. S2 keeps its velocity direction signal but adds a 3× conviction gate so only strong, unambiguous moves fire trades. Both use the same geometric certainty model for win_prob calculation.

**Tech Stack:** Python 3.12, bot_strategy.py (single file change), pytest

---

## File Structure

| File | Change |
|---|---|
| `bot_strategy.py` | Add `_s1_momentum_direction()`, add `_s1_certainty_win_prob()`, update `strategy_brain_s1` to use them, add S2 conviction gate, update S2 win_prob, raise max_entry to 55c |
| `tests/test_strategy_params.py` | Remove stale EMA calibration tests; add momentum threshold test |
| `tests/test_strategy_fix.py` | Update max_entry assertion (50→55c cap) |
| `tests/test_profitability_overhaul.py` | Update max_entry assertion |
| `tests/test_s2_fires.py` | Update yes_ask from 45c to 50c (new 55c cap allows it) |

---

## Task 1: Raise max_entry to 55c for both strategies

**Files:**
- Modify: `bot_strategy.py:256` (S1), `bot_strategy.py:512` (S2)
- Test: `tests/test_strategy_fix.py`, `tests/test_profitability_overhaul.py`

- [ ] **Step 1: Write the failing tests**

```python
# In tests/test_strategy_fix.py, update test_s1_max_entry_price_capped_for_profitability:
def test_s1_max_entry_price_capped_for_profitability():
    """S1 max_entry_price default must be 50-60c — uncertainty zone at realistic WR."""
    import re
    with open('bot_strategy.py', encoding='utf-8') as f:
        src = f.read()
    s1_section = src[src.index('def strategy_brain_s1'):src.index('def strategy_brain_s2')]
    defaults = re.findall(r'max_entry_price_cents",\s*([\d.]+)', s1_section)
    assert defaults, "max_entry_price_cents default not found in strategy_brain_s1"
    for d in defaults:
        assert 50.0 <= float(d) <= 60.0, \
            f"S1 max_entry_price {d} outside 50-60c range"

def test_s2_max_entry_price_capped_for_profitability():
    """S2 max_entry_price default must be 50-60c."""
    import re
    with open('bot_strategy.py', encoding='utf-8') as f:
        src = f.read()
    s2_section = src[src.index('def strategy_brain_s2'):]
    defaults = re.findall(r'max_entry_price_cents",\s*([\d.]+)', s2_section)
    assert defaults, "max_entry_price_cents default not found in strategy_brain_s2"
    for d in defaults:
        assert 50.0 <= float(d) <= 60.0, \
            f"S2 max_entry_price {d} outside 50-60c range"

# In tests/test_profitability_overhaul.py, update test_max_entry_price_caps_are_profitable:
def test_max_entry_price_caps_are_profitable():
    """S1/S2 max entry 50-60c — realistic 57-65% WR is profitable in this range."""
    import re
    with open('bot_strategy.py', encoding='utf-8') as f:
        src = f.read()
    s1 = src[src.index('def strategy_brain_s1'):src.index('def strategy_brain_s2')]
    s2 = src[src.index('def strategy_brain_s2'):]
    for d in re.findall(r'max_entry_price_cents",\s*([\d.]+)', s1):
        assert 50.0 <= float(d) <= 60.0, f"S1 max_entry {d}c outside 50-60c range"
    for d in re.findall(r'max_entry_price_cents",\s*([\d.]+)', s2):
        assert 50.0 <= float(d) <= 60.0, f"S2 max_entry {d}c outside 50-60c range"
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/test_strategy_fix.py::test_s1_max_entry_price_capped_for_profitability tests/test_strategy_fix.py::test_s2_max_entry_price_capped_for_profitability tests/test_profitability_overhaul.py::test_max_entry_price_caps_are_profitable -v
```
Expected: FAIL (current default is 50.0, tests now require 50.0-60.0 range — actually 50 passes the ≥50 part; need to check. Run to confirm exact failure.)

- [ ] **Step 3: Change max_entry defaults to 55c in bot_strategy.py**

In `bot_strategy.py`, find and change these two lines:

Line ~256 (S1 gate, comment says "50c max"):
```python
    # Gate 5: entry price range — 55c max: market-uncertainty zone, 57%+ WR profitable
    _min_p = float(get_asset_config(config, asset, "min_entry_price_cents", 20.0))
    _max_p = float(get_asset_config(config, asset, "max_entry_price_cents", 55.0))
```

Line ~512 (S2 gate, comment says "50c max"):
```python
    # Gate 3: entry price range — 55c max filters out expensive trades beyond uncertainty zone
    _min_p = float(get_asset_config(config, asset, "min_entry_price_cents", 20.0))
    _max_p = float(get_asset_config(config, asset, "max_entry_price_cents", 55.0))
```

- [ ] **Step 4: Run tests to verify they pass**

```
python -m pytest tests/test_strategy_fix.py::test_s1_max_entry_price_capped_for_profitability tests/test_strategy_fix.py::test_s2_max_entry_price_capped_for_profitability tests/test_profitability_overhaul.py::test_max_entry_price_caps_are_profitable -v
```
Expected: PASS

- [ ] **Step 5: Run full suite to check nothing else broke**

```
python -m pytest tests/ -q
```
Expected: all pass (test_s2_fires.py uses 45c entries which still pass 55c cap)

- [ ] **Step 6: Commit**

```bash
git add bot_strategy.py tests/test_strategy_fix.py tests/test_profitability_overhaul.py
git commit -m "feat(strategy): raise max_entry to 55c for S1 and S2"
```

---

## Task 2: Add `_s1_momentum_direction` and `_s1_certainty_win_prob` functions

**Files:**
- Modify: `bot_strategy.py` — add two new functions after line 173 (after `_s1_ema_direction`)
- Test: `tests/test_strategy_params.py`

**Why:** `_s1_ema_direction` smooths over 3-10 minutes, missing AMM lag windows. `_s1_momentum_direction` compares now vs 60s ago — catches a move within seconds of it starting. `_s1_certainty_win_prob` uses actual geometric probability (dist/vol/time) instead of look-ahead-biased WR tables.

- [ ] **Step 1: Write failing tests for both new functions**

Add to `tests/test_strategy_params.py`:

```python
def test_s1_momentum_direction_detects_up_move():
    """_s1_momentum_direction returns ('yes', pct) on rising prices."""
    import time
    from bot_strategy import _s1_momentum_direction
    now = time.time()
    # 61s of data: price rose from 2500 to 2510 (+0.4%)
    prices = [(now - 61 + i, 2500 + i * (10/60)) for i in range(62)]
    side, mom = _s1_momentum_direction(prices, window_seconds=60.0, min_momentum=0.001)
    assert side == "yes", f"Expected 'yes' on rising prices, got {side}"
    assert mom is not None and mom > 0.001, f"momentum {mom} below threshold"


def test_s1_momentum_direction_returns_none_on_flat():
    """_s1_momentum_direction returns (None, None) when move < min_momentum."""
    import time
    from bot_strategy import _s1_momentum_direction
    now = time.time()
    # Flat prices — 0.01% movement
    prices = [(now - 61 + i, 2500 + i * 0.004) for i in range(62)]
    side, mom = _s1_momentum_direction(prices, window_seconds=60.0, min_momentum=0.002)
    assert side is None, f"Expected None on flat prices, got {side}"


def test_s1_certainty_win_prob_increases_with_dist():
    """Geometric certainty: farther from strike = higher WR."""
    from bot_strategy import _s1_certainty_win_prob
    wp_close = _s1_certainty_win_prob(0.002, 480.0, "ETH")
    wp_far   = _s1_certainty_win_prob(0.008, 480.0, "ETH")
    assert wp_far > wp_close, f"WR should increase with distance: {wp_close:.3f} vs {wp_far:.3f}"


def test_s1_certainty_win_prob_increases_with_less_time():
    """Geometric certainty: less time left = higher WR (less time to cross back)."""
    from bot_strategy import _s1_certainty_win_prob
    wp_more_time = _s1_certainty_win_prob(0.004, 600.0, "ETH")
    wp_less_time = _s1_certainty_win_prob(0.004, 180.0, "ETH")
    assert wp_less_time > wp_more_time, \
        f"WR should be higher with less time: {wp_more_time:.3f} vs {wp_less_time:.3f}"


def test_s1_certainty_win_prob_range():
    """WR must stay in 0.52-0.75 range — no fantasy numbers."""
    from bot_strategy import _s1_certainty_win_prob
    for dist in [0.001, 0.005, 0.010, 0.030]:
        for t in [60, 300, 600, 840]:
            wp = _s1_certainty_win_prob(dist, float(t), "ETH")
            assert 0.52 <= wp <= 0.75, f"WR={wp:.3f} out of range at dist={dist} t={t}"
```

- [ ] **Step 2: Run tests to confirm they fail**

```
python -m pytest tests/test_strategy_params.py::test_s1_momentum_direction_detects_up_move tests/test_strategy_params.py::test_s1_certainty_win_prob_range -v
```
Expected: FAIL with ImportError (`_s1_momentum_direction` not defined yet)

- [ ] **Step 3: Add the two new functions to bot_strategy.py**

Insert immediately after the closing of `_s1_ema_direction` (after line 173), before `def strategy_brain_s1`:

```python
def _s1_momentum_direction(prices: list, window_seconds: float = 60.0, min_momentum: float = 0.003):
    """
    60-second raw momentum direction pointer.
    Compares current price to the average price ~window_seconds ago.
    Returns (side, momentum_pct): side='yes' if up, 'no' if down.
    Returns (None, None) when data insufficient or move below min_momentum.
    """
    if not prices or len(prices) < 4:
        return None, None
    now_ts  = prices[-1][0]
    current = float(prices[-1][1])
    # Find prices in a 20-second band centred on window_seconds ago
    lo = now_ts - window_seconds - 10
    hi = now_ts - window_seconds + 10
    older = [float(p) for ts, p in prices if lo <= ts <= hi]
    if not older:
        return None, None
    past_price = sum(older) / len(older)
    if past_price <= 0:
        return None, None
    momentum = (current - past_price) / past_price
    if abs(momentum) < min_momentum:
        return None, abs(momentum)
    return ("yes" if momentum > 0 else "no"), abs(momentum)


def _s1_certainty_win_prob(dist_pct: float, secs_left: float, asset: str) -> float:
    """
    Geometric Brownian Motion certainty model.
    Estimates P(price stays on current side of strike until settlement).
    Anchored to empirical 15-min vol per asset. Capped at 0.52-0.75.
    """
    # Empirical 15-min 1-sigma move as fraction of price
    _ASSET_VOL_15M = {
        "BTC": 0.008, "ETH": 0.007, "SOL": 0.012, "XRP": 0.010, "DOGE": 0.015,
    }
    vol_15m = _ASSET_VOL_15M.get(asset, 0.008)
    time_frac  = max(0.01, secs_left / 900.0)
    period_vol = vol_15m * math.sqrt(time_frac)
    z    = dist_pct / period_vol
    cert = 0.5 * (1.0 + math.erf(z / math.sqrt(2)))
    return max(0.52, min(0.75, cert))
```

- [ ] **Step 4: Run tests to confirm they pass**

```
python -m pytest tests/test_strategy_params.py::test_s1_momentum_direction_detects_up_move tests/test_strategy_params.py::test_s1_momentum_direction_returns_none_on_flat tests/test_strategy_params.py::test_s1_certainty_win_prob_increases_with_dist tests/test_strategy_params.py::test_s1_certainty_win_prob_increases_with_less_time tests/test_strategy_params.py::test_s1_certainty_win_prob_range -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bot_strategy.py tests/test_strategy_params.py
git commit -m "feat(strategy): add _s1_momentum_direction and _s1_certainty_win_prob"
```

---

## Task 3: Wire new S1 signal into strategy_brain_s1

**Files:**
- Modify: `bot_strategy.py` — `strategy_brain_s1` function, `_S1_ASSET_CONFIG`
- Test: `tests/test_strategy_params.py`, `tests/test_cleanup_b.py`

**Why:** Replace the EMA call at line ~240 with the momentum call. Replace `_s1_lookup_win_rate` at line ~263 with `_s1_certainty_win_prob`. Update `_S1_ASSET_CONFIG` to have `min_momentum` instead of `ema_short`/`ema_long`. Remove the rv gate (realized vol ceiling — not needed with momentum signal; momentum already reflects recent vol).

- [ ] **Step 1: Write failing test for wired-up S1 signal**

Add to `tests/test_strategy_params.py`:

```python
def test_s1_fires_on_momentum_not_ema():
    """strategy_brain_s1 must use momentum signal — fires when 60s price moved up."""
    import time
    from collections import deque
    import bot_state
    from bot_strategy import strategy_brain_s1

    asset = "ETH"
    # Seed prices with a clear upward 60-second move (~0.4%)
    now = time.time()
    prices = deque(maxlen=2000)
    base = 2510.0
    strike = 2500.0
    for i in range(90):
        ts = now - 90 + i
        # Flat for first 30s, then rose 0.4% over last 60s
        if i < 30:
            prices.append((ts, base))
        else:
            prices.append((ts, base + (i - 30) * (10.0 / 60)))
    bot_state._prices[asset] = prices

    result = strategy_brain_s1(
        btc_price=float(prices[-1][1]),
        strike=strike,
        yes_ask=48.0,
        no_ask=52.0,
        elapsed_seconds=300.0,
        secs_left=480.0,
        ticker="KXETH-TEST",
        asset=asset,
    )
    assert result["action"] == "trade", (
        f"S1 should fire on 60s upward momentum above strike, got: {result['reasoning']}"
    )
    assert result["side"] == "yes"


def test_s1_skips_when_momentum_flat():
    """strategy_brain_s1 skips when 60s price change is below min_momentum."""
    import time
    from collections import deque
    import bot_state
    from bot_strategy import strategy_brain_s1

    asset = "ETH"
    now = time.time()
    prices = deque(maxlen=2000)
    # Flat prices — no momentum
    for i in range(90):
        prices.append((now - 90 + i, 2510.0 + i * 0.001))  # 0.009% over 90s — below threshold
    bot_state._prices[asset] = prices

    result = strategy_brain_s1(
        btc_price=2510.0,
        strike=2500.0,
        yes_ask=48.0,
        no_ask=52.0,
        elapsed_seconds=300.0,
        secs_left=480.0,
        ticker="KXETH-TEST-FLAT",
        asset=asset,
    )
    assert result["action"] == "skip"
    assert "s1_momentum" in result["reasoning"], f"Expected momentum skip, got: {result['reasoning']}"
```

- [ ] **Step 2: Run to confirm failure**

```
python -m pytest tests/test_strategy_params.py::test_s1_fires_on_momentum_not_ema -v
```
Expected: FAIL (currently S1 still uses EMA)

- [ ] **Step 3: Update `_S1_ASSET_CONFIG` — replace ema_short/ema_long with min_momentum**

In `bot_strategy.py` lines 93-106, replace the entire `_S1_ASSET_CONFIG`:

```python
_S1_ASSET_CONFIG: dict = {
    #           min_dist  max_rv  min_momentum  min_ev  t_min  t_max
    # min_dist raised: only trade when price is meaningfully far from strike.
    # min_momentum: 60-second price change required to confirm recent directional move.
    # time_min=1.0: skip final minute (wide spreads, AMM settlement chaos).
    # time_max=12.0: skip very early (AMM hasn't had time to anchor contract price).
    "BTC":  dict(min_dist=0.0030, max_rv=1.0, min_momentum=0.0030, min_ev=0.04, time_min=1.0, time_max=12.0),
    "ETH":  dict(min_dist=0.0030, max_rv=1.0, min_momentum=0.0025, min_ev=0.04, time_min=1.0, time_max=12.0),
    "SOL":  dict(min_dist=0.0050, max_rv=1.0, min_momentum=0.0040, min_ev=0.04, time_min=1.0, time_max=12.0),
    "XRP":  dict(min_dist=0.0030, max_rv=1.0, min_momentum=0.0025, min_ev=0.04, time_min=1.0, time_max=12.0),
    "DOGE": dict(min_dist=0.0070, max_rv=1.0, min_momentum=0.0050, min_ev=0.04, time_min=1.0, time_max=12.0),
}
```

- [ ] **Step 4: Replace S1 signal and win_prob in `strategy_brain_s1`**

In `bot_strategy.py`, find the block starting at line ~235 and ending at ~314. Replace the direction pointer block and the return statements:

**Replace** the rv gate + EMA direction block + reversal gate + win_prob call + both return blocks (lines ~235-314) with:

```python
    # Direction pointer: 60-second raw momentum (catches AMM lag after fast moves)
    direction, momentum_pct = _s1_momentum_direction(
        prices_list, window_seconds=60.0, min_momentum=cfg["min_momentum"]
    )
    if direction is None:
        _reason = "s1_no_momentum_data" if momentum_pct is None else f"s1_momentum_flat:{momentum_pct:.4f}<{cfg['min_momentum']}"
        return _make_skip("yes", _reason, abs_pct, mins_left, variant="strategy1")

    side = direction  # 'yes' = bullish, 'no' = bearish
    entry_price = yes_ask if side == "yes" else no_ask

    # Continuation-only: momentum direction must match price position vs strike.
    # Upward momentum but price below strike = reversal bet → skip (coin flip).
    if side == "yes" and current_price < strike:
        return _make_skip(side, "s1_reversal_gate:mom=yes_price_below", abs_pct, mins_left, variant="strategy1")
    if side == "no" and current_price > strike:
        return _make_skip(side, "s1_reversal_gate:mom=no_price_above", abs_pct, mins_left, variant="strategy1")

    # Gate 5: entry price range — 55c max: market-uncertainty zone, 57%+ WR profitable
    _min_p = float(get_asset_config(config, asset, "min_entry_price_cents", 20.0))
    _max_p = float(get_asset_config(config, asset, "max_entry_price_cents", 55.0))
    if entry_price < _min_p or entry_price > _max_p:
        return _make_skip(side, f"s1_price_filter:{entry_price:.0f}c", abs_pct, mins_left,
                          variant="strategy1", price_filter=True)

    # Win probability: geometric certainty model (dist + time → GBM probability)
    win_prob = _s1_certainty_win_prob(abs_pct, secs_left, asset)

    # EV gate
    _ep_s1 = entry_price / 100.0
    _fee_cents_s1 = config.get("kalshi_fee_per_contract_cents", 7)
    fee = (_fee_cents_s1 / 100) * _ep_s1 * (1.0 - _ep_s1)
    ev = win_prob - _ep_s1 - fee
    if ev < cfg["min_ev"]:
        return {
            "action": "skip", "side": side,
            "confidence": int(win_prob * 100),
            "reasoning": f"s1_ev_gate:{ev:.3f}<{cfg['min_ev']:.3f}",
            "key_signals": [f"ev:{ev:.3f}", f"wp:{win_prob:.3f}", f"mom:{direction}"],
            "signals": {"win_prob": win_prob, "ev": ev, "momentum_pct": momentum_pct,
                        "abs_pct": abs_pct, "strike": strike},
            "win_prob": float(win_prob), "mom_label": direction,
            "mom_pct": float(momentum_pct or 0.0),
            "vel_signal": "neutral",
            "raw_p_yes": float(win_prob) if side == "yes" else float(1.0 - win_prob),
            "mins_left": mins_left, "abs_pct": abs_pct, "above": side == "yes",
            "_rv": None, "_vol_ratio": None, "price_filter_skip": False,
            "strategy_variant": "strategy1",
        }

    brain_log.info(
        "S1 TRADE %s %s | mom=%s pct=%.4f dist=%.4f ev=%.3f wp=%.3f mins=%.1f",
        asset, ticker, direction, momentum_pct or 0, abs_pct, ev, win_prob, mins_left,
    )
    return {
        "action": "trade", "side": side,
        "confidence": int(win_prob * 100),
        "reasoning": (
            f"s1_mom ev={ev:.3f} wp={win_prob:.3f} mom={direction} "
            f"dist={abs_pct:.3%} mom_pct={momentum_pct:.4f} mins={mins_left:.1f}"
        ),
        "key_signals": [
            f"ev:{ev:.3f}", f"wp:{win_prob:.3f}", f"mom:{direction}",
            f"dist:{abs_pct:.3%}", f"mom_pct:{momentum_pct:.4f}",
        ],
        "signals": {
            "win_prob": win_prob, "ev": ev, "momentum_pct": momentum_pct,
            "abs_pct": abs_pct, "mins_left": mins_left, "strike": strike,
        },
        "win_prob": float(win_prob), "mom_label": direction,
        "mom_pct": float(momentum_pct or 0.0),
        "vel_signal": "neutral",
        "raw_p_yes": float(win_prob) if side == "yes" else float(1.0 - win_prob),
        "mins_left": mins_left, "abs_pct": abs_pct, "above": side == "yes",
        "_rv": None, "_vol_ratio": None, "price_filter_skip": False,
        "strategy_variant": "strategy1",
    }
```

Note: also remove the rv gate block at lines ~232-237 (the `rv = _realized_vol(...)` and `if rv > cfg["max_rv"]` check) — momentum already reflects volatility direction; a separate rv ceiling is redundant.

- [ ] **Step 5: Fix `tests/test_strategy_params.py` — remove stale EMA tests, add momentum test**

Remove these three functions (they test ema_short/ema_long/min_dist against calibration — now stale):
- `test_s1_ema_long_matches_calibration`
- `test_s1_ema_short_matches_calibration`
- `test_s1_min_dist_matches_calibration`

Replace CALIBRATION_S1 dict with:

```python
CALIBRATION_S1 = {
    "BTC":  dict(min_dist=0.0030, min_momentum=0.0030),
    "ETH":  dict(min_dist=0.0030, min_momentum=0.0025),
    "SOL":  dict(min_dist=0.0050, min_momentum=0.0040),
    "XRP":  dict(min_dist=0.0030, min_momentum=0.0025),
    "DOGE": dict(min_dist=0.0070, min_momentum=0.0050),
}
```

Add this test:

```python
def test_s1_momentum_thresholds_match_calibration():
    """Live S1 min_momentum must match calibration values."""
    for asset, cal in CALIBRATION_S1.items():
        live = _S1_ASSET_CONFIG[asset]
        assert abs(live["min_momentum"] - cal["min_momentum"]) < 1e-9, (
            f"{asset}: live min_momentum={live['min_momentum']} != {cal['min_momentum']}"
        )

def test_s1_min_dist_matches_calibration():
    """Live S1 min_dist must match calibration values."""
    for asset, cal in CALIBRATION_S1.items():
        live = _S1_ASSET_CONFIG[asset]
        assert abs(live["min_dist"] - cal["min_dist"]) < 1e-9, (
            f"{asset}: live min_dist={live['min_dist']} != {cal['min_dist']}"
        )
```

- [ ] **Step 6: Run full test suite**

```
python -m pytest tests/ -q
```
Expected: all pass. Fix any failures before continuing — likely `test_cleanup_b.py` or `test_strategy_fix.py` referencing `ema` in reasoning strings.

- [ ] **Step 7: Commit**

```bash
git add bot_strategy.py tests/test_strategy_params.py
git commit -m "feat(strategy): replace S1 EMA signal with 60s momentum + geometric certainty WR"
```

---

## Task 4: Add S2 conviction gate + update S2 win_prob

**Files:**
- Modify: `bot_strategy.py` — `strategy_brain_s2` function

**Why:** S2 currently fires at 1.5× minimum velocity (barely above noise). Adding a 3× conviction gate means only strong, unambiguous contract momentum fires. The certainty model replaces the tanh WR formula — S2 win_prob now comes from dist+time, with velocity only qualifying the direction signal.

- [ ] **Step 1: Write failing test**

Add to `tests/test_s2_fires.py`:

```python
def test_s2_skips_weak_velocity_below_3x():
    """S2 must skip when velocity < 3x min_vel_delta (not enough conviction)."""
    ticker = "KXETH-25MAY30-T2800F"
    cfg = _S2_ASSET_CONFIG["ETH"]
    # Seed velocity exactly at 1.5x threshold (passes detection but fails conviction)
    lookback = cfg["vel_lookback"]
    min_vel  = cfg["min_vel_delta"]
    step = (min_vel * 1.5) / 2.0  # gives vel_delta ~= 1.5x min_vel
    base = 70.0
    history = collections.deque(maxlen=60)
    now = time.time()
    prices = [base + i * step for i in range(lookback + 1)]
    for i, p in enumerate(prices):
        history.append((now - (lookback - i) * 10, p))
    bot_state._contract_price_history[ticker] = history

    result = strategy_brain_s2(
        btc_price=2850.0,
        strike=2800.0,
        yes_ask=45.0,
        no_ask=55.0,
        elapsed_seconds=760.0,
        secs_left=480.0,
        ticker=ticker,
        asset="ETH",
    )
    assert result["action"] == "skip"
    assert "s2_vel_weak" in result["reasoning"], f"Expected conviction skip, got: {result['reasoning']}"
```

- [ ] **Step 2: Run to confirm failure**

```
python -m pytest tests/test_s2_fires.py::TestS2FiresETH::test_s2_skips_weak_velocity_below_3x -v
```
Expected: FAIL (no conviction gate exists yet — the trade fires)

- [ ] **Step 3: Add conviction gate and update win_prob in `strategy_brain_s2`**

In `bot_strategy.py`, after the velocity direction block (after line ~505), add the conviction check and update win_prob:

**Replace** from `direction, vel_delta = _s2_contract_direction(...)` through `base_p = _s2_lookup_win_rate(...)`:

```python
    # Direction pointer: contract price velocity
    direction, vel_delta = _s2_contract_direction(ticker, cfg["min_vel_delta"], cfg["vel_lookback"])
    if direction is None:
        _vel_reason = "s2_no_velocity_data" if vel_delta is None else f"s2_vel_flat:{vel_delta:.3f}<{cfg['min_vel_delta']}"
        return _make_skip("yes", _vel_reason, abs_pct, mins_left, variant="strategy2")

    # Conviction gate: require 3× minimum velocity — strong signal only, not noise
    _min_conviction = 3.0 * cfg["min_vel_delta"]
    if vel_delta < _min_conviction:
        return _make_skip(
            direction,
            f"s2_vel_weak:{vel_delta:.3f}<{_min_conviction:.3f}",
            abs_pct, mins_left, variant="strategy2",
        )

    side = direction
    entry_price = yes_ask if side == "yes" else no_ask

    # Gate 3: entry price range — 55c max filters out expensive trades beyond uncertainty zone
    _min_p = float(get_asset_config(config, asset, "min_entry_price_cents", 20.0))
    _max_p = float(get_asset_config(config, asset, "max_entry_price_cents", 55.0))
    if entry_price < _min_p or entry_price > _max_p:
        return _make_skip(side, f"s2_price_filter:{entry_price:.0f}c", abs_pct, mins_left,
                          variant="strategy2", price_filter=True)

    # Gate 4: OBI confirmation
    obi_ok, obi_val = _s2_obi_gate(ticker, side, cfg["min_obi"])
    if not obi_ok:
        return _make_skip(
            side,
            f"s2_obi_gate:obi={'None' if obi_val is None else f'{obi_val:.2f}'}_side={side}_min={cfg['min_obi']}",
            abs_pct, mins_left, variant="strategy2",
        )

    # Win probability: geometric certainty model (same as S1 — velocity qualifies direction,
    # dist+time determines how certain the outcome is)
    win_prob = _s1_certainty_win_prob(abs_pct, secs_left, asset)
```

Also update the brain_log.info line to include conviction info:
```python
    brain_log.info(
        "S2 TRADE %s %s | vel=%s delta=%.2f(%.1fx) obi=%s dist=%.4f ev=%.3f wp=%.3f mins=%.1f",
        asset, ticker, direction, vel_delta or 0, (vel_delta or 0) / max(cfg["min_vel_delta"], 1e-9),
        _obi_str, abs_pct, ev, win_prob, mins_left,
    )
```

- [ ] **Step 4: Update `_seed_velocity` in test_s2_fires.py to use 4× step for "strong signal" tests**

The existing `_seed_velocity` uses 1.5× margin. With the new 3× conviction gate, existing "should fire" tests will now fail (1.5× < 3×). Update `_seed_velocity`:

```python
def _seed_velocity(ticker: str, asset: str, direction: str = "yes"):
    """
    Seed contract price history with a strong velocity signal (4x minimum).
    Conviction gate requires 3x min_vel_delta, so 4x gives safety margin.
    """
    cfg = _S2_ASSET_CONFIG[asset]
    lookback = cfg["vel_lookback"]
    min_vel  = cfg["min_vel_delta"]
    # 4x margin: vel_delta computed from half-avg diff ≈ 2*step, so need step = 2*min_vel
    step = (min_vel * 4.0) / 2.0
    base = 70.0
    n = lookback + 1
    history = collections.deque(maxlen=60)
    if direction == "yes":
        prices = [base + i * step for i in range(n)]
    else:
        prices = [base + (n - 1 - i) * step for i in range(n)]
    now = time.time()
    for i, p in enumerate(prices):
        history.append((now - (n - 1 - i) * 10, p))
    bot_state._contract_price_history[ticker] = history
```

- [ ] **Step 5: Run full test suite**

```
python -m pytest tests/ -q
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add bot_strategy.py tests/test_s2_fires.py
git commit -m "feat(strategy): S2 conviction gate (3x vel) + geometric certainty win_prob"
```

---

## Task 5: Final cleanup and push

**Files:**
- `tests/test_strategy_fix.py` — verify `test_s1_min_dist_in_profitable_range` still passes with new config
- `tests/test_strategy_params.py` — verify `test_s2_win_rate_tables_all_none` still passes

- [ ] **Step 1: Run full test suite one more time**

```
python -m pytest tests/ -v 2>&1 | tail -30
```
Expected: all pass. If any test references "ema" in reasoning strings, update it to "mom" or "momentum".

- [ ] **Step 2: Verify S1 and S2 can both fire in a simulated scenario**

```python
# Quick sanity check — run from repo root:
python -c "
import math, time
from collections import deque
import bot_state
from bot_strategy import strategy_brain_s1, strategy_brain_s2

# S1: seed 90s of price data with a 0.4% upward move in last 60s
now = time.time()
prices = deque(maxlen=2000)
for i in range(90):
    ts = now - 90 + i
    p = 2510.0 if i < 30 else 2510.0 + (i - 30) * (10.0/60)
prices = deque([(now - 90 + i, 2510.0 + max(0, i-30)*(10.0/60)) for i in range(90)], maxlen=2000)
bot_state._prices['ETH'] = prices

r1 = strategy_brain_s1(float(prices[-1][1]), 2500.0, 50.0, 50.0, 300, 480, 'KXETH-TEST', 'ETH')
print('S1:', r1['action'], r1['reasoning'][:80])

# S2: seed strong velocity
from collections import deque as dq2
import bot_state
vel_hist = dq2([(now - 40 + i*10, 50 + i*0.5) for i in range(5)], maxlen=60)
bot_state._contract_price_history['KXETH-TEST'] = vel_hist
r2 = strategy_brain_s2(2520.0, 2500.0, 48.0, 52.0, 300, 480, 'KXETH-TEST', 'ETH')
print('S2:', r2['action'], r2['reasoning'][:80])
"
```
Expected: S1 prints `trade`, S2 prints `trade` (or skips only on EV gate, not momentum/vel gate).

- [ ] **Step 3: Push to GitHub**

```bash
git push
```

---

## Self-Review

**Spec coverage check:**
- ✅ Max entry 55c for both — Task 1
- ✅ S1 EMA replaced with momentum — Task 3
- ✅ Geometric certainty win_prob for S1 — Task 2 + 3
- ✅ S2 conviction gate (3× velocity) — Task 4
- ✅ S2 certainty win_prob — Task 4
- ✅ Tests updated throughout — each task

**Placeholder scan:** None found — all code blocks are complete implementations.

**Type consistency:** `_s1_momentum_direction` returns `(str | None, float | None)` — same pattern as `_s1_ema_direction`. `_s1_certainty_win_prob` returns `float` — same as `_s1_lookup_win_rate`. S2 uses `_s1_certainty_win_prob` (cross-strategy reuse is fine since the geometric model is direction-agnostic).
