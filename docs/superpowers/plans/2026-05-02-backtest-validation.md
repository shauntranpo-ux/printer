# Backtest Validation System (5-Layer) - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a five-layer quant research pipeline that produces a statistical verdict on whether the D3-hybrid strategy has real predictive edge or is overfitted noise.

**Architecture:** Each layer is a standalone module in `backtesting/research/`. They share a label builder and IC math library, and are orchestrated by `backtesting/research_cli.py`. Each layer returns a dict; the report writer collates them into `backtesting/output/research/{asset}/research_report.md` + `research.json`.

**Tech Stack:** Python 3.11, numpy, scipy, pandas, pytest. Reuses `backtesting/data/loaders.load_bars`, `backtesting/metrics/trading.sharpe_ratio`, `backtesting/validation/cpcv.get_cpcv_splits`.

---

## File Map

| File | Role |
|------|------|
| `backtesting/research/__init__.py` | Package |
| `backtesting/research/ic_analysis.py` | IC / ICIR / t-stat math (Layer 1) |
| `backtesting/research/signal_extractor.py` | Per-signal p_yes predictions from bars |
| `backtesting/research/layer1.py` | Layer 1 runner |
| `backtesting/research/layer2.py` | Lookahead audit + null simulator (Layer 2) |
| `backtesting/research/layer3.py` | DSR + PBO + MinBTL (Layer 3) |
| `backtesting/research/layer4.py` | Trade-level permutation test (Layer 4) |
| `backtesting/research/layer5.py` | Variance ratio regime breakdown (Layer 5) |
| `backtesting/research/report_writer.py` | Writes research_report.md + research.json |
| `backtesting/research_cli.py` | CLI: `python backtesting/research_cli.py --asset BTC` |
| `tests/backtesting/research/__init__.py` | Test package |
| `tests/backtesting/research/test_ic_analysis.py` | |
| `tests/backtesting/research/test_signal_extractor.py` | |
| `tests/backtesting/research/test_layer1.py` | |
| `tests/backtesting/research/test_layer2.py` | |
| `tests/backtesting/research/test_layer3.py` | |
| `tests/backtesting/research/test_layer4.py` | |
| `tests/backtesting/research/test_layer5.py` | |
| `tests/backtesting/research/test_report_writer.py` | |

---

## Task 1: Package Scaffold + IC Math Library

**Files:**
- Create: `backtesting/research/__init__.py`
- Create: `backtesting/research/ic_analysis.py`
- Create: `tests/backtesting/research/__init__.py`
- Create: `tests/backtesting/research/test_ic_analysis.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/backtesting/research/test_ic_analysis.py
import numpy as np
import pytest
from backtesting.research.ic_analysis import (
    compute_ic, compute_icir, compute_ic_tstat,
    compute_rolling_ic, evaluate_signal, ICResult,
)

def test_compute_ic_perfect():
    pred = np.array([0.8, 0.3, 0.7, 0.2, 0.9])
    out  = np.array([1,   0,   1,   0,   1  ])
    assert compute_ic(pred, out) > 0.9

def test_compute_ic_noise():
    rng = np.random.default_rng(42)
    pred = rng.uniform(0, 1, 500)
    out  = rng.integers(0, 2, 500)
    assert abs(compute_ic(pred, out)) < 0.15

def test_compute_ic_inverse():
    pred = np.array([0.8, 0.3, 0.7, 0.2, 0.9])
    out  = np.array([0,   1,   0,   1,   0  ])
    assert compute_ic(pred, out) < -0.9

def test_compute_icir_stable():
    ic_series = np.array([0.05, 0.06, 0.04, 0.05, 0.07])
    assert compute_icir(ic_series) > 0.5

def test_compute_icir_noisy():
    ic_series = np.array([0.05, -0.04, 0.06, -0.03, 0.07])
    assert abs(compute_icir(ic_series)) < 0.5

def test_ic_tstat_formula():
    assert compute_ic_tstat(0.05, 400) == pytest.approx(1.0, abs=0.01)

def test_evaluate_signal_fail_on_noise():
    rng = np.random.default_rng(99)
    pred = rng.uniform(0, 1, 200)
    out  = rng.integers(0, 2, 200)
    lag_outs = {1: out, 2: out, 4: out, 8: out}
    result = evaluate_signal(pred, out, lag_outs)
    assert isinstance(result, ICResult)
    assert result.verdict == "FAIL"

def test_evaluate_signal_pass_on_real_signal():
    rng = np.random.default_rng(0)
    true_prob = rng.uniform(0.4, 0.7, 1000)
    out = (rng.uniform(0, 1, 1000) < true_prob).astype(int)
    pred = np.clip(true_prob + rng.normal(0, 0.03, 1000), 0, 1)
    lag_outs = {1: out, 2: out, 4: out, 8: out}
    result = evaluate_signal(pred, out, lag_outs)
    assert result.verdict in ("PASS", "CONDITIONAL")
    assert len(result.ic_decay) == 4
```

- [ ] **Step 2: Run - expect ImportError (module doesn't exist)**

```
pytest tests/backtesting/research/test_ic_analysis.py -v
```

Expected: `ModuleNotFoundError: No module named 'backtesting.research'`

- [ ] **Step 3: Create package and implement IC math**

```python
# backtesting/research/__init__.py
# (empty)
```

```python
# backtesting/research/ic_analysis.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List
import numpy as np
from scipy import stats


@dataclass
class ICResult:
    ic: float
    icir: float
    t_stat: float
    ic_decay: List[float]   # IC at lags [1, 2, 4, 8]
    n_obs: int
    verdict: str            # PASS | CONDITIONAL | FAIL


def compute_ic(predicted: np.ndarray, outcomes: np.ndarray) -> float:
    """Spearman rank correlation between p_yes predictions and binary outcomes."""
    if len(predicted) < 5:
        return 0.0
    rho, _ = stats.spearmanr(predicted, outcomes)
    return float(rho) if not np.isnan(rho) else 0.0


def compute_icir(ic_series: np.ndarray) -> float:
    """ICIR = mean(IC) / std(IC). Returns 0 if std is zero."""
    if len(ic_series) < 2:
        return 0.0
    std = np.std(ic_series)
    return float(np.mean(ic_series) / std) if std > 0 else 0.0


def compute_ic_tstat(ic: float, n: int) -> float:
    """t-stat = IC * sqrt(N). Threshold > 2.0 indicates significance."""
    return ic * np.sqrt(n)


def compute_rolling_ic(
    predicted: np.ndarray,
    outcomes: np.ndarray,
    window: int = 30,
) -> np.ndarray:
    """Rolling IC computed over windows of size `window`."""
    ics = [
        compute_ic(predicted[i - window:i], outcomes[i - window:i])
        for i in range(window, len(predicted) + 1)
    ]
    return np.array(ics)


def evaluate_signal(
    predicted: np.ndarray,
    outcomes: np.ndarray,
    outcome_by_lag: Dict[int, np.ndarray],
    rolling_window: int = 30,
) -> ICResult:
    """Full IC evaluation for one signal against binary outcomes."""
    ic = compute_ic(predicted, outcomes)
    rolling_ics = compute_rolling_ic(predicted, outcomes, window=min(rolling_window, len(predicted) // 3))
    icir = compute_icir(rolling_ics)
    t_stat = compute_ic_tstat(ic, len(predicted))
    ic_decay = [
        compute_ic(predicted[:len(outcome_by_lag[lag])], outcome_by_lag[lag])
        for lag in [1, 2, 4, 8]
    ]

    if t_stat > 2.0 and icir > 0.30:
        verdict = "PASS"
    elif t_stat > 1.5 or icir > 0.20:
        verdict = "CONDITIONAL"
    else:
        verdict = "FAIL"

    return ICResult(ic=ic, icir=icir, t_stat=t_stat, ic_decay=ic_decay,
                    n_obs=len(predicted), verdict=verdict)
```

Also create `tests/backtesting/research/__init__.py` (empty).

- [ ] **Step 4: Run - expect all green**

```
pytest tests/backtesting/research/test_ic_analysis.py -v
```

Expected: `8 passed`

- [ ] **Step 5: Commit**

```
git add backtesting/research/__init__.py backtesting/research/ic_analysis.py tests/backtesting/research/__init__.py tests/backtesting/research/test_ic_analysis.py
git commit -m "feat: IC/ICIR math library for Layer 1 signal validation"
```

---

## Task 2: Label Builder for Binary Outcomes

**Files:**
- Create: `backtesting/research/label_builder.py`
- Create: `tests/backtesting/research/test_label_builder.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/backtesting/research/test_label_builder.py
import pandas as pd
import numpy as np
import pytest
from backtesting.research.label_builder import build_binary_labels, build_lagged_labels, STRIKE_SPACING

def _bars(prices, freq='1min'):
    idx = pd.date_range('2025-01-01 00:00', periods=len(prices), freq=freq, tz='UTC')
    return pd.DataFrame({'close': prices, 'open': prices, 'high': prices, 'low': prices, 'volume': 1.0}, index=idx)

def test_yes_wins():
    # Price rises: after 15 bars, close(15) > strike(0)
    prices = [100_000 + i * 100 for i in range(30)]
    bars = _bars(prices)
    labels = build_binary_labels(bars, strike=100_000.0, horizon_bars=15)
    assert labels[0] == 1   # close[15] = 101500 > 100000

def test_no_wins():
    prices = [100_000 - i * 100 for i in range(30)]
    bars = _bars(prices)
    labels = build_binary_labels(bars, strike=100_000.0, horizon_bars=15)
    assert labels[0] == 0   # close[15] = 98500 < 100000

def test_output_length():
    bars = _bars([100.0] * 40)
    labels = build_binary_labels(bars, strike=99.0, horizon_bars=15)
    assert len(labels) == 25  # 40 - 15

def test_lagged_labels_keys():
    bars = _bars([100.0 + i * 0.1 for i in range(60)])
    lagged = build_lagged_labels(bars, strike=100.0, lags=[1, 2, 4, 8])
    assert set(lagged.keys()) == {1, 2, 4, 8}
    assert len(lagged[1]) == 59   # 60 - 1
    assert len(lagged[8]) == 52   # 60 - 8

def test_strike_spacing_defined():
    assert 'BTC' in STRIKE_SPACING
    assert 'ETH' in STRIKE_SPACING
```

- [ ] **Step 2: Run - expect ImportError**

```
pytest tests/backtesting/research/test_label_builder.py -v
```

Expected: `ImportError: cannot import name 'build_binary_labels'`

- [ ] **Step 3: Implement label builder**

```python
# backtesting/research/label_builder.py
from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Dict, List

# Nearest valid strike spacing per asset (dollars)
STRIKE_SPACING: Dict[str, float] = {
    'BTC': 100.0,
    'ETH': 5.0,
    'SOL': 0.5,
    'XRP': 0.005,
    'DOGE': 0.001,
}


def nearest_strike(price: float, asset: str) -> float:
    """Round price to nearest valid Kalshi strike for the given asset."""
    spacing = STRIKE_SPACING.get(asset, 1.0)
    return round(round(price / spacing) * spacing, 8)


def build_binary_labels(
    bars: pd.DataFrame,
    strike: float,
    horizon_bars: int = 15,
) -> np.ndarray:
    """
    For bar i, outcome = 1 if bars.close[i + horizon_bars] > strike else 0.
    Returns array of length len(bars) - horizon_bars.
    """
    closes = bars['close'].values
    return (closes[horizon_bars:] > strike).astype(np.int8)


def build_lagged_labels(
    bars: pd.DataFrame,
    strike: float,
    lags: List[int] = None,
) -> Dict[int, np.ndarray]:
    """
    Build binary labels at multiple lags for IC decay analysis.
    Returns {lag: array of length (len(bars) - lag)}.
    """
    if lags is None:
        lags = [1, 2, 4, 8]
    return {lag: build_binary_labels(bars, strike, horizon_bars=lag) for lag in lags}
```

- [ ] **Step 4: Run - expect all green**

```
pytest tests/backtesting/research/test_label_builder.py -v
```

Expected: `5 passed`

- [ ] **Step 5: Commit**

```
git add backtesting/research/label_builder.py tests/backtesting/research/test_label_builder.py
git commit -m "feat: binary outcome label builder for IC analysis"
```

---

## Task 3: Signal Extractor

**Files:**
- Create: `backtesting/research/signal_extractor.py`
- Create: `tests/backtesting/research/test_signal_extractor.py`

**Note:** Read `src/strategies/signals/supertrend.py` and `src/strategies/signals/black_scholes.py` before implementing to confirm their public function signatures.

- [ ] **Step 1: Write failing tests**

```python
# tests/backtesting/research/test_signal_extractor.py
import numpy as np
import pandas as pd
import pytest
from backtesting.research.signal_extractor import extract_all_signals, SIGNAL_NAMES

def _bars(n=120, trend=0.0):
    idx = pd.date_range('2025-01-01', periods=n, freq='1min', tz='UTC')
    prices = [100_000.0 + trend * i + np.random.default_rng(42).normal(0, 50) for i in range(n)]
    return pd.DataFrame({
        'close': prices, 'open': prices, 'high': [p + 20 for p in prices],
        'low': [p - 20 for p in prices], 'volume': np.ones(n),
    }, index=idx)

def test_returns_all_signal_names():
    bars = _bars()
    result = extract_all_signals(bars, strike=100_000.0, asset='BTC')
    for name in SIGNAL_NAMES:
        assert name in result, f"Missing signal: {name}"

def test_predictions_are_probabilities():
    bars = _bars()
    result = extract_all_signals(bars, strike=100_000.0, asset='BTC')
    for name, preds in result.items():
        assert isinstance(preds, np.ndarray), f"{name} not ndarray"
        assert np.all((preds >= 0.0) & (preds <= 1.0)), f"{name} has out-of-range values"

def test_output_length_matches_bars():
    bars = _bars(n=90)
    result = extract_all_signals(bars, strike=100_000.0, asset='BTC')
    for name, preds in result.items():
        assert len(preds) == 90, f"{name} length mismatch"

def test_supertrend_bimodal():
    # Supertrend should return values clustered near 0.3 and 0.7, not 0.5
    bars = _bars(n=200, trend=100.0)  # clear uptrend
    result = extract_all_signals(bars, strike=100_000.0, asset='BTC')
    st = result['supertrend_direction']
    unique = np.unique(st)
    # Should have at most 2 distinct values (0.3 / 0.7)
    assert len(unique) <= 3
```

- [ ] **Step 2: Run - expect ImportError**

```
pytest tests/backtesting/research/test_signal_extractor.py -v
```

Expected: `ImportError: cannot import name 'extract_all_signals'`

- [ ] **Step 3: Implement signal extractor**

```python
# backtesting/research/signal_extractor.py
"""
Runs each D3 sub-signal independently on historical 1-min bars,
returning p_yes predictions for IC analysis.

Signal functions are called with simplified MarketFeatures built from bars.
Signals that require live Kalshi data (velocity, ratio_divergence) fall back
to 0.5 (no-information) rather than raising.
"""
from __future__ import annotations
import sys, os
import numpy as np
import pandas as pd
from typing import Dict

# Ensure src/ is importable
_SRC = os.path.join(os.path.dirname(__file__), '..', '..', 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

SIGNAL_NAMES = [
    'supertrend_direction',
    'bs_probability',
    'momentum_delta',
    'exhaustion_fade',
    'ratio_divergence',
    'rolling_beta',
    'variance_ratio_signal',
    'volume_spike',
]


def _supertrend_predictions(bars: pd.DataFrame, atr_period: int = 14, multiplier: float = 5.0) -> np.ndarray:
    try:
        from strategies.signals.supertrend import compute_supertrend
        direction = compute_supertrend(bars['close'].values, period=atr_period, multiplier=multiplier)
        return np.where(np.array(direction) > 0, 0.70, 0.30)
    except Exception:
        return np.full(len(bars), 0.5)


def _bs_predictions(bars: pd.DataFrame, strike: float, seconds_left: float = 900.0) -> np.ndarray:
    try:
        from strategies.signals.black_scholes import bs_prob_yes
        prices = bars['close'].values
        log_ret = np.diff(np.log(np.maximum(prices, 1e-8)))
        vol_1m = np.std(log_ret) * np.sqrt(252 * 24 * 60) if len(log_ret) > 5 else 0.5
        return np.array([
            bs_prob_yes(price=p, strike=strike, vol=vol_1m, t_seconds=seconds_left)
            for p in prices
        ], dtype=float)
    except Exception:
        return np.full(len(bars), 0.5)


def _momentum_predictions(bars: pd.DataFrame, lookback: int = 4) -> np.ndarray:
    closes = bars['close'].values
    preds = np.full(len(closes), 0.5)
    for i in range(lookback, len(closes)):
        delta = closes[i] - closes[i - lookback]
        preds[i] = 0.65 if delta > 0 else 0.35
    return preds


def extract_all_signals(
    bars: pd.DataFrame,
    strike: float,
    asset: str,
) -> Dict[str, np.ndarray]:
    """
    Returns {signal_name: np.ndarray of p_yes} for all SIGNAL_NAMES.
    Each array has the same length as bars. Out-of-range values are clipped to [0, 1].
    Signals requiring live data fall back to 0.5 (neutral / no-information).
    """
    n = len(bars)
    results: Dict[str, np.ndarray] = {}

    results['supertrend_direction'] = np.clip(_supertrend_predictions(bars), 0.0, 1.0)
    results['bs_probability']       = np.clip(_bs_predictions(bars, strike), 0.0, 1.0)
    results['momentum_delta']       = np.clip(_momentum_predictions(bars), 0.0, 1.0)

    # These signals require live Kalshi/cross-venue data; fall back to 0.5 for historical IC
    for name in ['exhaustion_fade', 'ratio_divergence', 'rolling_beta', 'variance_ratio_signal', 'volume_spike']:
        results[name] = np.full(n, 0.5)

    return results
```

- [ ] **Step 4: Run - expect all green**

```
pytest tests/backtesting/research/test_signal_extractor.py -v
```

Expected: `4 passed`

- [ ] **Step 5: Commit**

```
git add backtesting/research/signal_extractor.py tests/backtesting/research/test_signal_extractor.py
git commit -m "feat: signal extractor for per-signal IC analysis"
```

---

## Task 4: Layer 1 Runner

**Files:**
- Create: `backtesting/research/layer1.py`
- Create: `tests/backtesting/research/test_layer1.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/backtesting/research/test_layer1.py
import numpy as np
import pandas as pd
import pytest
from backtesting.research.layer1 import run_layer1, layer1_verdict

def _bars(n=200, trend=50.0):
    rng = np.random.default_rng(7)
    prices = [100_000.0 + trend * i + rng.normal(0, 100) for i in range(n)]
    idx = pd.date_range('2025-01-01', periods=n, freq='1min', tz='UTC')
    return pd.DataFrame({
        'close': prices, 'open': prices,
        'high': [p + 50 for p in prices], 'low': [p - 50 for p in prices],
        'volume': np.ones(n),
    }, index=idx)

def test_run_layer1_returns_all_signals():
    from backtesting.research.signal_extractor import SIGNAL_NAMES
    bars = _bars()
    result = run_layer1(bars, strike=100_000.0, asset='BTC')
    assert 'signals' in result
    assert 'verdict' in result
    assert 'n_failing' in result
    for name in SIGNAL_NAMES:
        assert name in result['signals']

def test_run_layer1_result_structure():
    bars = _bars()
    result = run_layer1(bars, strike=100_000.0, asset='BTC')
    sig = result['signals']['supertrend_direction']
    assert 'ic' in sig
    assert 'icir' in sig
    assert 't_stat' in sig
    assert 'ic_decay' in sig
    assert 'verdict' in sig
    assert len(sig['ic_decay']) == 4

def test_layer1_verdict_aggregation():
    # All FAIL → FAIL
    assert layer1_verdict(n_failing=8, n_total=8) == 'FAIL'
    # Most pass → PASS
    assert layer1_verdict(n_failing=1, n_total=8) == 'PASS'
    # Borderline → CONDITIONAL
    assert layer1_verdict(n_failing=3, n_total=8) == 'CONDITIONAL'
```

- [ ] **Step 2: Run - expect ImportError**

```
pytest tests/backtesting/research/test_layer1.py -v
```

Expected: `ImportError: cannot import name 'run_layer1'`

- [ ] **Step 3: Implement Layer 1 runner**

```python
# backtesting/research/layer1.py
from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Any, Dict

from backtesting.research.ic_analysis import evaluate_signal
from backtesting.research.label_builder import build_binary_labels, build_lagged_labels
from backtesting.research.signal_extractor import extract_all_signals, SIGNAL_NAMES


def layer1_verdict(n_failing: int, n_total: int) -> str:
    frac = n_failing / n_total if n_total > 0 else 1.0
    if frac >= 0.5:
        return 'FAIL'
    if frac >= 0.25:
        return 'CONDITIONAL'
    return 'PASS'


def run_layer1(bars: pd.DataFrame, strike: float, asset: str) -> Dict[str, Any]:
    """
    Layer 1: Signal Validation.

    For each sub-signal, compute IC / ICIR / t-stat / IC decay against
    binary directional outcomes (close[T+15] > strike).

    Returns:
        {
            'signals': {signal_name: {ic, icir, t_stat, ic_decay, verdict}},
            'verdict': PASS | CONDITIONAL | FAIL,
            'n_failing': int,
            'n_signals': int,
        }
    """
    # Build labels and lagged labels (trim to same length as signals)
    outcomes_15 = build_binary_labels(bars, strike=strike, horizon_bars=15)
    lag_outcomes = build_lagged_labels(bars, strike=strike, lags=[1, 2, 4, 8])
    signal_preds = extract_all_signals(bars, strike=strike, asset=asset)

    signal_results = {}
    n_failing = 0

    for name in SIGNAL_NAMES:
        preds = signal_preds[name]
        # Align lengths (predictions span full bar length; labels start at bar 0)
        n = min(len(preds), len(outcomes_15))
        aligned_pred = preds[:n]
        aligned_out  = outcomes_15[:n]
        aligned_lag  = {lag: arr[:n] for lag, arr in lag_outcomes.items()}

        ic_result = evaluate_signal(aligned_pred, aligned_out, aligned_lag)
        signal_results[name] = {
            'ic':       round(ic_result.ic, 4),
            'icir':     round(ic_result.icir, 4),
            't_stat':   round(ic_result.t_stat, 4),
            'ic_decay': [round(x, 4) for x in ic_result.ic_decay],
            'n_obs':    ic_result.n_obs,
            'verdict':  ic_result.verdict,
        }
        if ic_result.verdict == 'FAIL':
            n_failing += 1

    return {
        'signals':   signal_results,
        'verdict':   layer1_verdict(n_failing, len(SIGNAL_NAMES)),
        'n_failing': n_failing,
        'n_signals': len(SIGNAL_NAMES),
    }
```

- [ ] **Step 4: Run - expect all green**

```
pytest tests/backtesting/research/test_layer1.py -v
```

Expected: `3 passed`

- [ ] **Step 5: Commit**

```
git add backtesting/research/layer1.py tests/backtesting/research/test_layer1.py
git commit -m "feat: Layer 1 signal validation runner (IC/ICIR per sub-signal)"
```

---

## Task 5: Layer 2 - Null Simulator

**Files:**
- Create: `backtesting/research/layer2.py`
- Create: `tests/backtesting/research/test_layer2.py`

**Design:** "Flip side" null - take real entry timestamps and outcomes, randomly flip the trade side 1000 times, recompute Sharpe. This tests whether signal direction adds value beyond the entry filter selection.

- [ ] **Step 1: Write failing tests**

```python
# tests/backtesting/research/test_layer2.py
import numpy as np
import pandas as pd
import pytest
from backtesting.research.layer2 import (
    run_null_simulation, flip_side_pnl, NullResult, layer2_verdict,
    audit_lookahead,
)

def _trade_log(n=200, win_rate=0.58):
    rng = np.random.default_rng(42)
    wins = (rng.uniform(0, 1, n) < win_rate)
    pnls = np.where(wins, 0.08, -0.07)  # edge: +8c wins, -7c losses
    return pd.DataFrame({'pnl': pnls, 'side': np.where(wins, 'yes', 'no')})

def test_flip_side_pnl_inverts():
    pnls = np.array([0.08, -0.07, 0.08])
    flipped = flip_side_pnl(pnls)
    assert flipped[0] == pytest.approx(-0.08)
    assert flipped[1] == pytest.approx(0.07)

def test_null_result_structure():
    log = _trade_log(n=150)
    result = run_null_simulation(log['pnl'].values, n_iter=100, seed=0)
    assert isinstance(result, NullResult)
    assert len(result.null_sharpes) == 100
    assert result.real_sharpe != 0.0
    assert 0.0 <= result.p_value <= 1.0

def test_null_worse_than_real_on_good_signal():
    # Good signal: 60% win rate → real Sharpe > median null
    log = _trade_log(n=300, win_rate=0.60)
    result = run_null_simulation(log['pnl'].values, n_iter=500, seed=1)
    assert result.real_sharpe > np.median(result.null_sharpes)

def test_null_indistinguishable_on_noise():
    # Noise signal: 50% win rate → real Sharpe ≈ null distribution
    rng = np.random.default_rng(7)
    pnls = np.where(rng.integers(0, 2, 200), 0.08, -0.08)
    result = run_null_simulation(pnls, n_iter=500, seed=2)
    assert result.p_value > 0.05  # fail to reject H0

def test_layer2_verdict():
    assert layer2_verdict(0.01)  == 'PASS'
    assert layer2_verdict(0.07)  == 'CONDITIONAL'
    assert layer2_verdict(0.15)  == 'FAIL'

def test_audit_lookahead_returns_list():
    findings = audit_lookahead('BTC')
    assert isinstance(findings, list)
```

- [ ] **Step 2: Run - expect ImportError**

```
pytest tests/backtesting/research/test_layer2.py -v
```

Expected: `ImportError: cannot import name 'run_null_simulation'`

- [ ] **Step 3: Implement Layer 2**

```python
# backtesting/research/layer2.py
from __future__ import annotations
import os
import glob
from dataclasses import dataclass
from typing import List

import numpy as np
import pandas as pd

from backtesting.metrics.trading import sharpe_ratio


@dataclass
class NullResult:
    real_sharpe: float
    null_sharpes: np.ndarray
    p_value: float        # one-tailed: fraction where null >= real
    null_p95: float
    verdict: str


def flip_side_pnl(pnls: np.ndarray) -> np.ndarray:
    """Invert all P&Ls (simulating the opposite side on each trade)."""
    return -pnls


def run_null_simulation(
    trade_pnls: np.ndarray,
    n_iter: int = 1000,
    seed: int = 0,
) -> NullResult:
    """
    'Flip-side' null: randomly flip each trade's P&L sign with 50% probability
    n_iter times. P&L sign flip simulates trading the opposite side.
    p-value = fraction of null sharpes >= real sharpe (one-tailed).
    """
    rng = np.random.default_rng(seed)
    real_sharpe = sharpe_ratio(trade_pnls)

    null_sharpes = np.empty(n_iter)
    for i in range(n_iter):
        signs = rng.choice([-1.0, 1.0], size=len(trade_pnls))
        null_pnls = trade_pnls * signs
        null_sharpes[i] = sharpe_ratio(null_pnls)

    p_value = float(np.mean(null_sharpes >= real_sharpe))

    return NullResult(
        real_sharpe=real_sharpe,
        null_sharpes=null_sharpes,
        p_value=p_value,
        null_p95=float(np.percentile(null_sharpes, 95)),
        verdict=layer2_verdict(p_value),
    )


def layer2_verdict(p_value: float) -> str:
    if p_value < 0.05:
        return 'PASS'
    if p_value < 0.10:
        return 'CONDITIONAL'
    return 'FAIL'


def audit_lookahead(asset: str) -> List[str]:
    """
    Static checks for known lookahead risks. Returns list of finding strings.
    Empty list = no issues found.
    """
    findings = []
    root = os.path.join(os.path.dirname(__file__), '..', '..', 'backtesting', 'output', 'models')

    cal_path = os.path.join(root, f'{asset.lower()}_calibrated_model.pkl')
    if os.path.exists(cal_path):
        findings.append(
            f"[WARN] {asset} calibrator model exists at {cal_path}. "
            "Verify it was fit per-fold only (not on full dataset) before trusting Layer 2."
        )

    # Check for any 'future' keyword in feature_builder
    fb_path = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'strategies', 'feature_builder.py')
    if os.path.exists(fb_path):
        with open(fb_path) as f:
            src = f.read()
        if 'shift(-' in src or 'future' in src.lower():
            findings.append('[WARN] feature_builder.py may reference future bars (found shift(- or "future").')

    return findings


def run_layer2(trade_log: pd.DataFrame, asset: str, n_iter: int = 1000) -> dict:
    """
    Layer 2: Strategy simulation + null hypothesis.

    trade_log must have column 'pnl' (dollars per dollar staked).
    """
    pnls = trade_log['pnl'].values
    audit = audit_lookahead(asset)
    null  = run_null_simulation(pnls, n_iter=n_iter)

    return {
        'real_sharpe':      null.real_sharpe,
        'null_p95':         null.null_p95,
        'p_value':          null.p_value,
        'verdict':          null.verdict,
        'lookahead_issues': audit,
        'n_trades':         len(pnls),
    }
```

- [ ] **Step 4: Run - expect all green**

```
pytest tests/backtesting/research/test_layer2.py -v
```

Expected: `6 passed`

- [ ] **Step 5: Commit**

```
git add backtesting/research/layer2.py tests/backtesting/research/test_layer2.py
git commit -m "feat: Layer 2 null simulator (flip-side permutation + lookahead audit)"
```

---

## Task 6: Layer 3 - DSR, PBO, MinBTL

**Files:**
- Create: `backtesting/research/layer3.py`
- Create: `tests/backtesting/research/test_layer3.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/backtesting/research/test_layer3.py
import numpy as np
import pytest
from backtesting.research.layer3 import (
    deflated_sharpe_ratio, min_backtest_length,
    probability_of_backtest_overfitting, layer3_verdict,
)

def test_dsr_high_for_good_strategy():
    # SR=1.5, 5000 obs, symmetric returns, 10 trials → DSR near 1
    pnls = np.random.default_rng(0).normal(0.003, 0.06, 5000)
    dsr = deflated_sharpe_ratio(sr_obs=1.5, pnls=pnls, num_trials=10)
    assert dsr > 0.80

def test_dsr_low_when_many_trials():
    # Same SR but 500 trials → heavily penalised
    pnls = np.random.default_rng(0).normal(0.003, 0.06, 5000)
    dsr = deflated_sharpe_ratio(sr_obs=1.5, pnls=pnls, num_trials=500)
    assert dsr < 0.90

def test_dsr_between_zero_and_one():
    pnls = np.random.default_rng(1).normal(0, 0.05, 200)
    dsr = deflated_sharpe_ratio(sr_obs=0.5, pnls=pnls, num_trials=50)
    assert 0.0 <= dsr <= 1.0

def test_min_backtest_length_formula():
    # SR=1.0, alpha=0.05 → ~2.7 years
    years = min_backtest_length(sr=1.0, alpha=0.05)
    assert 2.0 < years < 3.5

def test_min_backtest_length_higher_sr_needs_less():
    y1 = min_backtest_length(sr=1.0)
    y2 = min_backtest_length(sr=2.0)
    assert y2 < y1

def test_pbo_all_is_best_is_oos_best():
    # IS-best always also OOS-best → PBO = 0
    folds = [{'is_rank': 0, 'oos_rank': 0} for _ in range(10)]
    assert probability_of_backtest_overfitting(folds) == pytest.approx(0.0)

def test_pbo_is_best_never_oos_best():
    # IS-best always OOS-worst → PBO = 1
    folds = [{'is_rank': 0, 'oos_rank': 4} for _ in range(10)]
    assert probability_of_backtest_overfitting(folds) == pytest.approx(1.0)

def test_layer3_verdict():
    assert layer3_verdict(dsr=0.97, pbo=0.18, minbtl=1.2, data_years=3.0) == 'PASS'
    assert layer3_verdict(dsr=0.60, pbo=0.18, minbtl=1.2, data_years=3.0) == 'FAIL'
    assert layer3_verdict(dsr=0.97, pbo=0.18, minbtl=4.0, data_years=3.0) == 'FAIL'
```

- [ ] **Step 2: Run - expect ImportError**

```
pytest tests/backtesting/research/test_layer3.py -v
```

Expected: `ImportError`

- [ ] **Step 3: Implement Layer 3**

```python
# backtesting/research/layer3.py
from __future__ import annotations
import math
from typing import List, Dict, Any

import numpy as np
import pandas as pd
from scipy import stats
from scipy.stats import norm


def _expected_max_sharpe(num_trials: int) -> float:
    """
    Expected maximum Sharpe across num_trials IID zero-mean unit-variance strategies.
    Formula: AFML Ch. 14 (Bailey & López de Prado 2014).
    """
    if num_trials < 2:
        return 0.0
    gamma = 0.5772156649  # Euler-Mascheroni constant
    e = math.e
    z1 = (1 - gamma) * norm.ppf(1 - 1.0 / num_trials)
    z2 = gamma * norm.ppf(1 - 1.0 / (num_trials * e))
    return z1 + z2


def deflated_sharpe_ratio(
    sr_obs: float,
    pnls: np.ndarray,
    num_trials: int,
) -> float:
    """
    Deflated Sharpe Ratio - adjusts observed SR for non-normality of returns
    and for the number of independent configurations tested.

    sr_obs:     annualized Sharpe ratio of the strategy
    pnls:       per-observation P&L array (used for skew + kurtosis)
    num_trials: number of independent configurations tested (e.g. EV sweeps)

    Returns DSR ∈ [0, 1]: probability that the true SR > 0 after adjustment.
    """
    n = len(pnls)
    if n < 10:
        return 0.0
    skew = float(stats.skew(pnls))
    kurt = float(stats.kurtosis(pnls, fisher=False))  # full kurtosis (normal = 3)

    # Standard error of the Sharpe ratio estimator
    sr_std = math.sqrt(
        max(1e-10, (1.0 - skew * sr_obs + (kurt - 1.0) / 4.0 * sr_obs ** 2) / (n - 1))
    )
    sr_star = _expected_max_sharpe(num_trials)

    dsr = norm.cdf((sr_obs - sr_star) / sr_std)
    return float(dsr)


def min_backtest_length(sr: float, alpha: float = 0.05) -> float:
    """
    Minimum years of data needed to reject H0 (SR=0) at significance level alpha.
    Approximation: MinBTL ≈ (Z_alpha / SR)^2 years.
    """
    if sr <= 0:
        return float('inf')
    z_alpha = norm.ppf(1 - alpha)
    return (z_alpha / sr) ** 2


def probability_of_backtest_overfitting(
    fold_results: List[Dict[str, int]],
) -> float:
    """
    PBO from CPCV fold results.

    fold_results: list of dicts with keys 'is_rank' and 'oos_rank'.
        is_rank:  0-indexed rank of IS-best strategy among all configs (0 = best IS)
        oos_rank: 0-indexed rank of that same strategy's OOS performance

    PBO = fraction of folds where the IS-best strategy ranked below the median OOS.
    Threshold: n_configs // 2.
    """
    if not fold_results:
        return float('nan')
    overfit_count = sum(
        1 for f in fold_results if f['oos_rank'] > len(fold_results) // 2
    )
    return overfit_count / len(fold_results)


def layer3_verdict(dsr: float, pbo: float, minbtl: float, data_years: float) -> str:
    if dsr < 0.95 or pbo > 0.25 or minbtl > data_years:
        if dsr < 0.80 or pbo > 0.45 or minbtl > data_years * 1.5:
            return 'FAIL'
        return 'CONDITIONAL'
    return 'PASS'


def run_layer3(
    trade_log: pd.DataFrame,
    wfa_sharpes: List[float],
    data_years: float,
    num_trials: int = 50,
) -> Dict[str, Any]:
    """
    Layer 3: WFA significance.

    trade_log:   DataFrame with 'pnl' column.
    wfa_sharpes: per-fold Sharpe ratios from walk-forward analysis.
    data_years:  total years of history used.
    num_trials:  number of independent configs tested (e.g. EV sweep count).
    """
    from backtesting.metrics.trading import sharpe_ratio
    pnls = trade_log['pnl'].values
    sr_obs = sharpe_ratio(pnls)

    dsr    = deflated_sharpe_ratio(sr_obs, pnls, num_trials)
    minbtl = min_backtest_length(sr_obs)

    # PBO: rank WFA folds by IS vs OOS consistency (simplified: compare fold SR to median)
    median_wfa = float(np.median(wfa_sharpes)) if wfa_sharpes else 0.0
    fold_results = [
        {'is_rank': 0, 'oos_rank': 0 if s >= median_wfa else 1}
        for s in wfa_sharpes
    ]
    pbo = probability_of_backtest_overfitting(fold_results)

    return {
        'sr_obs':      round(sr_obs, 4),
        'dsr':         round(dsr, 4),
        'pbo':         round(pbo, 4),
        'minbtl':      round(minbtl, 2),
        'data_years':  round(data_years, 2),
        'num_trials':  num_trials,
        'verdict':     layer3_verdict(dsr, pbo, minbtl, data_years),
    }
```

- [ ] **Step 4: Run - expect all green**

```
pytest tests/backtesting/research/test_layer3.py -v
```

Expected: `8 passed`

- [ ] **Step 5: Commit**

```
git add backtesting/research/layer3.py tests/backtesting/research/test_layer3.py
git commit -m "feat: Layer 3 WFA significance (DSR, PBO, MinBTL)"
```

---

## Task 7: Layer 4 - Trade-Level Permutation Test

**Files:**
- Create: `backtesting/research/layer4.py`
- Create: `tests/backtesting/research/test_layer4.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/backtesting/research/test_layer4.py
import numpy as np
import pandas as pd
import pytest
from backtesting.research.layer4 import (
    block_shuffle, full_shuffle_test, block_shuffle_test,
    min_trades_needed, PermResult, run_layer4,
)

def test_block_shuffle_preserves_values():
    arr = np.arange(100, dtype=float)
    shuffled = block_shuffle(arr, block_size=10, seed=0)
    assert sorted(shuffled) == sorted(arr)

def test_block_shuffle_different_order():
    arr = np.arange(50, dtype=float)
    shuffled = block_shuffle(arr, block_size=5, seed=1)
    assert not np.array_equal(arr, shuffled)

def test_full_shuffle_p_low_for_good_signal():
    rng = np.random.default_rng(42)
    # 60% win rate, +8/-7 edge
    wins = rng.uniform(0, 1, 300) < 0.60
    pnls = np.where(wins, 0.08, -0.07)
    result = full_shuffle_test(pnls, n_iter=1000, seed=0)
    assert isinstance(result, PermResult)
    assert result.p_value < 0.10

def test_block_shuffle_p_high_for_noise():
    rng = np.random.default_rng(99)
    pnls = np.where(rng.integers(0, 2, 200), 0.08, -0.08)
    result = block_shuffle_test(pnls, n_iter=500, block_size=10, seed=0)
    assert result.p_value > 0.05  # can't reject H0

def test_min_trades_needed():
    assert min_trades_needed(win_rate=0.65) <= 60
    assert min_trades_needed(win_rate=0.55) >= 200

def test_run_layer4_structure():
    rng = np.random.default_rng(0)
    wins = rng.uniform(0, 1, 200) < 0.58
    log = pd.DataFrame({'pnl': np.where(wins, 0.08, -0.07)})
    result = run_layer4(log, n_iter=200)
    assert 'p_value_full' in result
    assert 'p_value_block' in result
    assert 'verdict' in result
    assert 'sufficient_data' in result
```

- [ ] **Step 2: Run - expect ImportError**

```
pytest tests/backtesting/research/test_layer4.py -v
```

Expected: `ImportError`

- [ ] **Step 3: Implement Layer 4**

```python
# backtesting/research/layer4.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict

import numpy as np
import pandas as pd

from backtesting.metrics.trading import sharpe_ratio


@dataclass
class PermResult:
    real_sharpe: float
    null_median: float
    null_p95: float
    p_value: float       # one-tailed: fraction where null >= real
    verdict: str


def block_shuffle(arr: np.ndarray, block_size: int = 10, seed: int = 0) -> np.ndarray:
    """Shuffle arr in contiguous blocks of block_size, preserving all values."""
    rng = np.random.default_rng(seed)
    n = len(arr)
    blocks = [arr[i:i + block_size] for i in range(0, n, block_size)]
    rng.shuffle(blocks)
    shuffled = np.concatenate(blocks)
    return shuffled[:n]


def _permutation_test(
    pnls: np.ndarray,
    n_iter: int,
    shuffle_fn,
) -> PermResult:
    real_sr = sharpe_ratio(pnls)
    null_srs = np.array([sharpe_ratio(shuffle_fn(pnls)) for _ in range(n_iter)])
    p_val = float(np.mean(null_srs >= real_sr))
    verdict = 'PASS' if p_val < 0.05 else ('CONDITIONAL' if p_val < 0.10 else 'FAIL')
    return PermResult(
        real_sharpe=real_sr,
        null_median=float(np.median(null_srs)),
        null_p95=float(np.percentile(null_srs, 95)),
        p_value=p_val,
        verdict=verdict,
    )


def full_shuffle_test(pnls: np.ndarray, n_iter: int = 10_000, seed: int = 0) -> PermResult:
    rng = np.random.default_rng(seed)
    return _permutation_test(pnls, n_iter, lambda a: rng.permutation(a))


def block_shuffle_test(
    pnls: np.ndarray,
    n_iter: int = 10_000,
    block_size: int = 10,
    seed: int = 0,
) -> PermResult:
    seeds = np.random.default_rng(seed).integers(0, 2**31, n_iter)
    return _permutation_test(
        pnls, n_iter,
        lambda a: block_shuffle(a, block_size=block_size, seed=int(seeds[0]))
    )


def min_trades_needed(win_rate: float, alpha: float = 0.05) -> int:
    """
    Approximate minimum trades for a permutation test to reach p < alpha.
    Based on normal approximation of win rate vs. 50%:
    n >= (Z_alpha / (win_rate - 0.5) )^2 * 0.25
    """
    from scipy.stats import norm
    if win_rate <= 0.5:
        return 10_000
    z = norm.ppf(1 - alpha)
    edge = win_rate - 0.5
    return max(30, int(np.ceil((z / edge) ** 2 * 0.25)))


def run_layer4(trade_log: pd.DataFrame, n_iter: int = 10_000) -> Dict[str, Any]:
    """
    Layer 4: Trade-level permutation test.
    trade_log must have column 'pnl'.
    """
    pnls = trade_log['pnl'].values
    win_rate = float(np.mean(pnls > 0))
    min_trades = min_trades_needed(win_rate)
    sufficient = len(pnls) >= min_trades

    full  = full_shuffle_test(pnls, n_iter=n_iter)
    block = block_shuffle_test(pnls, n_iter=n_iter)

    # Primary verdict uses block shuffle (more conservative)
    return {
        'n_trades':        len(pnls),
        'win_rate':        round(win_rate, 4),
        'real_sharpe':     round(full.real_sharpe, 4),
        'p_value_full':    round(full.p_value, 4),
        'p_value_block':   round(block.p_value, 4),
        'null_p95_block':  round(block.null_p95, 4),
        'verdict':         block.verdict if sufficient else 'INSUFFICIENT_DATA',
        'sufficient_data': sufficient,
        'min_trades':      min_trades,
    }
```

- [ ] **Step 4: Run - expect all green**

```
pytest tests/backtesting/research/test_layer4.py -v
```

Expected: `6 passed`

- [ ] **Step 5: Commit**

```
git add backtesting/research/layer4.py tests/backtesting/research/test_layer4.py
git commit -m "feat: Layer 4 trade-level permutation test (full + block shuffle)"
```

---

## Task 8: Layer 5 - Regime Robustness

**Files:**
- Create: `backtesting/research/layer5.py`
- Create: `tests/backtesting/research/test_layer5.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/backtesting/research/test_layer5.py
import numpy as np
import pandas as pd
import pytest
from backtesting.research.layer5 import (
    variance_ratio, classify_vol_tercile, classify_trend_regime,
    session_label, compute_regime_breakdown, run_layer5,
)

def _price_series(n=200, drift=0.0, vol=0.01, seed=0):
    rng = np.random.default_rng(seed)
    log_rets = rng.normal(drift, vol, n)
    return np.exp(np.cumsum(log_rets)) * 100_000

def test_variance_ratio_trending():
    # Strong uptrend → VR > 1.1
    prices = _price_series(n=200, drift=0.003, vol=0.001)
    vr = variance_ratio(prices, q=4)
    assert vr > 1.0  # trending process has VR > 1

def test_variance_ratio_mean_reverting():
    # Alternating up/down → VR < 0.9
    n = 200
    prices = np.cumprod(1 + np.tile([-0.005, 0.005], n // 2)) * 100_000
    vr = variance_ratio(prices[:n], q=4)
    assert vr < 1.0

def test_variance_ratio_random_walk():
    # Pure random walk → VR ≈ 1.0 (within noise)
    rng = np.random.default_rng(42)
    log_rets = rng.normal(0, 0.01, 500)
    prices = np.exp(np.cumsum(log_rets)) * 100_000
    vr = variance_ratio(prices, q=4)
    assert 0.6 < vr < 1.4  # wider band; random walk VR converges slowly

def test_classify_vol_tercile():
    idx = pd.date_range('2025-01-01', periods=300, freq='1min', tz='UTC')
    prices = np.ones(300) * 100_000
    bars = pd.DataFrame({'close': prices}, index=idx)
    result = classify_vol_tercile(bars, window_bars=100)
    assert set(result.unique()).issubset({'low', 'mid', 'high'})

def test_session_label():
    assert session_label(pd.Timestamp('2025-01-01 10:00', tz='UTC')) == 'US'
    assert session_label(pd.Timestamp('2025-01-01 05:00', tz='UTC')) == 'London'
    assert session_label(pd.Timestamp('2025-01-01 01:00', tz='UTC')) == 'Asia'

def test_compute_regime_breakdown_structure():
    idx = pd.date_range('2025-01-01', periods=100, freq='15min', tz='UTC')
    log = pd.DataFrame({
        'pnl':     np.random.default_rng(0).normal(0.01, 0.06, 100),
        'timestamp': idx,
    })
    regime = pd.Series(['low_vol_trending'] * 100, index=idx)
    result = compute_regime_breakdown(log, regime)
    assert 'regime_sharpes' in result
    assert 'session_sharpes' in result
```

- [ ] **Step 2: Run - expect ImportError**

```
pytest tests/backtesting/research/test_layer5.py -v
```

Expected: `ImportError`

- [ ] **Step 3: Implement Layer 5**

```python
# backtesting/research/layer5.py
from __future__ import annotations
from typing import Any, Dict

import numpy as np
import pandas as pd

from backtesting.metrics.trading import sharpe_ratio

# UTC hour boundaries for session classification
_ASIA_START, _ASIA_END     = 0,  8   # 00:00-08:00 UTC  ≈ 20:00-04:00 ET
_LONDON_START, _LONDON_END = 8,  13  # 08:00-13:00 UTC  ≈ 04:00-09:00 ET
# US: 13:00-00:00 UTC ≈ 09:00-20:00 ET


def variance_ratio(prices: np.ndarray, q: int = 4) -> float:
    """
    Lo-MacKinlay (1988) variance ratio at aggregation lag q.
    VR > 1.1 → trending, VR < 0.9 → mean-reverting, ≈1 → random walk.
    """
    n = len(prices)
    if n < q + 5:
        return 1.0
    log_prices = np.log(np.maximum(prices, 1e-12))
    returns = np.diff(log_prices)
    mu = returns.mean()

    # 1-period variance
    var1 = float(np.sum((returns - mu) ** 2) / (n - 1))

    # q-period variance (overlapping)
    q_returns = log_prices[q:] - log_prices[:-q]
    m = q * (n - q) * (1 - q / n)
    var_q = float(np.sum((q_returns - q * mu) ** 2) / m)

    return (var_q / var1) if var1 > 0 else 1.0


def classify_vol_tercile(bars: pd.DataFrame, window_bars: int = 43_200) -> pd.Series:
    """
    Label each bar as 'low', 'mid', or 'high' vol based on rolling realized vol
    (window_bars = 30 days × 24h × 60min = 43200 for 1-min bars).
    """
    log_ret = np.log(bars['close'] / bars['close'].shift(1))
    rolling_vol = log_ret.rolling(window_bars).std()
    q33 = rolling_vol.quantile(0.333)
    q67 = rolling_vol.quantile(0.667)
    labels = pd.Series('mid', index=bars.index)
    labels[rolling_vol <= q33] = 'low'
    labels[rolling_vol > q67]  = 'high'
    return labels


def classify_trend_regime(prices: np.ndarray, window: int = 60, q: int = 4) -> str:
    """Return 'trending', 'random', or 'mean_reverting' for a price window."""
    if len(prices) < window:
        return 'random'
    vr = variance_ratio(prices[-window:], q=q)
    if vr > 1.1:
        return 'trending'
    if vr < 0.9:
        return 'mean_reverting'
    return 'random'


def session_label(ts: pd.Timestamp) -> str:
    """Classify UTC timestamp into Asia / London / US trading session."""
    ts_utc = ts.tz_convert('UTC') if ts.tzinfo else ts
    hour = ts_utc.hour
    if _ASIA_START <= hour < _ASIA_END:
        return 'Asia'
    if _LONDON_START <= hour < _LONDON_END:
        return 'London'
    return 'US'


def compute_regime_breakdown(
    trade_log: pd.DataFrame,
    regime_series: pd.Series,
) -> Dict[str, Any]:
    """
    Compute Sharpe per regime cell and per session.

    trade_log: must have 'pnl' and 'timestamp' (or DatetimeIndex).
    regime_series: Series with same DatetimeIndex as bars, values = regime label strings.
    """
    if 'timestamp' in trade_log.columns:
        trade_log = trade_log.set_index('timestamp')

    regime_sharpes: Dict[str, float] = {}
    session_sharpes: Dict[str, float] = {}

    # Per-regime Sharpe
    for regime in regime_series.unique():
        regime_ts = regime_series[regime_series == regime].index
        mask = trade_log.index.isin(regime_ts)
        subset = trade_log.loc[mask, 'pnl'].values
        if len(subset) >= 5:
            regime_sharpes[regime] = round(sharpe_ratio(subset), 3)

    # Per-session Sharpe
    for sess in ['Asia', 'London', 'US']:
        mask = trade_log.index.map(session_label) == sess
        subset = trade_log.loc[mask, 'pnl'].values
        if len(subset) >= 5:
            session_sharpes[sess] = round(sharpe_ratio(subset), 3)

    return {'regime_sharpes': regime_sharpes, 'session_sharpes': session_sharpes}


def layer5_verdict(regime_sharpes: Dict[str, float]) -> str:
    if not regime_sharpes:
        return 'INSUFFICIENT_DATA'
    n_pos = sum(1 for v in regime_sharpes.values() if v > 0)
    n_cat = max(round(0.75 * len(regime_sharpes)), 1)  # need 75%+ positive
    if n_pos >= n_cat and min(regime_sharpes.values()) > -1.0:
        return 'PASS'
    if n_pos >= round(0.50 * len(regime_sharpes)):
        return 'CONDITIONAL'
    return 'FAIL'


def run_layer5(
    trade_log: pd.DataFrame,
    bars: pd.DataFrame,
    vol_window_bars: int = 43_200,
    vr_window: int = 60,
) -> Dict[str, Any]:
    """
    Layer 5: Regime robustness.
    trade_log: 'pnl' + DatetimeIndex or 'timestamp' column.
    bars: 1-min OHLCV with DatetimeIndex.
    """
    vol_regime = classify_vol_tercile(bars, window_bars=vol_window_bars)

    # Build regime label for each bar (e.g. "low_trending")
    closes = bars['close'].values
    trend_labels = [
        classify_trend_regime(closes[max(0, i - vr_window):i + 1], window=vr_window)
        for i in range(len(closes))
    ]
    trend_series = pd.Series(trend_labels, index=bars.index)
    regime_combined = vol_regime + '_' + trend_series

    breakdown = compute_regime_breakdown(trade_log, regime_combined)
    verdict   = layer5_verdict(breakdown['regime_sharpes'])

    return {**breakdown, 'verdict': verdict}
```

- [ ] **Step 4: Run - expect all green**

```
pytest tests/backtesting/research/test_layer5.py -v
```

Expected: `8 passed`

- [ ] **Step 5: Commit**

```
git add backtesting/research/layer5.py tests/backtesting/research/test_layer5.py
git commit -m "feat: Layer 5 regime robustness (variance ratio + vol tercile + session)"
```

---

## Task 9: Report Writer

**Files:**
- Create: `backtesting/research/report_writer.py`
- Create: `tests/backtesting/research/test_report_writer.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/backtesting/research/test_report_writer.py
import json
from pathlib import Path
import tempfile
import pytest
from backtesting.research.report_writer import write_research_report

FAKE_RESULTS = {
    'layer1': {'verdict': 'PASS', 'n_failing': 2, 'n_signals': 8,
               'signals': {'supertrend_direction': {'ic': 0.05, 'icir': 0.4, 't_stat': 2.5,
                                                     'ic_decay': [0.05, 0.04, 0.03, 0.02],
                                                     'n_obs': 1000, 'verdict': 'PASS'}}},
    'layer2': {'verdict': 'PASS', 'p_value': 0.02, 'real_sharpe': 1.43,
               'null_p95': 0.61, 'n_trades': 312, 'lookahead_issues': []},
    'layer3': {'verdict': 'PASS', 'dsr': 0.97, 'pbo': 0.18, 'minbtl': 1.2,
               'data_years': 3.1, 'sr_obs': 1.43, 'num_trials': 50},
    'layer4': {'verdict': 'PASS', 'p_value_block': 0.004, 'real_sharpe': 1.43,
               'n_trades': 312, 'sufficient_data': True, 'win_rate': 0.59},
    'layer5': {'verdict': 'CONDITIONAL', 'regime_sharpes': {'low_trending': 1.82},
               'session_sharpes': {'US': 1.68}},
}

def test_writes_markdown_and_json(tmp_path):
    write_research_report('BTC', FAKE_RESULTS, output_dir=tmp_path)
    assert (tmp_path / 'research_report.md').exists()
    assert (tmp_path / 'research.json').exists()

def test_json_is_valid(tmp_path):
    write_research_report('BTC', FAKE_RESULTS, output_dir=tmp_path)
    data = json.loads((tmp_path / 'research.json').read_text())
    assert data['asset'] == 'BTC'
    assert 'overall_verdict' in data
    assert 'layers' in data

def test_markdown_contains_verdict(tmp_path):
    write_research_report('BTC', FAKE_RESULTS, output_dir=tmp_path)
    md = (tmp_path / 'research_report.md').read_text()
    assert 'BTC' in md
    assert 'PASS' in md or 'CONDITIONAL' in md or 'FAIL' in md

def test_overall_verdict_conditional_when_one_layer_fails(tmp_path):
    results = {**FAKE_RESULTS, 'layer5': {**FAKE_RESULTS['layer5'], 'verdict': 'FAIL'}}
    write_research_report('BTC', results, output_dir=tmp_path)
    data = json.loads((tmp_path / 'research.json').read_text())
    assert data['overall_verdict'] in ('CONDITIONAL', 'FAIL')
```

- [ ] **Step 2: Run - expect ImportError**

```
pytest tests/backtesting/research/test_report_writer.py -v
```

Expected: `ImportError`

- [ ] **Step 3: Implement report writer**

```python
# backtesting/research/report_writer.py
from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict


def _overall_verdict(results: Dict[str, Any]) -> str:
    verdicts = [v.get('verdict', 'FAIL') for v in results.values()]
    if any(v == 'FAIL' for v in verdicts):
        return 'FAIL' if verdicts.count('FAIL') >= 2 else 'CONDITIONAL'
    if any(v == 'CONDITIONAL' for v in verdicts):
        return 'CONDITIONAL'
    return 'PASS'


def _verdict_emoji(v: str) -> str:
    return {'PASS': '✓', 'CONDITIONAL': '~', 'FAIL': '✗'}.get(v, '?')


def write_research_report(
    asset: str,
    results: Dict[str, Dict[str, Any]],
    output_dir: Path,
) -> None:
    """
    Write research_report.md and research.json to output_dir.
    results: {layer_key: layer_result_dict}
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    overall = _overall_verdict(results)

    # ── JSON ────────────────────────────────────────────────────────────────
    json_out = {'asset': asset, 'overall_verdict': overall, 'layers': results}
    (output_dir / 'research.json').write_text(
        json.dumps(json_out, indent=2, default=str)
    )

    # ── Markdown ─────────────────────────────────────────────────────────────
    lines = [
        f'# {asset} - Backtest Validation Report',
        '',
        f'**Overall verdict: {overall}**',
        '',
        '## Layer Summary',
        '',
        '| Layer | Verdict |',
        '|-------|---------|',
    ]
    layer_names = {
        'layer1': 'Layer 1 - Signal IC',
        'layer2': 'Layer 2 - Null Hypothesis',
        'layer3': 'Layer 3 - WFA Significance',
        'layer4': 'Layer 4 - Permutation Test',
        'layer5': 'Layer 5 - Regime Robustness',
    }
    for key, name in layer_names.items():
        if key in results:
            v = results[key].get('verdict', 'N/A')
            lines.append(f'| {name} | {_verdict_emoji(v)} {v} |')

    lines += ['', '---', '']

    # Layer 1 details
    if 'layer1' in results:
        r = results['layer1']
        lines += [
            '## Layer 1 - Signal IC',
            f"Failing signals: {r.get('n_failing', '?')}/{r.get('n_signals', '?')}",
            '',
            '| Signal | IC | ICIR | t-stat | Verdict |',
            '|--------|----|------|--------|---------|',
        ]
        for name, sig in r.get('signals', {}).items():
            lines.append(
                f"| {name} | {sig['ic']:.3f} | {sig['icir']:.3f} "
                f"| {sig['t_stat']:.2f} | {sig['verdict']} |"
            )
        lines.append('')

    # Layer 2 details
    if 'layer2' in results:
        r = results['layer2']
        lines += [
            '## Layer 2 - Null Hypothesis',
            f"Real Sharpe: {r.get('real_sharpe', '?'):.3f}  "
            f"Null 95th%: {r.get('null_p95', '?'):.3f}  "
            f"p-value: {r.get('p_value', '?'):.4f}",
        ]
        if r.get('lookahead_issues'):
            lines += ['', '**Lookahead findings:**']
            for issue in r['lookahead_issues']:
                lines.append(f'- {issue}')
        lines.append('')

    # Layer 3 details
    if 'layer3' in results:
        r = results['layer3']
        lines += [
            '## Layer 3 - WFA Significance',
            f"DSR: {r.get('dsr', '?'):.3f}  PBO: {r.get('pbo', '?'):.3f}  "
            f"MinBTL: {r.get('minbtl', '?'):.1f}yr (have {r.get('data_years', '?'):.1f}yr)",
            '',
        ]

    # Layer 4 details
    if 'layer4' in results:
        r = results['layer4']
        lines += [
            '## Layer 4 - Permutation Test',
            f"Trades: {r.get('n_trades', '?')}  Win rate: {r.get('win_rate', 0)*100:.1f}%  "
            f"p-value (block): {r.get('p_value_block', '?'):.4f}",
            '' if r.get('sufficient_data', True) else
            f"⚠ Insufficient data - need {r.get('min_trades', '?')} trades minimum.",
            '',
        ]

    # Layer 5 details
    if 'layer5' in results:
        r = results['layer5']
        lines += ['## Layer 5 - Regime Robustness', '']
        rs = r.get('regime_sharpes', {})
        if rs:
            lines += ['| Regime | Sharpe |', '|--------|--------|']
            for regime, sr in sorted(rs.items()):
                lines.append(f'| {regime} | {sr:.3f} |')
        ss = r.get('session_sharpes', {})
        if ss:
            lines += ['', '| Session | Sharpe |', '|---------|--------|']
            for sess, sr in ss.items():
                lines.append(f'| {sess} | {sr:.3f} |')
        lines.append('')

    (output_dir / 'research_report.md').write_text('\n'.join(lines))
```

- [ ] **Step 4: Run - expect all green**

```
pytest tests/backtesting/research/test_report_writer.py -v
```

Expected: `4 passed`

- [ ] **Step 5: Commit**

```
git add backtesting/research/report_writer.py tests/backtesting/research/test_report_writer.py
git commit -m "feat: research report writer (markdown + JSON)"
```

---

## Task 10: CLI Entry Point + Integration Smoke Test

**Files:**
- Create: `backtesting/research_cli.py`

- [ ] **Step 1: Implement CLI**

```python
# backtesting/research_cli.py
"""
Entry point: python backtesting/research_cli.py --asset BTC [--layers 1,2,3,4,5]

Loads 1-min bars for the asset, runs requested layers, writes report to
backtesting/output/research/{asset}/.
"""
from __future__ import annotations
import argparse
import os
import sys
from pathlib import Path

# Ensure project imports work when called directly
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / 'src'))

import pandas as pd
from backtesting.data.loaders import load_bars
from backtesting.research.label_builder import STRIKE_SPACING, nearest_strike
from backtesting.research.layer1 import run_layer1
from backtesting.research.layer2 import run_layer2
from backtesting.research.layer3 import run_layer3
from backtesting.research.layer4 import run_layer4
from backtesting.research.layer5 import run_layer5
from backtesting.research.report_writer import write_research_report


def _count_trials(asset: str) -> int:
    """Estimate number of independent configs tested from EV sweep files."""
    sweep_dir = _ROOT / 'backtesting' / 'output'
    pattern = f'ev_wfa_{asset.lower()}_ev*.csv'
    files = list(sweep_dir.glob(pattern))
    return max(len(files), 5)  # minimum 5


def main():
    parser = argparse.ArgumentParser(description='5-layer backtest validation')
    parser.add_argument('--asset', required=True, choices=['BTC', 'ETH', 'SOL', 'XRP', 'DOGE'])
    parser.add_argument('--layers', default='1,2,3,4,5', help='Comma-separated layer numbers')
    parser.add_argument('--iters', type=int, default=1000, help='Permutation iterations (layer 4)')
    args = parser.parse_args()

    layers = [int(x.strip()) for x in args.layers.split(',')]
    asset  = args.asset.upper()
    output_dir = _ROOT / 'backtesting' / 'output' / 'research' / asset
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f'[research] Loading bars for {asset}...')
    bars = load_bars(asset)
    if bars.empty:
        sys.exit(f'[research] ERROR: no bar data found for {asset}')

    # Use median price as ATM strike approximation
    strike = nearest_strike(float(bars['close'].median()), asset)
    print(f'[research] Strike (ATM approx): {strike}')

    results = {}

    if 1 in layers:
        print('[research] Running Layer 1 - Signal IC...')
        results['layer1'] = run_layer1(bars, strike=strike, asset=asset)
        print(f"  → {results['layer1']['verdict']} ({results['layer1']['n_failing']} failing signals)")

    # Build synthetic trade log for layers 2-4 if not already present from layer 2
    trade_log: pd.DataFrame | None = None

    if 2 in layers:
        print('[research] Running Layer 2 - Null Hypothesis...')
        # For layer 2 null simulation, use a dummy trade log from existing WFA trades
        # if available; otherwise skip null (require real backtest run first)
        wfa_log_path = _ROOT / 'backtesting' / 'output' / f'{asset.lower()}_trades.csv'
        if wfa_log_path.exists():
            trade_log = pd.read_csv(wfa_log_path)
            results['layer2'] = run_layer2(trade_log, asset=asset, n_iter=args.iters)
            print(f"  → {results['layer2']['verdict']} (p={results['layer2']['p_value']:.4f})")
        else:
            print(f'  [SKIP] No trade log at {wfa_log_path} - run WFA first')
            results['layer2'] = {'verdict': 'SKIPPED', 'reason': 'no_trade_log'}

    if trade_log is None:
        wfa_log_path = _ROOT / 'backtesting' / 'output' / f'{asset.lower()}_trades.csv'
        if wfa_log_path.exists():
            trade_log = pd.read_csv(wfa_log_path)

    if 3 in layers and trade_log is not None:
        print('[research] Running Layer 3 - WFA Significance...')
        import glob
        wfa_files = glob.glob(str(_ROOT / 'backtesting' / 'output' / f'ev_wfa_{asset.lower()}*.csv'))
        wfa_sharpes = []
        for f in wfa_files:
            df = pd.read_csv(f)
            if 'sharpe' in df.columns:
                wfa_sharpes.extend(df['sharpe'].dropna().tolist())
        data_years = max((bars.index[-1] - bars.index[0]).days / 365.25, 0.1)
        results['layer3'] = run_layer3(trade_log, wfa_sharpes=wfa_sharpes,
                                       data_years=data_years, num_trials=_count_trials(asset))
        print(f"  → {results['layer3']['verdict']} (DSR={results['layer3']['dsr']:.3f})")

    if 4 in layers and trade_log is not None:
        print('[research] Running Layer 4 - Permutation Test...')
        results['layer4'] = run_layer4(trade_log, n_iter=args.iters)
        print(f"  → {results['layer4']['verdict']} (p_block={results['layer4']['p_value_block']:.4f})")

    if 5 in layers and trade_log is not None:
        print('[research] Running Layer 5 - Regime Robustness...')
        results['layer5'] = run_layer5(trade_log, bars)
        print(f"  → {results['layer5']['verdict']}")

    write_research_report(asset, results, output_dir=output_dir)
    print(f'[research] Report written to {output_dir}/research_report.md')
    overall = max((r.get('verdict', 'FAIL') for r in results.values()),
                  key=lambda v: {'PASS': 0, 'CONDITIONAL': 1, 'FAIL': 2, 'SKIPPED': 1}.get(v, 1))
    print(f'[research] Overall verdict: {overall}')


if __name__ == '__main__':
    main()
```

- [ ] **Step 2: Smoke test - Layer 1 only (no trade log needed)**

```
python backtesting/research_cli.py --asset BTC --layers 1
```

Expected:
```
[research] Loading bars for BTC...
[research] Strike (ATM approx): <price>
[research] Running Layer 1 - Signal IC...
  → PASS|CONDITIONAL|FAIL (<n> failing signals)
[research] Report written to backtesting/output/research/BTC/research_report.md
```

If `load_bars` errors due to missing data, confirm `data/historical/BTC_1m_extended.parquet` exists before running.

- [ ] **Step 3: Run full test suite to verify no regressions**

```
pytest tests/backtesting/research/ -v
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```
git add backtesting/research_cli.py
git commit -m "feat: research CLI entry point (python backtesting/research_cli.py --asset BTC)"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Task |
|-----------------|------|
| IC / ICIR / t-stat per sub-signal | Tasks 1, 3, 4 |
| IC decay curves at lags 1,2,4,8 | Tasks 1, 3, 4 |
| Binary outcome label builder | Task 2 |
| Signal extractor (supertrend, BS, momentum; others fallback) | Task 3 |
| Layer 1 runner + verdict | Task 4 |
| Lookahead audit | Task 5 |
| Null hypothesis (flip-side, 1000 iter) | Task 5 |
| DSR (AFML Ch. 14 formula) | Task 6 |
| PBO from fold ranks | Task 6 |
| MinBTL formula (Z_α/SR)² | Task 6 |
| Permutation test, full + block shuffle | Task 7 |
| Min trades per win rate | Task 7 |
| Variance ratio (Lo-MacKinlay) | Task 8 |
| Vol tercile (30-day window) | Task 8 |
| Session breakdown Asia/London/US | Task 8 |
| 9-cell regime grid | Task 8 |
| research_report.md + research.json | Task 9 |
| CLI `--asset BTC [--layers ...]` | Task 10 |

**Known limitations explicitly documented in code:**
- Signals requiring live Kalshi data (exhaustion_fade, ratio_divergence, etc.) fall back to p=0.5 in signal_extractor.py - IC for these signals is uninformative and will always FAIL. This is expected and documented.
- Layer 2 null simulation requires a pre-existing WFA trade log. Run `python backtesting/cli.py all --asset BTC` first.
- Layer 3 PBO uses a simplified rank computation (above/below median per fold) rather than the full Bailey-López de Prado multi-strategy CPCV rank. Full PBO requires running multiple EV threshold configs; a follow-up task can extend this.
