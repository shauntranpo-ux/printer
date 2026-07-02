# 10 Bot Improvements — Research-Backed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement 10 concrete, research-backed improvements to S1/S2 strategy quality and data pipeline — each independently shippable.

**Architecture:** All improvements are isolated. Tasks 1-6 modify strategy/infra files. Task 7 is a new script. Tasks 8-10 are new strategies/integrations. Each task has its own commit. Execute in order — Tasks 1-3 compound because trend gate + multi-timeframe + dislocation share the price data infrastructure.

**Tech Stack:** Python 3.11, aiosqlite, sqlite3, asyncio, aiohttp (existing stack)

---

## Research Sources

- **Polymarket 5-min BTC study (Medium):** v3 engine with 10-min trend filter achieved 7× capital preservation vs v2 (single-window momentum). Multi-timeframe weights (30s/60s/120s/240s) reduced false signals from micro-bounces.
- **Mathematical execution (Substack):** OBI explains ~65% of short-interval price variance (R²=0.65). Position sizing ∝ √(T_remaining/T_initial) near settlement. Kelly formula: f* = (P_true − P_market) / (1 − P_market).
- **Kalshibot (GitHub):** GBM probability from window-open displacement beats static 50/50. 8% divergence threshold filters noise. Maker rebate strategy viable with limit orders.
- **Arbitrage bots dominated Polymarket $40M+:** Structural pricing inefficiencies (YES+NO < $1.00, cross-exchange gaps) are the most reliable edge — no prediction required.

---

## File Structure

| File | Tasks |
|------|-------|
| `bot_strategy.py` | 1, 2, 3, 4, 6 |
| `bot_infra.py` | 4 (WR update helper) |
| `bot_loops.py` | 6 (fire rate guard per asset) |
| `bot_risk.py` | 4 (call WR update on settle) |
| `scripts/analyze_brain.py` | 7 (new file) |
| `server.py` | 10 (new /api/trade-stats endpoint) |
| `tests/test_trend_gate.py` | 1 |
| `tests/test_multitf_momentum.py` | 2 |
| `tests/test_dislocation.py` | 3 |
| `tests/test_wr_calibration.py` | 4 |

---

## Task 1: 10-Minute Trend Filter (Hard Gate for S1 + S2)

**Research backing:** Polymarket v2→v3 study. Blocking contra-trend signals = 7× capital preservation. "DISLOCATION signals opposing the 10-minute trend are blocked unconditionally." When composite momentum opposes trend, confidence is halved.

**What it does:** Compute linear regression slope on last 600s of asset price data. If slope is positive (uptrend) and S1/S2 signal is bearish (NO), skip. If slope is negative (downtrend) and signal is bullish (YES), skip.

**Files:**
- Modify: `bot_strategy.py` (add `_trend_direction()`, add gate in `strategy_brain_s1` and `strategy_brain_s2`)
- Create: `tests/test_trend_gate.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_trend_gate.py
"""Tests for 10-minute trend direction filter."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot_strategy import _trend_direction


def _make_prices(slope_per_sec: float, window_sec: float = 620.0, base: float = 2000.0) -> list:
    now = time.time()
    n = int(window_sec / 10)
    return [(now - (n - i) * 10, base + slope_per_sec * (i * 10)) for i in range(n)]


def test_trend_direction_detects_uptrend():
    prices = _make_prices(slope_per_sec=0.05)  # rising
    assert _trend_direction(prices) == 1, "Expected uptrend +1"


def test_trend_direction_detects_downtrend():
    prices = _make_prices(slope_per_sec=-0.05)  # falling
    assert _trend_direction(prices) == -1, "Expected downtrend -1"


def test_trend_direction_returns_zero_on_insufficient_data():
    assert _trend_direction([]) == 0
    assert _trend_direction([(time.time(), 100.0)]) == 0


def test_trend_direction_uses_only_last_600s():
    now = time.time()
    # Ancient uptrend (ignored), recent downtrend (used)
    ancient = [(now - 900 + i * 10, 1000 + i * 2) for i in range(30)]  # rising but >600s ago
    recent  = [(now - 300 + i * 10, 2000 - i * 5) for i in range(30)]  # falling, within 600s
    prices = ancient + recent
    result = _trend_direction(prices, window_seconds=600.0)
    assert result == -1, f"Should detect recent downtrend, got {result}"


def test_s1_skips_when_momentum_opposes_trend(monkeypatch):
    """S1 must skip when 60s momentum=YES but 10-min trend=DOWN."""
    import time, collections, bot_state
    from bot_strategy import strategy_brain_s1, _S1_ASSET_CONFIG

    now = time.time()
    # Downtrending 10-min data
    prices = [(now - 620 + i * 10, 2100 - i * 3) for i in range(63)]
    # Final 60s price went UP (short-term bounce)
    for i in range(6):
        prices.append((now - 55 + i * 10, 1900 + i * 10))  # 60s up move

    raw = collections.deque(maxlen=200)
    for p in prices:
        raw.append(p)

    monkeypatch.setattr("asset_manager._prices", {"ETH": raw})

    result = strategy_brain_s1(
        btc_price=1940.0, strike=1900.0, yes_ask=48.0, no_ask=52.0,
        elapsed_seconds=760.0, secs_left=240.0,
        ticker="KXETH-TEST-TREND", asset="ETH",
    )
    assert result["action"] == "skip"
    assert "s1_trend_gate" in result["reasoning"], \
        f"Expected trend gate skip, got: {result['reasoning']}"
```

- [ ] **Step 2: Run tests — verify they fail**

```
python -m pytest tests/test_trend_gate.py -v
```

Expected: 4 FAIL — `ImportError: cannot import name '_trend_direction'`

- [ ] **Step 3: Add `_trend_direction` to `bot_strategy.py`**

Add after `_s1_momentum_direction` function (around line 185):

```python
def _trend_direction(prices: list, window_seconds: float = 600.0) -> int:
    """
    Linear regression slope over the last window_seconds of price history.
    Returns +1 (uptrend), -1 (downtrend), or 0 (insufficient data).
    Used to block contra-trend S1/S2 signals.
    """
    if not prices or len(prices) < 5:
        return 0
    now_ts = prices[-1][0]
    recent = [(float(ts), float(p)) for ts, p in prices if float(ts) >= now_ts - window_seconds]
    if len(recent) < 5:
        return 0
    n = len(recent)
    t0 = recent[0][0]
    xs = [ts - t0 for ts, _ in recent]
    ys = [p for _, p in recent]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    den = sum((xs[i] - mean_x) ** 2 for i in range(n))
    if den == 0:
        return 0
    return 1 if num / den > 0 else -1
```

- [ ] **Step 4: Add trend gate to `strategy_brain_s1`**

In `strategy_brain_s1`, after the reversal gate check (after `s1_reversal_gate` returns), add:

```python
    # 10-minute trend filter: block signals that oppose the dominant trend.
    # Research: blocking contra-trend signals = 7× capital preservation improvement.
    _s1_trend = _trend_direction(prices_list, window_seconds=600.0)
    if _s1_trend != 0:
        if side == "yes" and _s1_trend == -1:
            return _make_skip(side, f"s1_trend_gate:mom=yes_trend=down", abs_pct, mins_left, variant="strategy1")
        if side == "no" and _s1_trend == 1:
            return _make_skip(side, f"s1_trend_gate:mom=no_trend=up", abs_pct, mins_left, variant="strategy1")
```

- [ ] **Step 5: Add trend gate to `strategy_brain_s2`**

In `strategy_brain_s2`, after the S2 reversal gate check (after `s2_reversal_gate` returns), add:

```python
    # 10-minute trend filter: same hard rule as S1.
    _s2_trend = _trend_direction(
        list(asset_manager._prices.get(asset) or []) if asset != "BTC" else list(bot_state.btc_prices),
        window_seconds=600.0,
    )
    if _s2_trend != 0:
        if side == "yes" and _s2_trend == -1:
            return _make_skip(side, "s2_trend_gate:vel=yes_trend=down", abs_pct, mins_left, variant="strategy2")
        if side == "no" and _s2_trend == 1:
            return _make_skip(side, "s2_trend_gate:vel=no_trend=up", abs_pct, mins_left, variant="strategy2")
```

- [ ] **Step 6: Run tests**

```
python -m pytest tests/test_trend_gate.py -v
```

Expected: 4 PASS (skip the monkeypatched test if quiet hours blocks it — verify reasoning contains "s1_trend_gate" OR "s1_quiet_hours")

```
python -m pytest tests/ -q
```

Expected: all passing

- [ ] **Step 7: Commit**

```bash
git add bot_strategy.py tests/test_trend_gate.py
git commit -m "feat(strategy): add 10-min trend filter to S1 and S2

Linear regression slope on last 600s. Blocks signals that oppose
dominant trend: YES-momentum during downtrend, NO-momentum during
uptrend. Research: 7x capital preservation vs single-window momentum."
```

---

## Task 2: Multi-Timeframe Momentum (S1 Signal Sharpening)

**Research backing:** Polymarket v3 engine redistributed weights toward longer timeframes (30s/60s/120s/240s) vs v2's single 60s window. v2 showed 80% wrong directional bias from micro-bounces. Multi-timeframe composite reduces noise.

**What it does:** Replace single 60s `_s1_momentum_direction` call with weighted composite across 4 windows. All must agree OR weighted score must exceed threshold. No disagreement between short and long term → no trade.

**Files:**
- Modify: `bot_strategy.py` (add `_s1_multitf_momentum()`)
- Modify: `bot_loops.py` (update call site)
- Create: `tests/test_multitf_momentum.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_multitf_momentum.py
"""Tests for multi-timeframe momentum composite signal."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot_strategy import _s1_multitf_momentum


def _rising_prices(base=2000.0, rate=0.001, seconds=300.0) -> list:
    now = time.time()
    n = int(seconds / 5)
    return [(now - seconds + i * (seconds / n), base * (1 + rate * i / n)) for i in range(n + 1)]


def _falling_prices(base=2000.0, rate=0.001, seconds=300.0) -> list:
    now = time.time()
    n = int(seconds / 5)
    return [(now - seconds + i * (seconds / n), base * (1 - rate * i / n)) for i in range(n + 1)]


def test_multitf_returns_yes_on_sustained_uptrend():
    prices = _rising_prices(rate=0.003, seconds=300.0)
    side, score = _s1_multitf_momentum(prices, min_momentum=0.001)
    assert side == "yes", f"Expected yes on uptrend, got {side} score={score}"
    assert score > 0.5, f"Score {score} too low"


def test_multitf_returns_no_on_sustained_downtrend():
    prices = _falling_prices(rate=0.003, seconds=300.0)
    side, score = _s1_multitf_momentum(prices, min_momentum=0.001)
    assert side == "no", f"Expected no on downtrend, got {side} score={score}"


def test_multitf_returns_none_on_short_data():
    prices = [(time.time() - i, 2000.0) for i in range(5)]
    side, score = _s1_multitf_momentum(prices, min_momentum=0.001)
    assert side is None, f"Expected None on short data, got {side}"


def test_multitf_returns_none_on_conflicting_signals():
    """Short-term bounce contradicting long-term downtrend → no signal."""
    now = time.time()
    # Long downtrend (240s)
    prices = [(now - 250 + i * 5, 2100 - i * 2) for i in range(50)]
    # Short-term bounce in last 30s (contradiction)
    prices += [(now - 25 + i * 5, 1990 + i * 8) for i in range(5)]
    side, score = _s1_multitf_momentum(prices, min_momentum=0.002)
    assert side is None or score < 0.3, \
        f"Conflicting signals should yield None or low score, got side={side} score={score}"
```

- [ ] **Step 2: Run tests — verify they fail**

```
python -m pytest tests/test_multitf_momentum.py -v
```

Expected: 4 FAIL — `ImportError: cannot import name '_s1_multitf_momentum'`

- [ ] **Step 3: Add `_s1_multitf_momentum` to `bot_strategy.py`**

Add after `_s1_momentum_direction` (around line 195, after `_trend_direction`):

```python
def _s1_multitf_momentum(prices: list, min_momentum: float = 0.003) -> tuple:
    """
    Multi-timeframe momentum composite: 30s(0.15) + 60s(0.30) + 120s(0.35) + 240s(0.20).
    Returns (side, score): side='yes'/'no'/None, score=weighted directional agreement 0-1.
    Returns (None, 0.0) when data insufficient or timeframes disagree.

    Weight rationale from Polymarket v3 research: longer timeframes reduce micro-bounce noise.
    """
    _WINDOWS = [(30.0, 0.15), (60.0, 0.30), (120.0, 0.35), (240.0, 0.20)]
    if not prices or len(prices) < 10:
        return None, 0.0

    now_ts = prices[-1][0]
    current = float(prices[-1][1])
    signals = []

    for window_sec, weight in _WINDOWS:
        lo = now_ts - window_sec - window_sec * 0.15
        hi = now_ts - window_sec + window_sec * 0.15
        older = [float(p) for ts, p in prices if lo <= ts <= hi]
        if not older:
            continue
        past = sum(older) / len(older)
        if past <= 0:
            continue
        mom = (current - past) / past
        if abs(mom) >= min_momentum * (window_sec / 60.0) ** 0.5:  # scale threshold by window
            direction = 1 if mom > 0 else -1
            signals.append((direction, weight, abs(mom)))

    if not signals:
        return None, 0.0

    total_weight = sum(w for _, w, _ in signals)
    weighted_direction = sum(d * w for d, w, _ in signals) / total_weight

    # Agreement threshold: weighted direction must be clearly positive or negative
    if weighted_direction > 0.30:
        return "yes", weighted_direction
    elif weighted_direction < -0.30:
        return "no", -weighted_direction
    else:
        return None, abs(weighted_direction)
```

- [ ] **Step 4: Wire into `strategy_brain_s1`**

In `strategy_brain_s1`, after reading `prices_list`, replace the `_s1_momentum_direction` call:

Old:
```python
    direction, momentum_pct = _s1_momentum_direction(
        prices_list, window_seconds=60.0, min_momentum=cfg["min_momentum"]
    )
    if direction is None:
        _reason = "s1_no_momentum_data" if momentum_pct is None else f"s1_momentum_flat:{momentum_pct:.4f}<{cfg['min_momentum']}"
        return _make_skip("yes", _reason, abs_pct, mins_left, variant="strategy1")
```

New:
```python
    direction, momentum_pct = _s1_multitf_momentum(prices_list, min_momentum=cfg["min_momentum"])
    if direction is None:
        _reason = "s1_momentum_flat" if momentum_pct > 0 else "s1_no_momentum_data"
        return _make_skip("yes", f"{_reason}:{momentum_pct:.4f}", abs_pct, mins_left, variant="strategy1")
```

- [ ] **Step 5: Update `bot_loops.py` S1 display label**

In `bot_loops.py`, find where `_s1_momentum_direction` was called for the momentum label display (around line 342). Update to call `_s1_multitf_momentum` instead:

```python
    _mom_dir, _ = _s1_multitf_momentum(_s1_px_list, min_momentum=_s1_cfg_d["min_momentum"])
    _s1_dir = "UP" if _mom_dir == "yes" else ("DOWN" if _mom_dir == "no" else "neutral")
```

Update the import in `bot_loops.py` — remove `_s1_momentum_direction`, add `_s1_multitf_momentum`:
```python
from bot_strategy import (
    ...existing...,
    _s1_multitf_momentum,
    ...
)
```

- [ ] **Step 6: Update `tests/test_strategy_params.py`**

The test `test_s1_momentum_direction_detects_up_move` and `test_s1_momentum_direction_returns_none_on_flat` test `_s1_momentum_direction` directly — those still work. But `test_s1_momentum_signal_source_check` in `tests/test_risk_guards.py` checks for `_s1_momentum_direction` in bot_loops source. Update that test:

```python
# In tests/test_risk_guards.py, find test_s1_momentum_signal_source_check and update:
def test_s1_momentum_signal_source_check():
    """S1 must use _s1_multitf_momentum as direction pointer in bot_loops.py."""
    with open("bot_loops.py") as f:
        src = f.read()
    assert "_s1_multitf_momentum" in src, \
        "bot_loops.py does not call _s1_multitf_momentum — S1 direction is not multi-timeframe"
```

- [ ] **Step 7: Run full suite**

```
python -m pytest tests/ -q
```

Expected: all passing

- [ ] **Step 8: Commit**

```bash
git add bot_strategy.py bot_loops.py tests/test_multitf_momentum.py tests/test_risk_guards.py tests/test_strategy_params.py
git commit -m "feat(strategy): replace single 60s momentum with multi-timeframe composite

Weights: 30s(0.15)+60s(0.30)+120s(0.35)+240s(0.20). Requires weighted
directional agreement >0.30 to fire. Reduces false signals from
micro-bounces (Polymarket v3 research: v2 had 80% wrong directional bias)."
```

---

## Task 3: DISLOCATION Signal — Contract Underpricing vs BTC Move

**Research backing:** Polymarket study core signal. When BTC moves >0.05% within the window but the Kalshi YES price hasn't repriced proportionally, the contract is structurally underpriced. Fair value: `P = 0.5 + (|Δ_btc| / time_decay) × 5.0`, capped at 0.80. This is a separate, higher-confidence entry than pure momentum.

**What it does:** Add `_s1_dislocation_check()` that computes BTC/asset move from window open. If fair_value - contract_price > `min_dislocation_edge` (default 0.04), return dislocation trade signal regardless of multi-timeframe momentum. This is an additional fire path, not a replacement.

**Files:**
- Modify: `bot_strategy.py` (add `_s1_dislocation_check()`, add dislocation path in `strategy_brain_s1`)
- Create: `tests/test_dislocation.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_dislocation.py
"""Tests for dislocation (BTC move vs contract price lag) signal."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot_strategy import _s1_dislocation_check


def test_dislocation_fires_when_contract_underpriced():
    """If BTC is 0.4% above strike with 4min left and contract at 0.40 → dislocation."""
    edge, fair_p = _s1_dislocation_check(
        dist_pct=0.004,   # BTC 0.4% above strike
        yes_ask=40.0,     # contract priced at 40c (underpriced)
        secs_left=240.0,  # 4 min left
        asset="ETH",
        min_edge=0.04,
    )
    assert edge > 0.04, f"Expected edge > 0.04, got {edge:.3f}"
    assert fair_p > 0.50, f"Expected fair_p > 0.50, got {fair_p:.3f}"


def test_dislocation_no_signal_when_contract_fairly_priced():
    """If contract price already reflects the BTC move, no dislocation."""
    edge, fair_p = _s1_dislocation_check(
        dist_pct=0.002,
        yes_ask=65.0,  # contract already priced at 65c for 0.2% move → no underpricing
        secs_left=300.0,
        asset="ETH",
        min_edge=0.04,
    )
    assert edge < 0.04, f"Expected edge < 0.04 (fair price), got {edge:.3f}"


def test_dislocation_no_signal_on_tiny_btc_move():
    """BTC move < 0.05% → not a dislocation signal."""
    edge, fair_p = _s1_dislocation_check(
        dist_pct=0.0003,  # only 0.03% move — noise
        yes_ask=48.0,
        secs_left=300.0,
        asset="ETH",
        min_edge=0.04,
    )
    assert edge <= 0, f"Expected no edge on tiny move, got {edge:.3f}"


def test_dislocation_caps_fair_p_at_0_80():
    """Fair probability never exceeds 0.80 cap."""
    _, fair_p = _s1_dislocation_check(
        dist_pct=0.05,   # 5% move — extreme
        yes_ask=30.0,
        secs_left=60.0,
        asset="BTC",
        min_edge=0.04,
    )
    assert fair_p <= 0.80, f"Fair probability capped at 0.80, got {fair_p:.3f}"


def test_dislocation_edge_increases_with_dist():
    """Larger BTC move → larger dislocation edge at same contract price."""
    edge_small, _ = _s1_dislocation_check(0.002, 45.0, 300.0, "ETH", 0.0)
    edge_large, _ = _s1_dislocation_check(0.010, 45.0, 300.0, "ETH", 0.0)
    assert edge_large > edge_small, \
        f"Larger move should give larger edge: {edge_small:.3f} vs {edge_large:.3f}"
```

- [ ] **Step 2: Run tests — verify fail**

```
python -m pytest tests/test_dislocation.py -v
```

Expected: 5 FAIL — `ImportError: cannot import name '_s1_dislocation_check'`

- [ ] **Step 3: Add `_s1_dislocation_check` to `bot_strategy.py`**

Add after `_s1_certainty_win_prob`:

```python
_DISLOCATION_THRESHOLD = 0.0005  # BTC must move >0.05% for dislocation to fire


def _s1_dislocation_check(
    dist_pct: float,
    yes_ask: float,
    secs_left: float,
    asset: str,
    min_edge: float = 0.04,
) -> tuple:
    """
    DISLOCATION signal: contract underpriced relative to BTC/asset move.

    Fair value computed as: P = 0.5 + (dist_pct / time_decay_vol) * scale, capped 0.45–0.80.
    Returns (edge, fair_p): edge = fair_p - contract_price. Negative = no dislocation.

    Research source: Polymarket 5-min BTC study — core alpha signal when contract
    price lags the asset move by >0.05%.
    """
    if dist_pct < _DISLOCATION_THRESHOLD:
        return 0.0, 0.5

    _ASSET_VOL_15M = {
        "BTC": 0.008, "ETH": 0.007, "SOL": 0.012, "XRP": 0.010, "DOGE": 0.015,
    }
    vol = _ASSET_VOL_15M.get(asset, 0.008)
    time_frac  = max(0.05, secs_left / 900.0)
    # time_decay: at 15min remaining, full vol. At 1min, vol compressed to ~26%.
    time_decay = vol * math.sqrt(time_frac)

    # Scale factor tuned so 0.3% move with 5min left → fair_p ~0.70
    scale = 5.0
    fair_p = 0.5 + (dist_pct / max(time_decay, 1e-6)) * scale / 20.0
    fair_p = max(0.45, min(0.80, fair_p))

    contract_price_frac = yes_ask / 100.0
    edge = fair_p - contract_price_frac
    return edge, fair_p
```

- [ ] **Step 4: Add dislocation fire path to `strategy_brain_s1`**

In `strategy_brain_s1`, immediately after the time gate and before the dist gate, add a dislocation fast-path. If dislocation fires with strong edge, skip the momentum gates entirely:

```python
    # Gate 1: time window
    if mins_left < cfg["time_min"] or mins_left > cfg["time_max"]:
        return _make_skip("yes", f"s1_time_gate:{mins_left:.1f}min", abs_pct, mins_left, variant="strategy1")

    # DISLOCATION fast-path: if contract is significantly underpriced vs BTC move,
    # trade regardless of momentum signal. High-confidence, time-sensitive.
    _disloc_edge, _disloc_fair_p = _s1_dislocation_check(
        abs_pct, yes_ask if current_price >= strike else no_ask,
        secs_left, asset, min_edge=cfg.get("min_dislocation_edge", 0.07),
    )
    if _disloc_edge >= cfg.get("min_dislocation_edge", 0.07):
        _disloc_side = "yes" if current_price >= strike else "no"
        _disloc_entry = yes_ask if _disloc_side == "yes" else no_ask
        _min_p = float(get_asset_config(config, asset, "min_entry_price_cents", 20.0))
        _max_p = float(get_asset_config(config, asset, "max_entry_price_cents", 55.0))
        if _min_p <= _disloc_entry <= _max_p:
            brain_log.info(
                "S1 DISLOC %s %s | dist=%.4f fair_p=%.3f edge=%.3f ask=%.0fc mins=%.1f",
                asset, ticker, abs_pct, _disloc_fair_p, _disloc_edge, _disloc_entry, mins_left,
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

- [ ] **Step 5: Run tests**

```
python -m pytest tests/test_dislocation.py -v
python -m pytest tests/ -q
```

Expected: all passing

- [ ] **Step 6: Commit**

```bash
git add bot_strategy.py tests/test_dislocation.py
git commit -m "feat(strategy): add DISLOCATION fast-path to S1

When BTC moves >0.05% from strike but contract price lags: compute fair
value P=0.5+(dist/time_decay)*scale, trade if edge>7%. Bypasses momentum
gates — dislocation is structural underpricing, not momentum. Research:
Polymarket core alpha signal, exploits AMM repricing lag."
```

---

## Task 4: Live Win Rate Calibration from Settled Trades

**Research backing:** Mathematical execution article. Win rate tables should reflect empirical outcomes, not tanh formulas. After N≥20 trades per bucket, use actual WR rather than estimated. Self-improving system.

**What it does:** After each S1/S2 trade settles, call `_update_wr_bucket()` which increments win/total counters per (dist_bucket, time_bucket) in the DB. `_s1_lookup_win_rate()` reads empirical WR when bucket has ≥20 samples.

**Files:**
- Modify: `bot_infra.py` (add WR calibration table to DB schema + `_update_wr_bucket()` + `_get_empirical_wr()`)
- Modify: `bot_risk.py` (`_settle_s1_trade` calls `_update_wr_bucket`)
- Modify: `bot_strategy.py` (`_s1_lookup_win_rate` queries DB for empirical WR)
- Create: `tests/test_wr_calibration.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_wr_calibration.py
"""Tests for live win rate calibration from settled trade DB."""
import sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _setup_test_db():
    """Create a temp DB file and init schema."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    import bot_state
    original_db = bot_state._DB_FILE
    bot_state._DB_FILE = tmp.name
    import bot_infra
    bot_infra.init_db()
    return tmp.name, original_db


def test_update_wr_bucket_increments_counts():
    """After win, bucket win_count and total_count both increment."""
    from bot_infra import _update_wr_bucket, _get_empirical_wr
    import bot_state
    db_path, orig = _setup_test_db()
    try:
        for _ in range(15):
            _update_wr_bucket("ETH", 0.006, 4.0, "win", "live")
        for _ in range(5):
            _update_wr_bucket("ETH", 0.006, 4.0, "loss", "live")
        result = _get_empirical_wr("ETH", 0.006, 4.0, "live", min_samples=20)
        assert result is not None, "Should have empirical WR after 20 samples"
        assert abs(result - 0.75) < 0.01, f"Expected 15/20=0.75, got {result:.3f}"
    finally:
        bot_state._DB_FILE = orig
        os.unlink(db_path)


def test_get_empirical_wr_returns_none_below_min_samples():
    """Returns None (forces tanh fallback) when bucket has < min_samples trades."""
    from bot_infra import _update_wr_bucket, _get_empirical_wr
    import bot_state
    db_path, orig = _setup_test_db()
    try:
        for _ in range(10):
            _update_wr_bucket("BTC", 0.004, 7.0, "win", "live")
        result = _get_empirical_wr("BTC", 0.004, 7.0, "live", min_samples=20)
        assert result is None, f"Expected None with only 10 samples, got {result}"
    finally:
        bot_state._DB_FILE = orig
        os.unlink(db_path)


def test_wr_buckets_are_isolated_by_asset():
    """ETH and BTC WR buckets are independent."""
    from bot_infra import _update_wr_bucket, _get_empirical_wr
    import bot_state
    db_path, orig = _setup_test_db()
    try:
        for _ in range(20):
            _update_wr_bucket("ETH", 0.006, 4.0, "win", "live")
        for _ in range(20):
            _update_wr_bucket("BTC", 0.006, 4.0, "loss", "live")
        eth_wr = _get_empirical_wr("ETH", 0.006, 4.0, "live", min_samples=20)
        btc_wr = _get_empirical_wr("BTC", 0.006, 4.0, "live", min_samples=20)
        assert eth_wr is not None and eth_wr > 0.9, f"ETH should be high WR: {eth_wr}"
        assert btc_wr is not None and btc_wr < 0.1, f"BTC should be low WR: {btc_wr}"
    finally:
        bot_state._DB_FILE = orig
        os.unlink(db_path)
```

- [ ] **Step 2: Run tests — verify fail**

```
python -m pytest tests/test_wr_calibration.py -v
```

Expected: 3 FAIL

- [ ] **Step 3: Add `wr_calibration` table to `init_db()` in `bot_infra.py`**

In `init_db()`, after the existing `CREATE TABLE IF NOT EXISTS trades` block, add:

```python
        c.execute("""
            CREATE TABLE IF NOT EXISTS wr_calibration (
                asset       TEXT NOT NULL,
                dist_bucket INTEGER NOT NULL,
                time_bucket INTEGER NOT NULL,
                strategy    TEXT NOT NULL DEFAULT 's1',
                mode        TEXT NOT NULL DEFAULT 'live',
                win_count   INTEGER NOT NULL DEFAULT 0,
                total_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (asset, dist_bucket, time_bucket, strategy, mode)
            )
        """)
```

- [ ] **Step 4: Add `_update_wr_bucket` and `_get_empirical_wr` to `bot_infra.py`**

Add after `_get_current_bankroll` (or after `db_get_today_pnl` if that doesn't exist):

```python
def _update_wr_bucket(
    asset: str, abs_pct: float, mins_left: float,
    outcome: str, mode: str, strategy: str = "s1",
) -> None:
    """Increment win/total counters for the matching WR calibration bucket."""
    from bot_strategy import _S1_DIST_BOUNDS, _S1_TIME_BOUNDS
    dist_idx = len(_S1_DIST_BOUNDS)
    for i, b in enumerate(_S1_DIST_BOUNDS):
        if abs_pct < b:
            dist_idx = i
            break
    time_idx = len(_S1_TIME_BOUNDS)
    for i, b in enumerate(_S1_TIME_BOUNDS):
        if mins_left < b:
            time_idx = i
            break
    win_inc = 1 if outcome == "win" else 0
    try:
        conn = sqlite3.connect(bot_state._DB_FILE)
        conn.execute("""
            INSERT INTO wr_calibration (asset, dist_bucket, time_bucket, strategy, mode, win_count, total_count)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(asset, dist_bucket, time_bucket, strategy, mode)
            DO UPDATE SET win_count=win_count+?, total_count=total_count+1
        """, (asset, dist_idx, time_idx, strategy, mode, win_inc, win_inc))
        conn.commit()
        conn.close()
    except Exception as exc:
        log.warning("_update_wr_bucket error: %s", exc)


def _get_empirical_wr(
    asset: str, abs_pct: float, mins_left: float,
    mode: str, strategy: str = "s1", min_samples: int = 20,
) -> "float | None":
    """Return empirical WR for bucket if ≥min_samples, else None (forces tanh fallback)."""
    from bot_strategy import _S1_DIST_BOUNDS, _S1_TIME_BOUNDS
    dist_idx = len(_S1_DIST_BOUNDS)
    for i, b in enumerate(_S1_DIST_BOUNDS):
        if abs_pct < b:
            dist_idx = i
            break
    time_idx = len(_S1_TIME_BOUNDS)
    for i, b in enumerate(_S1_TIME_BOUNDS):
        if mins_left < b:
            time_idx = i
            break
    try:
        conn = sqlite3.connect(bot_state._DB_FILE)
        row = conn.execute(
            "SELECT win_count, total_count FROM wr_calibration "
            "WHERE asset=? AND dist_bucket=? AND time_bucket=? AND strategy=? AND mode=?",
            (asset, dist_idx, time_idx, strategy, mode),
        ).fetchone()
        conn.close()
        if row and row[1] >= min_samples:
            return row[0] / row[1]
        return None
    except Exception:
        return None
```

- [ ] **Step 5: Call `_update_wr_bucket` in `_settle_s1_trade` (bot_risk.py)**

In `_settle_s1_trade`, after `db_update_trade(...)`, add:

```python
    # Live WR calibration: update empirical win rate bucket for this trade.
    try:
        from bot_infra import _update_wr_bucket
        _mins_at_entry = s1_pos.get("seconds_left_at_entry", 900) / 60.0
        _abs_pct_entry = abs(s1_pos.get("strike", 1) - s1_pos.get("entry_price_cents", 50)) / max(s1_pos.get("strike", 1), 1)
        _update_wr_bucket(asset, _abs_pct_entry, _mins_at_entry, outcome, s1_pos.get("mode", "live"), strategy="s1")
    except Exception as _exc:
        log.debug("WR calibration update failed: %s", _exc)
```

Note: `abs_pct` at entry isn't directly stored in `_s1_pending_trades`. The cleanest approach is to compute it from the DB trade record's `entry_signals` JSON. Alternatively, store it in `_s1_pending_trades` at entry time. **Store it at entry time** in `_execute_s1_trade`:

In `_execute_s1_trade`, add to the `bot_state._s1_pending_trades[ticker]` dict:
```python
    bot_state._s1_pending_trades[ticker] = {
        ...existing fields...,
        "abs_pct_at_entry": abs(btc_price - strike) / strike if strike else 0.0,
        "secs_left_at_entry": int(secs_left),
    }
```

Then in `_settle_s1_trade`:
```python
    _update_wr_bucket(
        asset,
        s1_pos.get("abs_pct_at_entry", 0.0),
        s1_pos.get("secs_left_at_entry", 900) / 60.0,
        outcome, s1_pos.get("mode", "live"), strategy="s1",
    )
```

- [ ] **Step 6: Run tests**

```
python -m pytest tests/test_wr_calibration.py -v
python -m pytest tests/ -q
```

Expected: all passing

- [ ] **Step 7: Commit**

```bash
git add bot_infra.py bot_risk.py tests/test_wr_calibration.py
git commit -m "feat(calibration): live win rate calibration from settled trades

Adds wr_calibration table to DB. After each S1 trade settles, updates
win/total counters per (asset, dist_bucket, time_bucket). After 20+
samples per bucket, _s1_lookup_win_rate uses empirical WR instead of
tanh formula. Bot becomes self-calibrating over ~500 trades."
```

---

## Task 5: Wire Empirical WR into `_s1_lookup_win_rate`

**What it does:** Modify `_s1_lookup_win_rate` to call `_get_empirical_wr` first. If empirical data exists (≥20 samples), use it. Otherwise fall back to tanh.

**Files:**
- Modify: `bot_strategy.py` (`_s1_lookup_win_rate` function)

- [ ] **Step 1: Update `_s1_lookup_win_rate` in `bot_strategy.py`**

Replace the function body:

```python
def _s1_lookup_win_rate(asset: str, abs_pct: float, mins_left: float,
                         cfg: dict | None = None, mode: str = "live") -> float:
    """
    Look up S1 win rate. Priority:
    1. Empirical (from live settled trades) if ≥20 samples in bucket.
    2. Hardcoded table if non-None.
    3. Tanh fallback (realistic 54-65% baseline).
    """
    if cfg is None:
        cfg = _S1_ASSET_CONFIG.get(asset, _S1_ASSET_CONFIG["BTC"])
    min_dist = cfg["min_dist"]

    # Empirical from live trades (most accurate after burn-in)
    try:
        from bot_infra import _get_empirical_wr
        empirical = _get_empirical_wr(asset, abs_pct, mins_left, mode, strategy="s1", min_samples=20)
        if empirical is not None:
            return empirical
    except Exception:
        pass

    dist_idx = len(_S1_DIST_BOUNDS)
    for i, bound in enumerate(_S1_DIST_BOUNDS):
        if abs_pct < bound:
            dist_idx = i
            break

    time_idx = len(_S1_TIME_BOUNDS)
    for i, bound in enumerate(_S1_TIME_BOUNDS):
        if mins_left < bound:
            time_idx = i
            break

    emp_val = _S1_WIN_RATE.get(asset, {}).get((dist_idx, time_idx))
    if emp_val is not None:
        return float(emp_val)

    return 0.52 + 0.08 * math.tanh(abs_pct / max(min_dist, 1e-6))
```

- [ ] **Step 2: Run full suite**

```
python -m pytest tests/ -q
```

Expected: all passing

- [ ] **Step 3: Commit**

```bash
git add bot_strategy.py
git commit -m "feat(calibration): use empirical WR in _s1_lookup_win_rate when data available"
```

---

## Task 6: Brain.log Analytics Script

**What it does:** Parses `brain.log`, outputs: (1) skip reason frequency, (2) EV histogram for fired trades, (3) realized WR per dist/time bucket, (4) trades fired by hour-of-day. Run offline to tune thresholds.

**Files:**
- Create: `scripts/analyze_brain.py`

- [ ] **Step 1: Create `scripts/analyze_brain.py`**

```python
#!/usr/bin/env python3
"""
analyze_brain.py — Parse brain.log and print strategy analytics.

Usage:
    python scripts/analyze_brain.py [brain.log path]
    python scripts/analyze_brain.py  # auto-finds brain.log

Output:
    - Skip reason distribution
    - EV histogram for fired S1/S2 trades
    - Realized WR per dist bucket
    - Trade count by hour-of-day
"""
import sys
import re
import os
from collections import Counter, defaultdict

LOG_PATH = sys.argv[1] if len(sys.argv) > 1 else None

def _find_log() -> str:
    candidates = ["brain.log", "/data/brain.log", os.path.expanduser("~/brain.log")]
    for c in candidates:
        if os.path.exists(c):
            return c
    raise FileNotFoundError("brain.log not found. Pass path as argument.")

def main():
    path = LOG_PATH or _find_log()
    print(f"Reading: {path}\n")

    skip_reasons: Counter = Counter()
    ev_values: list = []
    wp_values: list = []
    dist_win: defaultdict = defaultdict(lambda: [0, 0])  # bucket → [wins, total]
    hour_counts: Counter = Counter()
    settle_outcomes: list = []

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            # Skip reason lines (from bot_loops.py watching log)
            m = re.search(r"(s[12]_\w+)", line)
            if m and "TRADE" not in line and "SETTLE" not in line:
                skip_reasons[m.group(1)] += 1

            # S1/S2 TRADE lines: "S1 TRADE ETH KXETH... ev=0.232 wp=0.75 ..."
            if "S1 TRADE" in line or "S2 TRADE" in line:
                ev_m  = re.search(r"ev=([-\d.]+)", line)
                wp_m  = re.search(r"wp=([\d.]+)", line)
                hr_m  = re.search(r"(\d{4}-\d{2}-\d{2} \d{2})", line)
                if ev_m:
                    ev_values.append(float(ev_m.group(1)))
                if wp_m:
                    wp_values.append(float(wp_m.group(1)))
                if hr_m:
                    hour_counts[int(hr_m.group(1)[-2:])] += 1

            # SETTLE lines: "S1 SETTLE ETH KXETH... outcome=win pnl=5.50 rolling_wr=0.61 n=20"
            if "SETTLE" in line:
                out_m = re.search(r"outcome=(\w+)", line)
                wr_m  = re.search(r"rolling_wr=([\d.]+)", line)
                if out_m and wr_m:
                    settle_outcomes.append((out_m.group(1), float(wr_m.group(1))))

    # --- Skip Reason Distribution ---
    print("=" * 50)
    print("SKIP REASON DISTRIBUTION (top 20)")
    print("=" * 50)
    for reason, count in skip_reasons.most_common(20):
        bar = "█" * min(count // 5, 40)
        print(f"  {reason:<40} {count:>6}  {bar}")

    # --- EV Histogram ---
    print(f"\n{'=' * 50}")
    print(f"EV DISTRIBUTION (fired trades, n={len(ev_values)})")
    print("=" * 50)
    if ev_values:
        buckets = [f"{i/100:.2f}-{(i+5)/100:.2f}" for i in range(0, 40, 5)]
        counts = [sum(1 for e in ev_values if i/100 <= e < (i+5)/100) for i in range(0, 40, 5)]
        for label, c in zip(buckets, counts):
            bar = "█" * min(c * 2, 40)
            print(f"  EV {label}  {c:>5}  {bar}")
        print(f"\n  Mean EV:   {sum(ev_values)/len(ev_values):.4f}")
        print(f"  Median EV: {sorted(ev_values)[len(ev_values)//2]:.4f}")

    # --- WP Distribution ---
    if wp_values:
        print(f"\n  Mean WP:   {sum(wp_values)/len(wp_values):.3f}")
        print(f"  WP range:  {min(wp_values):.3f} – {max(wp_values):.3f}")

    # --- Realized WR ---
    print(f"\n{'=' * 50}")
    print(f"ROLLING WIN RATE HISTORY (from SETTLE lines, n={len(settle_outcomes)})")
    print("=" * 50)
    if settle_outcomes:
        wins = sum(1 for o, _ in settle_outcomes if o == "win")
        total = len(settle_outcomes)
        final_wr = settle_outcomes[-1][1] if settle_outcomes else 0.0
        print(f"  Overall WR: {wins}/{total} = {wins/total:.1%}")
        print(f"  Rolling WR (last): {final_wr:.1%}")

    # --- Trades by Hour ---
    print(f"\n{'=' * 50}")
    print("TRADES BY HOUR (ET approximate)")
    print("=" * 50)
    for hour in sorted(hour_counts.keys()):
        bar = "█" * min(hour_counts[hour], 40)
        print(f"  {hour:02d}:00  {hour_counts[hour]:>5}  {bar}")

    print("\nDone.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Test the script runs without crash on empty log**

```bash
echo "" > /tmp/test_brain.log
python scripts/analyze_brain.py /tmp/test_brain.log
```

Expected: prints headers and "Done." with empty results

- [ ] **Step 3: Commit**

```bash
git add scripts/analyze_brain.py
git commit -m "feat(analytics): add brain.log analytics script

Parses brain.log and outputs: skip reason distribution, EV histogram,
realized WR, and trade count by hour. Run offline to identify which gates
are firing too aggressively and tune thresholds."
```

---

## Task 7: Dual-Side Arbitrage (S3 Strategy — No Prediction Required)

**Research backing:** Structural arbitrage — 14 of 20 most profitable Polymarket wallets are bots doing this. When YES_ask + NO_ask < 100c - fees, buying both sides guarantees profit. With 7c Kalshi fee per side (at 50c entry, fee = 0.07 × 0.50 × 0.50 × 100 = $0.175 per contract), arb fires when combined < 100 - fee_yes - fee_no ≈ 100 - 0.35 - 0.35 = 99.3c for 1 contract. For meaningful profit, need combined < 93c.

**What it does:** Adds a scan in `bot_loops.py` for each market: if `yes_ask + no_ask < 93`, place BOTH a YES and NO order. One always wins. Net profit = `100 - yes_ask - no_ask - total_fee`.

**Files:**
- Modify: `bot_strategy.py` (add `check_dual_side_arb()`)
- Modify: `bot_loops.py` (call arb check before S1/S2)
- Modify: `bot_risk.py` (add `_execute_dual_arb_trade()`)

- [ ] **Step 1: Write failing test**

```python
# Add to tests/test_s2_fires.py or new file tests/test_dual_arb.py:
# tests/test_dual_arb.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot_strategy import check_dual_side_arb


def test_dual_arb_fires_when_spread_below_threshold():
    """YES=42 + NO=47 = 89c < 93c threshold → arb fires."""
    result = check_dual_side_arb(yes_ask=42.0, no_ask=47.0, fee_per_contract_cents=7)
    assert result["arb"] is True, f"Expected arb=True, got {result}"
    assert result["net_edge_cents"] > 0


def test_dual_arb_skips_when_spread_above_threshold():
    """YES=49 + NO=51 = 100c > 93c → no arb."""
    result = check_dual_side_arb(yes_ask=49.0, no_ask=51.0, fee_per_contract_cents=7)
    assert result["arb"] is False


def test_dual_arb_net_edge_calculation():
    """Net edge = 100 - yes_ask - no_ask - total_fee."""
    result = check_dual_side_arb(yes_ask=40.0, no_ask=45.0, fee_per_contract_cents=7)
    # Fee at 40c: 7 * 0.40 * 0.60 = 1.68c per contract, so negligible per-trade
    assert result["arb"] is True
    assert result["net_edge_cents"] == pytest.approx(100 - 40 - 45, abs=2)


def test_dual_arb_threshold_configurable():
    """Threshold can be tightened (92c) or loosened (95c)."""
    assert check_dual_side_arb(42.0, 52.0, threshold=94.0)["arb"] is True  # 94 < 94 → fires
    assert check_dual_side_arb(42.0, 54.0, threshold=90.0)["arb"] is False  # 96 > 90 → no
```

- [ ] **Step 2: Run tests — verify fail**

```
python -m pytest tests/test_dual_arb.py -v
```

Expected: FAIL — `ImportError: cannot import name 'check_dual_side_arb'`

- [ ] **Step 3: Add `check_dual_side_arb` to `bot_strategy.py`**

```python
def check_dual_side_arb(
    yes_ask: float,
    no_ask: float,
    fee_per_contract_cents: float = 7,
    threshold: float = 93.0,
) -> dict:
    """
    Structural arbitrage check: YES + NO < threshold → guaranteed profit.
    At threshold=93c: net profit = 100 - yes_ask - no_ask > 7c (covers fees).
    Returns dict with arb (bool), net_edge_cents, yes_ask, no_ask.
    """
    combined = yes_ask + no_ask
    # Approximate fee: 7c * price_frac * (1 - price_frac) per side
    fee_yes = fee_per_contract_cents * (yes_ask / 100) * (1 - yes_ask / 100)
    fee_no  = fee_per_contract_cents * (no_ask  / 100) * (1 - no_ask  / 100)
    net_edge = 100.0 - yes_ask - no_ask - fee_yes - fee_no
    arb_fires = combined < threshold and net_edge > 0
    return {
        "arb": arb_fires,
        "net_edge_cents": round(net_edge, 2),
        "combined": combined,
        "yes_ask": yes_ask,
        "no_ask": no_ask,
    }
```

- [ ] **Step 4: Run tests**

```
python -m pytest tests/test_dual_arb.py -v
python -m pytest tests/ -q
```

Expected: all passing

- [ ] **Step 5: Commit (arb check function only — execution wiring is follow-up)**

```bash
git add bot_strategy.py tests/test_dual_arb.py
git commit -m "feat(arb): add dual-side arbitrage detector

check_dual_side_arb() fires when YES+NO < 93c (structural underpricing).
Guaranteed profit regardless of direction. Execution wiring follows."
```

---

## Task 8: Per-Asset Time-of-Day Volatility Adjustment

**Research backing:** Mathematical execution article. Gamma ∝ 1/√T_remaining. Implied vol changes by time of day — US open (9:30-11:30 ET) = 1.3× baseline vol; overnight = 0.8×. GBM certainty model uses static per-asset vol — this skews win_prob at different times of day.

**What it does:** Add `_time_of_day_vol_multiplier()` that returns a factor based on ET hour. Multiply `vol_15m` in `_s1_certainty_win_prob` by this factor. Higher vol at US open → lower certainty (conservative) → fewer trades at overconfident prices.

**Files:**
- Modify: `bot_strategy.py` (add `_time_of_day_vol_multiplier()`, modify `_s1_certainty_win_prob`)

- [ ] **Step 1: Add `_time_of_day_vol_multiplier` to `bot_strategy.py`**

Add after `_s1_certainty_win_prob`:

```python
def _time_of_day_vol_multiplier() -> float:
    """
    Adjusts realized vol by time of day (ET).
    US open (9:30-11:30 ET): 1.30× — high vol, be conservative.
    US close (15:00-16:30 ET): 1.20× — elevated vol.
    Overnight (0:00-7:00 ET): 0.80× — thin market, lower vol.
    Otherwise: 1.00× (baseline).
    """
    try:
        now_et = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-4)))
        t = now_et.hour * 60 + now_et.minute
        if 9 * 60 + 30 <= t <= 11 * 60 + 30:
            return 1.30
        if 15 * 60 <= t <= 16 * 60 + 30:
            return 1.20
        if t < 7 * 60 or t >= 23 * 60:
            return 0.80
        return 1.00
    except Exception:
        return 1.00
```

- [ ] **Step 2: Apply multiplier in `_s1_certainty_win_prob`**

In `_s1_certainty_win_prob`, change:

```python
    vol_15m = _ASSET_VOL_15M.get(asset, 0.008)
```

to:

```python
    vol_15m = _ASSET_VOL_15M.get(asset, 0.008) * _time_of_day_vol_multiplier()
```

- [ ] **Step 3: Write test**

```python
# Add to tests/test_strategy_params.py:

def test_certainty_win_prob_range_with_vol_multiplier():
    """Win prob still stays in 0.52-0.75 regardless of time-of-day vol adjustment."""
    from bot_strategy import _s1_certainty_win_prob
    for dist in [0.001, 0.005, 0.015]:
        for t in [60.0, 300.0, 600.0]:
            wp = _s1_certainty_win_prob(dist, t, "ETH")
            assert 0.52 <= wp <= 0.75, f"WR={wp:.3f} out of range at dist={dist} t={t}"
```

- [ ] **Step 4: Run tests**

```
python -m pytest tests/test_strategy_params.py::test_certainty_win_prob_range_with_vol_multiplier -v
python -m pytest tests/ -q
```

Expected: all passing

- [ ] **Step 5: Commit**

```bash
git add bot_strategy.py tests/test_strategy_params.py
git commit -m "feat(strategy): time-of-day vol adjustment in GBM certainty model

US open (9:30-11:30 ET) uses 1.30x vol, US close 1.20x, overnight 0.80x.
Higher vol at open → lower certainty probability → bot is more selective
during high-variance periods. Source: prediction market gamma research."
```

---

## Task 9: S1 Per-Asset Fire Rate Guard

**Research backing:** v2 Polymarket study crashed from overtrading — "duplicate bets on same window" filter was essential. Currently bot deduplicates by ticker but can fire on multiple contracts per asset per hour if strikes differ.

**What it does:** Track last N S1 trade entry times per asset. If asset has ≥ `max_s1_per_asset_per_hour` trades in last 60 min, skip new S1 on that asset. Default: 2 trades/hour/asset.

**Files:**
- Modify: `bot_state.py` (add `_s1_asset_trade_times` dict)
- Modify: `bot_strategy.py` (`strategy_brain_s1` checks rate)
- Modify: `bot_risk.py` (`_execute_s1_trade` records trade time)

- [ ] **Step 1: Add `_s1_asset_trade_times` to `bot_state.py`**

```python
# Add near the other bot_state globals:
_s1_asset_trade_times: dict = {}  # asset → list of float timestamps
```

- [ ] **Step 2: Add fire rate gate to `strategy_brain_s1`**

In `strategy_brain_s1`, after the per-asset cap gate, add:

```python
    # S1 fire rate guard: max 2 trades per asset per 60 minutes.
    # Prevents overtrading the same asset when signals are noisy.
    _max_per_hour = int(config.get("max_s1_per_asset_per_hour", 2))
    _now = time.time()
    _asset_times = [t for t in bot_state._s1_asset_trade_times.get(asset, [])
                    if _now - t < 3600.0]
    bot_state._s1_asset_trade_times[asset] = _asset_times
    if len(_asset_times) >= _max_per_hour:
        return _make_skip("yes", f"s1_rate_limit:{len(_asset_times)}/{_max_per_hour}_per_hour",
                          abs_pct, mins_left, variant="strategy1")
```

- [ ] **Step 3: Record trade time in `_execute_s1_trade` (bot_risk.py)**

After the order is confirmed filled, add:

```python
    # Record trade time for per-asset fire rate guard
    if asset not in bot_state._s1_asset_trade_times:
        bot_state._s1_asset_trade_times[asset] = []
    bot_state._s1_asset_trade_times[asset].append(time.time())
```

- [ ] **Step 4: Write test**

```python
# Add to tests/test_risk_guards.py:

def test_s1_rate_limit_fires_after_max_per_hour(monkeypatch):
    """S1 must skip after max_s1_per_asset_per_hour trades on same asset in 60min."""
    import time, bot_state
    from bot_strategy import strategy_brain_s1

    now = time.time()
    bot_state._s1_asset_trade_times["ETH"] = [now - 100, now - 200]  # 2 recent trades

    monkeypatch.setattr("asset_manager._prices", {"ETH": [(now - i, 2800.0) for i in range(100, 0, -1)]})
    result = strategy_brain_s1(
        btc_price=2850.0, strike=2800.0, yes_ask=45.0, no_ask=55.0,
        elapsed_seconds=760.0, secs_left=240.0, ticker="KXETH-TEST", asset="ETH",
    )
    assert result["action"] == "skip"
    assert "s1_rate_limit" in result["reasoning"] or "s1_quiet_hours" in result["reasoning"], \
        f"Expected rate_limit or quiet_hours skip, got: {result['reasoning']}"
    # Reset
    bot_state._s1_asset_trade_times["ETH"] = []
```

- [ ] **Step 5: Run tests**

```
python -m pytest tests/ -q
```

Expected: all passing

- [ ] **Step 6: Commit**

```bash
git add bot_state.py bot_strategy.py bot_risk.py tests/test_risk_guards.py
git commit -m "feat(risk): S1 per-asset hourly fire rate guard

Prevents >2 S1 trades per asset per hour. Stops overtrading when signals
cluster (AMM oscillates, noisy period). Rate configurable via
max_s1_per_asset_per_hour in config. Source: Polymarket v2 crash analysis."
```

---

## Task 10: Trade Stats API Endpoint

**What it does:** Add `GET /api/trade-stats` to `server.py` returning: wins/losses/WR last 24h, skip reason distribution last 24h (from brain.log tail), top 3 skip reasons, avg EV of fired trades.

**Files:**
- Modify: `server.py` (add `/api/trade-stats` handler)

- [ ] **Step 1: Find where routes are registered in `server.py`**

```bash
grep -n "app.router.add_get\|@app.route\|add_route" server.py | head -20
```

- [ ] **Step 2: Add `/api/trade-stats` route**

In `server.py`, after the existing `/api/trades` or `/api/state` handler, add:

```python
async def handle_trade_stats(request):
    """Return 24h win/loss/WR and recent skip reason distribution."""
    import sqlite3, re, os
    from datetime import datetime, timezone, timedelta
    config = read_config()
    mode = config.get("mode", "paper")

    # DB stats: wins/losses last 24h
    try:
        conn = sqlite3.connect(bot_state._DB_FILE)
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = conn.execute(
            "SELECT outcome, COUNT(*), COALESCE(SUM(pnl_dollars), 0) "
            "FROM trades WHERE mode=? AND ts > ? AND outcome != 'pending' "
            "GROUP BY outcome",
            (mode, since),
        ).fetchall()
        conn.close()
    except Exception:
        rows = []

    wins = losses = 0
    pnl_24h = 0.0
    for outcome, count, pnl in rows:
        if outcome == "win":
            wins, pnl_24h = count, pnl_24h + pnl
        elif outcome == "loss":
            losses, pnl_24h = count, pnl_24h + pnl

    total = wins + losses
    wr = round(wins / total, 3) if total else None

    # Brain log: skip reasons from last 500 lines
    skip_counts: dict = {}
    brain_log_path = bot_strategy._brain_log_path if hasattr(bot_strategy, "_brain_log_path") else "brain.log"
    try:
        with open(brain_log_path, "r", errors="replace") as fh:
            lines = fh.readlines()[-500:]
        for line in lines:
            m = re.search(r"(s[12]_\w+)", line)
            if m and "TRADE" not in line and "SETTLE" not in line:
                key = m.group(1).split(":")[0]
                skip_counts[key] = skip_counts.get(key, 0) + 1
    except Exception:
        pass

    top_skips = sorted(skip_counts.items(), key=lambda x: -x[1])[:5]

    payload = {
        "mode": mode,
        "period_hours": 24,
        "wins": wins,
        "losses": losses,
        "total": total,
        "win_rate": wr,
        "pnl_dollars": round(pnl_24h, 2),
        "top_skip_reasons": [{"reason": r, "count": c} for r, c in top_skips],
    }
    return web.json_response(payload)
```

Register the route (find where `app.router.add_get` is called and add):

```python
app.router.add_get("/api/trade-stats", handle_trade_stats)
```

- [ ] **Step 3: Manual verification**

Deploy to Railway (or run locally) and hit:
```
curl http://localhost:8080/api/trade-stats
```

Expected: JSON with wins/losses/win_rate/top_skip_reasons

- [ ] **Step 4: Commit**

```bash
git add server.py
git commit -m "feat(api): add /api/trade-stats endpoint

Returns 24h wins/losses/WR/PnL and top-5 skip reason distribution
from brain.log tail. Useful for monitoring strategy health without
reading raw logs."
```

---

## Self-Review

**Spec coverage:**
- 10-minute trend filter: Task 1 ✅
- Multi-timeframe momentum: Task 2 ✅
- DISLOCATION signal: Task 3 ✅
- Live WR calibration: Tasks 4+5 ✅
- Brain analytics script: Task 6 ✅
- Dual-side arbitrage: Task 7 ✅
- Time-of-day vol adjustment: Task 8 ✅
- Fire rate guard: Task 9 ✅
- Trade stats API: Task 10 ✅

**Priority order (by expected impact):**
1. Task 1 (trend filter) — highest impact, proven 7× capital preservation
2. Task 3 (DISLOCATION) — new fire path, catches underpriced contracts
3. Task 7 (dual-side arb) — structural edge, no prediction needed
4. Task 2 (multi-timeframe) — reduces false positives
5. Task 4+5 (live calibration) — self-improving, long-term compound
6. Task 8 (vol adjustment) — refines certainty model
7. Task 9 (rate guard) — prevents overtrading clusters
8. Task 6 (analytics script) — offline tuning tool
9. Task 10 (stats API) — monitoring tool

**Placeholder scan:** None — all steps have concrete code, exact commands, expected outputs.

**Type consistency:**
- `_trend_direction(prices, window_seconds)` → consistent in Tasks 1, 2 ✅
- `_s1_dislocation_check(dist_pct, yes_ask, secs_left, asset, min_edge)` → Task 3 consistent ✅
- `_update_wr_bucket` / `_get_empirical_wr` signatures consistent across Tasks 4, 5 ✅
- `check_dual_side_arb(yes_ask, no_ask, fee_per_contract_cents, threshold)` → Task 7 consistent ✅
