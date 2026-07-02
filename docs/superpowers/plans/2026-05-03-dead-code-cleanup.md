# Dead Code Cleanup + Signal Extractor Fix - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete all pre-ultraplan strategy code and rewrite the research signal extractor to test the five actual voters from `compute_15m_signal`.

**Architecture:** Pure deletion + targeted rewrite. No new abstractions. Dead files removed, bot.py simplified to a single FifteenMinStrategy dispatch path, signal_extractor.py fully replaced with V1-V5 extractors that mirror the live signal logic.

**Tech Stack:** Python, pytest, numpy, pandas, scipy

---

### Task 1: Delete 18 dead signal files

**Files:**
- Delete: `src/strategies/signals/supertrend.py`
- Delete: `src/strategies/signals/exhaustion_fade.py`
- Delete: `src/strategies/signals/ratio_divergence.py`
- Delete: `src/strategies/signals/rolling_beta.py`
- Delete: `src/strategies/signals/variance_ratio.py`
- Delete: `src/strategies/signals/volume_spike.py`
- Delete: `src/strategies/signals/btc_context.py`
- Delete: `src/strategies/signals/correlation_monitor.py`
- Delete: `src/strategies/signals/event_calendar.py`
- Delete: `src/strategies/signals/kalshi_velocity.py`
- Delete: `src/strategies/signals/taper.py`
- Delete: `src/strategies/signals/intraday_signals.py`
- Delete: `src/strategies/signals/solana_health.py`
- Delete: `src/strategies/signals/funding_dispersion.py`
- Delete: `src/strategies/signals/beta_cache.py`
- Delete: `src/strategies/signals/idiosyncratic_detector.py`
- Delete: `src/strategies/signals/btc_diurnal_obi.py`
- Delete: `src/strategies/signals/session_clock.py`

Do NOT delete: `time_windows.py`, `black_scholes.py`, `fifteen_min_signal.py` - these are imported by the live bot.

- [ ] **Step 1: Confirm the 18 files exist**

```bash
ls src/strategies/signals/
```

Expected: all 18 files listed above are present alongside `black_scholes.py`, `fifteen_min_signal.py`, `time_windows.py`.

- [ ] **Step 2: Delete the 18 files**

```bash
cd src/strategies/signals && \
  rm supertrend.py exhaustion_fade.py ratio_divergence.py rolling_beta.py \
     variance_ratio.py volume_spike.py btc_context.py correlation_monitor.py \
     event_calendar.py kalshi_velocity.py taper.py intraday_signals.py \
     solana_health.py funding_dispersion.py beta_cache.py idiosyncratic_detector.py \
     btc_diurnal_obi.py session_clock.py
```

- [ ] **Step 3: Verify surviving signals files**

```bash
ls src/strategies/signals/
```

Expected output contains exactly: `__init__.py`, `black_scholes.py`, `fifteen_min_signal.py`, `time_windows.py` (plus `__pycache__` if present).

- [ ] **Step 4: Run the strategy tests that do NOT import deleted files**

```bash
pytest tests/strategies/test_base.py tests/strategies/test_calibration.py \
  tests/strategies/test_utilities.py tests/strategies/test_skip_layer.py \
  tests/strategies/test_ev.py tests/strategies/test_entry_range.py \
  tests/strategies/test_fees.py tests/strategies/test_fifteen_min_strategy.py \
  tests/strategies/test_base_15m_replay.py tests/strategies/test_base_strategy.py \
  -v
```

Expected: all pass. If any test fails with `ModuleNotFoundError` on a deleted file, that test file must be deleted in Task 2 (it imports dead code).

- [ ] **Step 5: Commit**

```bash
git add -A src/strategies/signals/
git commit -m "feat: delete 18 dead pre-ultraplan signal files"
```

---

### Task 2: Delete 5 dead strategy files and 7 dead test files

**Files:**
- Delete: `src/strategies/btc_strategy.py`
- Delete: `src/strategies/sol_strategy.py`
- Delete: `src/strategies/xrp_strategy.py`
- Delete: `src/strategies/doge_strategy.py`
- Delete: `src/strategies/baseline.py`
- Delete: `tests/strategies/test_signals.py`
- Delete: `tests/strategies/test_supertrend.py`
- Delete: `tests/strategies/test_baseline.py`
- Delete: `tests/strategies/test_btc_strategy.py`
- Delete: `tests/strategies/test_sol_strategy.py`
- Delete: `tests/strategies/test_xrp_strategy.py`
- Delete: `tests/strategies/test_doge_strategy.py`

- [ ] **Step 1: Delete the 5 dead strategy files**

```bash
cd src/strategies && \
  rm btc_strategy.py sol_strategy.py xrp_strategy.py doge_strategy.py baseline.py
```

- [ ] **Step 2: Delete the 7 dead test files**

```bash
cd tests/strategies && \
  rm test_signals.py test_supertrend.py test_baseline.py \
     test_btc_strategy.py test_sol_strategy.py test_xrp_strategy.py test_doge_strategy.py
```

- [ ] **Step 3: Run the surviving strategy tests**

```bash
pytest tests/strategies/ -v
```

Expected: all 10 surviving test files pass. The deleted test files are gone so pytest won't try to collect them.

- [ ] **Step 4: Commit**

```bash
git add -A src/strategies/ tests/strategies/
git commit -m "feat: delete 5 dead per-asset strategies and 7 dead test files"
```

---

### Task 3: Collapse bot.py strategy dispatch to FifteenMinStrategy

**Files:**
- Modify: `bot.py`

The `_get_or_make_strategy` function at ~line 1700 has a 5-branch if/elif that imports BTCStrategy, SOLStrategy, XRPStrategy, DOGEStrategy for specific assets and falls back to FifteenMinStrategy for others. Replace the entire if/elif with a single FifteenMinStrategy instantiation for all assets.

- [ ] **Step 1: Locate the dispatch block**

```bash
grep -n "BTCStrategy\|SOLStrategy\|XRPStrategy\|DOGEStrategy" bot.py
```

Expected: lines ~1700-1724 show the 5 import branches.

- [ ] **Step 2: Replace the if/elif block**

Find this block in `bot.py` (lines ~1700-1730):

```python
        if asset == "BTC":
            from src.strategies.btc_strategy import BTCStrategy
            strat = BTCStrategy(skip_config=skip_cfg, min_ev=min_ev, stake_dollars=stake)
        elif asset == "ETH":
            from strategies.fifteen_min_strategy import FifteenMinStrategy
            strat = FifteenMinStrategy(
                asset=asset,
                skip_config=skip_cfg,
                min_ev=min_ev,
                stake_dollars=stake,
                confidence_threshold=confidence_threshold,
                supertrend_atr_period=st_period,
                supertrend_atr_multiplier=st_mult,
                momentum_lookback=mom_lookback,
            )
        elif asset == "SOL":
            from src.strategies.sol_strategy import SOLStrategy
            strat = SOLStrategy(skip_config=skip_cfg, min_ev=min_ev, stake_dollars=stake)
        elif asset == "XRP":
            from src.strategies.xrp_strategy import XRPStrategy
            strat = XRPStrategy(skip_config=skip_cfg, min_ev=min_ev, stake_dollars=stake)
        elif asset == "DOGE":
            from src.strategies.doge_strategy import DOGEStrategy
            strat = DOGEStrategy(skip_config=skip_cfg, min_ev=min_ev, stake_dollars=stake)
        else:
            from strategies.fifteen_min_strategy import FifteenMinStrategy
            strat = FifteenMinStrategy(
                asset=asset,
                skip_config=skip_cfg,
                min_ev=min_ev,
                stake_dollars=stake,
                confidence_threshold=confidence_threshold,
                supertrend_atr_period=st_period,
                supertrend_atr_multiplier=st_mult,
                momentum_lookback=mom_lookback,
            )
```

Replace with:

```python
        from strategies.fifteen_min_strategy import FifteenMinStrategy
        strat = FifteenMinStrategy(
            asset=asset,
            skip_config=skip_cfg,
            min_ev=min_ev,
            stake_dollars=stake,
            confidence_threshold=confidence_threshold,
            supertrend_atr_period=st_period,
            supertrend_atr_multiplier=st_mult,
            momentum_lookback=mom_lookback,
        )
```

- [ ] **Step 3: Confirm no dead imports remain**

```bash
grep -n "BTCStrategy\|SOLStrategy\|XRPStrategy\|DOGEStrategy\|btc_strategy\|sol_strategy\|xrp_strategy\|doge_strategy" bot.py
```

Expected: no output.

- [ ] **Step 4: Import-check bot.py**

```bash
python -c "import bot" 2>&1 | head -20
```

Expected: no `ModuleNotFoundError` or `ImportError`. (Other errors about missing env vars or async context are fine.)

- [ ] **Step 5: Run strategy tests (sanity)**

```bash
pytest tests/strategies/ -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add bot.py
git commit -m "feat: collapse strategy dispatch to FifteenMinStrategy for all assets"
```

---

### Task 4: Write new failing tests for signal_extractor (TDD - red phase)

**Files:**
- Modify: `tests/backtesting/research/test_signal_extractor.py`

Replace all existing tests with tests that assert V1-V5 signal names and behavior. These tests will FAIL because `signal_extractor.py` still has the old SIGNAL_NAMES list.

- [ ] **Step 1: Overwrite the test file with new tests**

Replace the entire contents of `tests/backtesting/research/test_signal_extractor.py`:

```python
import numpy as np
import pandas as pd
from backtesting.research.signal_extractor import extract_all_signals, SIGNAL_NAMES


def _bars(n=120, trend=0.0, seed=42):
    rng = np.random.default_rng(seed)
    prices = [100_000.0 + trend * i + rng.normal(0, 50) for i in range(n)]
    return pd.DataFrame({
        'close': prices, 'open': prices,
        'high': [p + 20 for p in prices],
        'low': [p - 20 for p in prices],
        'volume': np.ones(n),
    })


def test_returns_all_signal_names():
    result = extract_all_signals(_bars(), strike=100_000.0, asset='BTC')
    assert set(result.keys()) == set(SIGNAL_NAMES)
    for name in SIGNAL_NAMES:
        assert name in result, f"Missing signal: {name}"


def test_predictions_are_probabilities():
    result = extract_all_signals(_bars(), strike=100_000.0, asset='BTC')
    for name, preds in result.items():
        assert isinstance(preds, np.ndarray), f"{name} not ndarray"
        assert np.all((preds >= 0.0) & (preds <= 1.0)), f"{name} out-of-range values"


def test_output_length_matches_bars():
    result = extract_all_signals(_bars(n=90), strike=100_000.0, asset='BTC')
    for name, preds in result.items():
        assert len(preds) == 90, f"{name} length mismatch"


def test_v2_inverted_downtrend_produces_high_prob():
    # Strong downtrend → MTF momentum negative → V2 inverted → 0.65 (YES)
    result = extract_all_signals(_bars(n=200, trend=-500.0), strike=200_000.0, asset='BTC')
    v2 = result['v2_mtf_momentum']
    late = v2[50:]
    assert np.any(late == 0.65), "Expected v2_mtf_momentum=0.65 in downtrend (inverted)"


def test_v3_inverted_downtrend_produces_high_prob():
    # Strong downtrend → RSI oversold → V3 inverted → 0.65 (YES)
    result = extract_all_signals(_bars(n=200, trend=-500.0), strike=200_000.0, asset='BTC')
    v3 = result['v3_rsi']
    late = v3[30:]
    assert np.any(late == 0.65), "Expected v3_rsi=0.65 (oversold) in downtrend"


def test_v5_fires_at_least_as_often_as_v2():
    # V5 threshold = MTF_threshold/2 so it fires more often than V2
    result = extract_all_signals(_bars(n=200, trend=-500.0), strike=200_000.0, asset='BTC')
    v2 = result['v2_mtf_momentum']
    v5 = result['v5_mtf_magnitude']
    # Where V2 fires YES, V5 must also fire YES (V5 has a lower threshold)
    v2_yes_mask = v2 == 0.65
    assert np.all(v5[v2_yes_mask] == 0.65), "V5 must fire YES everywhere V2 fires YES"


def test_asset_thresholds_differ():
    # BTC RSI threshold=5.0, SOL=10.0 - both should run cleanly and return correct shapes
    result_btc = extract_all_signals(_bars(n=200, trend=-100.0), strike=100_000.0, asset='BTC')
    result_sol = extract_all_signals(_bars(n=200, trend=-100.0), strike=100_000.0, asset='SOL')
    for name in SIGNAL_NAMES:
        assert len(result_btc[name]) == 200, f"BTC {name} length wrong"
        assert len(result_sol[name]) == 200, f"SOL {name} length wrong"
```

- [ ] **Step 2: Run tests - confirm they FAIL**

```bash
pytest tests/backtesting/research/test_signal_extractor.py -v
```

Expected: `FAILED test_returns_all_signal_names` (because extractor still has old names). If the test unexpectedly passes, double-check that `SIGNAL_NAMES` in the extractor still contains the old values before proceeding.

---

### Task 5: Rewrite signal_extractor.py to make tests pass (TDD - green phase)

**Files:**
- Modify: `backtesting/research/signal_extractor.py`

Replace the entire file. The three helper functions (`_rsi`, `_boll_zscore`, `_multi_tf_mom`) are copied verbatim from `src/strategies/signals/fifteen_min_signal.py` to keep the extractor self-contained for batch bar processing.

- [ ] **Step 1: Overwrite signal_extractor.py**

Replace the entire contents of `backtesting/research/signal_extractor.py`:

```python
"""
Runs each live voter from compute_15m_signal independently on historical 1-min bars,
returning p_yes predictions for IC analysis.
"""
from __future__ import annotations

import logging
import math
import os
import sys
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

_log = logging.getLogger(__name__)

_SRC = os.path.join(os.path.dirname(__file__), '..', '..', 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

SIGNAL_NAMES = [
    'v1_bs_prob',
    'v2_mtf_momentum',
    'v3_rsi',
    'v4_bollinger',
    'v5_mtf_magnitude',
]

# Mirrored from fifteen_min_signal.py - update both if thresholds change
_MTF_THRESHOLDS  = {'BTC': 0.0005, 'ETH': 0.0005, 'SOL': 0.0005, 'XRP': 0.0005}
_RSI_THRESHOLDS  = {'BTC': 5.0,    'ETH': 8.0,     'SOL': 10.0,   'XRP': 8.0}
_BOLL_THRESHOLDS = {'BTC': 0.75,   'ETH': 0.50,    'SOL': 0.50,   'XRP': 0.35}
_MTF_DEFAULT     = 0.0005
_RSI_DEFAULT     = 8.0
_BOLL_DEFAULT    = 0.50


# ── helpers copied verbatim from fifteen_min_signal.py ─────────────────────────

def _rsi(prices: List[float], period: int = 14) -> Optional[float]:
    if len(prices) < period + 2:
        return None
    changes = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains  = [max(0.0, c) for c in changes]
    losses = [max(0.0, -c) for c in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _boll_zscore(prices: List[float], period: int = 20) -> Optional[float]:
    if len(prices) < period:
        return None
    recent = prices[-period:]
    mean_p = sum(recent) / len(recent)
    var_p  = sum((p - mean_p) ** 2 for p in recent) / (len(recent) - 1)
    std_p  = math.sqrt(var_p) if var_p > 0 else 0.0
    if std_p <= 0:
        return None
    return (prices[-1] - mean_p) / std_p


def _multi_tf_mom(prices: List[float]) -> Optional[float]:
    if len(prices) < 31:
        return None
    cur = prices[-1]
    if cur <= 0:
        return None
    r5  = (cur - prices[-6])  / prices[-6]  if prices[-6]  > 0 else 0.0
    r15 = (cur - prices[-16]) / prices[-16] if prices[-16] > 0 else 0.0
    r30 = (cur - prices[-31]) / prices[-31] if prices[-31] > 0 else 0.0
    return (r5 + r15 + r30) / 3.0


# ── per-voter extractors ───────────────────────────────────────────────────────

def _v1_predictions(bars: pd.DataFrame, strike: float, seconds_left: float = 900.0) -> np.ndarray:
    """V1: BS p_yes directly (continuous)."""
    try:
        from strategies.signals.black_scholes import compute_bs_p_yes
        prices = bars['close'].values
        log_ret = np.diff(np.log(np.maximum(prices, 1e-8)))
        vol_1m = float(np.std(log_ret)) if len(log_ret) > 5 else 0.01
        result = np.full(len(prices), 0.5)
        for i, p in enumerate(prices):
            v = compute_bs_p_yes(
                current_price=p, strike=strike,
                realized_vol_1min=vol_1m, seconds_left=seconds_left,
            )
            if v is not None:
                result[i] = v
        return result
    except Exception as exc:
        _log.warning("_v1_predictions failed: %s", exc)
        return np.full(len(bars), 0.5)


def _v2_predictions(bars: pd.DataFrame, asset: str) -> np.ndarray:
    """V2: MTF momentum inverted - negative momentum → 0.65 (YES)."""
    T = _MTF_THRESHOLDS.get(asset.upper(), _MTF_DEFAULT)
    prices = bars['close'].tolist()
    n = len(prices)
    preds = np.full(n, 0.5)
    for i in range(30, n):
        mtf = _multi_tf_mom(prices[:i + 1])
        if mtf is None:
            continue
        if mtf < -T:
            preds[i] = 0.65
        elif mtf > T:
            preds[i] = 0.35
    return preds


def _v3_predictions(bars: pd.DataFrame, asset: str) -> np.ndarray:
    """V3: RSI deviation inverted - oversold (rsi_dev < -T) → 0.65 (YES)."""
    T = _RSI_THRESHOLDS.get(asset.upper(), _RSI_DEFAULT)
    prices = bars['close'].tolist()
    n = len(prices)
    preds = np.full(n, 0.5)
    for i in range(15, n):
        rsi = _rsi(prices[:i + 1])
        if rsi is None:
            continue
        rsi_dev = rsi - 50.0
        if rsi_dev < -T:
            preds[i] = 0.65
        elif rsi_dev > T:
            preds[i] = 0.35
    return preds


def _v4_predictions(bars: pd.DataFrame, asset: str) -> np.ndarray:
    """V4: Bollinger z-score inverted - below lower band (z < -T) → 0.65 (YES)."""
    T = _BOLL_THRESHOLDS.get(asset.upper(), _BOLL_DEFAULT)
    prices = bars['close'].tolist()
    n = len(prices)
    preds = np.full(n, 0.5)
    for i in range(19, n):
        boll = _boll_zscore(prices[:i + 1])
        if boll is None:
            continue
        if boll < -T:
            preds[i] = 0.65
        elif boll > T:
            preds[i] = 0.35
    return preds


def _v5_predictions(bars: pd.DataFrame, asset: str) -> np.ndarray:
    """V5: MTF magnitude soft confirmation - uses T/2 threshold, inverted."""
    T = _MTF_THRESHOLDS.get(asset.upper(), _MTF_DEFAULT) / 2.0
    prices = bars['close'].tolist()
    n = len(prices)
    preds = np.full(n, 0.5)
    for i in range(30, n):
        mtf = _multi_tf_mom(prices[:i + 1])
        if mtf is None:
            continue
        if mtf < -T:
            preds[i] = 0.65
        elif mtf > T:
            preds[i] = 0.35
    return preds


def extract_all_signals(
    bars: pd.DataFrame,
    strike: float,
    asset: str,
) -> Dict[str, np.ndarray]:
    """
    Returns {signal_name: np.ndarray of p_yes} for all SIGNAL_NAMES.
    Each array has the same length as bars. Values clipped to [0, 1].
    """
    return {
        'v1_bs_prob':       np.clip(_v1_predictions(bars, strike), 0.0, 1.0),
        'v2_mtf_momentum':  np.clip(_v2_predictions(bars, asset), 0.0, 1.0),
        'v3_rsi':           np.clip(_v3_predictions(bars, asset), 0.0, 1.0),
        'v4_bollinger':     np.clip(_v4_predictions(bars, asset), 0.0, 1.0),
        'v5_mtf_magnitude': np.clip(_v5_predictions(bars, asset), 0.0, 1.0),
    }
```

- [ ] **Step 2: Run tests - confirm they PASS**

```bash
pytest tests/backtesting/research/test_signal_extractor.py -v
```

Expected: all 7 tests pass. If `test_v2_inverted_downtrend_produces_high_prob` fails, check that `_MTF_THRESHOLDS['BTC'] = 0.0005` - the bars have `trend=-500.0` per step so MTF should far exceed that threshold.

- [ ] **Step 3: Run full test suite to check no regressions**

```bash
pytest tests/ -v 2>&1 | tail -30
```

Expected: only tests that were already present and passing continue to pass. No new failures.

- [ ] **Step 4: Commit**

```bash
git add backtesting/research/signal_extractor.py tests/backtesting/research/test_signal_extractor.py
git commit -m "feat: rewrite signal_extractor with V1-V5 voters from compute_15m_signal"
```

---

### Task 6: Re-run Layer 1 validation with correct signals

**Files:**
- Read: `backtesting/output/research/BTC/research_report.md` (after run)

- [ ] **Step 1: Run Layer 1 for BTC**

```bash
python backtesting/research_cli.py --asset BTC --layers 1 --iters 1000
```

This will take a few minutes (4.5M bars × 5 signals, but the rolling IC is vectorized so it should be fast).

Expected output: Layer 1 verdict printed with IC results for `v1_bs_prob`, `v2_mtf_momentum`, `v3_rsi`, `v4_bollinger`, `v5_mtf_magnitude`.

- [ ] **Step 2: Check the report**

```bash
cat backtesting/output/research/BTC/research_report.md
```

Expected: 5 signal rows in the IC table, all using the V1-V5 names. Record the IC, ICIR, and t-stat for each signal.

- [ ] **Step 3: Commit the report**

```bash
git add backtesting/output/research/BTC/research_report.md
git commit -m "research: BTC Layer 1 IC results for V1-V5 signals (correct voters)"
```

---

## Self-Review

**Spec coverage:**
- Part 1 (delete dead code): Tasks 1-3 cover all 18 signal files, 5 strategy files, 7 test files, and bot.py dispatch.
- Part 2 (rewrite extractor): Tasks 4-5 fully rewrite the extractor and its tests with TDD.
- Part 3 (re-run validation): Task 6 runs the CLI and commits results.

**Placeholder scan:** None found - all steps contain exact file paths, exact commands, and complete code.

**Type consistency:** `extract_all_signals(bars, strike, asset)` signature unchanged between old and new - callers in `layer1.py` and `research_cli.py` are unaffected.

**Out-of-scope items confirmed NOT included:**
- `compute_15m_signal` is untouched
- Thresholds and strategy parameters are untouched
- Backtest scripts in `scripts/` that reference old signals are not touched
- `layer1.py`, `layer2.py`, `layer3.py`, `layer4.py`, `layer5.py` are untouched
