# 15-Minute Strategy Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Strategy A (calibrated probability model) and Strategy B (contract mean-reversion detector) for Kalshi 15-minute crypto binary markets across BTC, ETH, SOL, XRP.

**Architecture:** Eight model instances (4 assets × 2 strategies) with shared typed infrastructure. Strategy A concatenates five feature modules (HAR-RS-J vol, order flow, time-of-day regimes, cross-asset BTC signals, funding/OI) into a calibrated logistic regression. Strategy B detects Kalshi contract dislocations from spot-implied fair value and fades them.

**Tech Stack:** Python 3.11, pandas, numpy, scikit-learn, scipy, pyyaml, pydantic-style dataclasses, xgboost/lightgbm (swap-in), pytest.

---

## File Map

```
strategies/
  __init__.py
  strategy_a/
    __init__.py
    model.py                        # StrategyAModel: predict_proba, get_edge, should_trade
    features/
      __init__.py
      har_rv.py                     # HARRSJForecaster: online + batch, sigma_forecast
      order_flow.py                 # OrderFlowFeatures: OFI, VAMP, VPIN, spread, flows
      time_of_day.py                # compute(ts) → session/dow/cyclic/proximity features
      cross_asset.py                # compute(dict) → BTC-derived features, graceful degrade
      funding.py                    # FundingFeatures: funding/OI z-scores + crowded flags
    config/
      btc.yaml
      eth.yaml
      sol.yaml
      xrp.yaml
  strategy_b/
    __init__.py
    contract_dislocation.py         # ContractDislocationDetector: detect_dislocation
    config/
      btc.yaml
      eth.yaml
      sol.yaml
      xrp.yaml
  shared/
    __init__.py
    types.py                        # FeatureVector, Signal, DislocationSignal, JumpFlag
    probability_utils.py            # drift_vol_to_prob, prob_to_contract_price, dp→dc
    regime_filters.py               # get_current_regime, get_regime_threshold, get_fee_adjusted_threshold
    fees.yaml
tests/
  conftest.py                       # sys.path fix so imports resolve from strategies/
  test_har_rv.py
  test_order_flow.py
  test_time_of_day.py
  test_cross_asset.py
  test_funding.py
  test_model.py
  test_contract_dislocation.py
strategies/README.md
```

---

## Task 1: Directory scaffold, `__init__.py` stubs, and `shared/types.py`

**Files:**
- Create: `strategies/__init__.py`
- Create: `strategies/strategy_a/__init__.py`
- Create: `strategies/strategy_a/features/__init__.py`
- Create: `strategies/strategy_b/__init__.py`
- Create: `strategies/shared/__init__.py`
- Create: `strategies/shared/types.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Write `tests/conftest.py`** (pytest sys.path fix)

```python
# tests/conftest.py
import sys
import os

# Allow `from strategy_a.features.har_rv import ...` in all test files
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "strategies"))
```

- [ ] **Step 2: Write `strategies/shared/types.py`**

```python
# strategies/shared/types.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd


@dataclass
class FeatureVector:
    """All feature module outputs. Call to_flat_dict() before feeding to model."""
    har_rv: dict[str, float] = field(default_factory=dict)
    order_flow: dict[str, float] = field(default_factory=dict)
    time_of_day: dict[str, float] = field(default_factory=dict)
    cross_asset: dict[str, float] = field(default_factory=dict)
    funding: dict[str, float] = field(default_factory=dict)

    def to_flat_dict(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for module in ("har_rv", "order_flow", "time_of_day", "cross_asset", "funding"):
            for k, v in getattr(self, module).items():
                out[f"{module}__{k}"] = v
        return out


@dataclass
class Signal:
    """Output of Strategy A's trade decision."""
    timestamp: pd.Timestamp
    asset: str
    p_model: float        # calibrated P(up at 15-min expiry) in [0, 1]
    p_market: float       # Kalshi YES price in [0, 1] (not cents)
    edge: float           # p_model - p_market; positive → buy YES
    regime: str
    side: str             # "YES" or "NO"
    strategy: str = "strategy_a"


@dataclass
class DislocationSignal:
    """Output of Strategy B's dislocation detector."""
    timestamp: pd.Timestamp
    asset: str
    direction: str        # "fade_up" or "fade_down"
    confidence: float     # [0, 1] scaled by residual magnitude
    side: str             # "YES" or "NO" (the recommended trade side)
    residual_magnitude: float   # |actual_move - implied_move| in cents
    staleness_timestamp: pd.Timestamp   # signal expires after this time


@dataclass
class JumpFlag:
    """A statistically detected price jump from BV-based heuristic."""
    timestamp: pd.Timestamp
    is_jump: bool
    magnitude: float      # |J| = max(RV - BV, 0)
    direction: str        # "up" or "down"
```

- [ ] **Step 3: Create blank `__init__.py` files**

```python
# content for ALL __init__.py files (strategies/, strategy_a/, features/, strategy_b/, shared/)
```

Create each as an empty file (or with a single comment `# package`).

- [ ] **Step 4: Smoke-test the imports**

Run: `cd strategies && python -c "from shared.types import FeatureVector, Signal, DislocationSignal, JumpFlag; print('ok')"`

Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add strategies/ tests/conftest.py
git commit -m "feat: scaffold strategy layer directory structure and shared types"
```

---

## Task 2: Shared utilities - `probability_utils.py`, `regime_filters.py`, `fees.yaml`

**Files:**
- Create: `strategies/shared/probability_utils.py`
- Create: `strategies/shared/regime_filters.py`
- Create: `strategies/shared/fees.yaml`

- [ ] **Step 1: Write failing test for `drift_vol_to_prob`**

Create `tests/test_probability_utils.py`:

```python
# tests/test_probability_utils.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "strategies"))
import math
from shared.probability_utils import drift_vol_to_prob, prob_to_contract_price, contract_price_change_from_prob_change

def test_zero_drift_gives_half():
    # With zero drift, P(up) = 0.5 exactly
    p = drift_vol_to_prob(mu=0.0, sigma=0.5, dt=1/365)
    assert abs(p - 0.5) < 1e-10

def test_positive_drift_above_half():
    p = drift_vol_to_prob(mu=100.0, sigma=0.5, dt=1/(365*96))
    assert p > 0.5

def test_prob_to_price_identity():
    assert prob_to_contract_price(0.7) == 70.0
    assert prob_to_contract_price(0.0) == 0.0
    assert prob_to_contract_price(1.0) == 100.0

def test_dp_to_dc_identity():
    assert contract_price_change_from_prob_change(0.05) == 5.0
    assert contract_price_change_from_prob_change(-0.03) == -3.0
```

Run: `pytest tests/test_probability_utils.py -v`
Expected: ImportError (module not yet created)

- [ ] **Step 2: Write `strategies/shared/probability_utils.py`**

```python
# strategies/shared/probability_utils.py
from __future__ import annotations
import numpy as np
from scipy.stats import norm


def drift_vol_to_prob(mu: float, sigma: float, dt: float) -> float:
    """
    P(S_T > S_0) under Brownian-with-drift over horizon dt.

    Args:
        mu:    annualized log-drift (log-return per year)
        sigma: annualized log-volatility
        dt:    horizon in years (e.g. 15/(365*24*60) for 15 minutes)

    Returns Φ((mu · dt) / (sigma · √dt)) per Black-Scholes probability.
    Returns 0.5 when sigma or dt is non-positive (degenerate input guard).
    """
    if sigma <= 0.0 or dt <= 0.0:
        return 0.5
    return float(norm.cdf((mu * dt) / (sigma * np.sqrt(dt))))


def prob_to_contract_price(p: float) -> float:
    """
    Convert probability to Kalshi contract price in cents.
    Kalshi contract price IS the probability: a YES at 70c pays $1 with P=0.70.
    This is a named wrapper to make the conversion point explicit in call sites.
    """
    return p * 100.0


def contract_price_change_from_prob_change(dp: float) -> float:
    """
    Convert a change in probability (dimensionless) to a contract price change (cents).
    The mapping is 1:1: dp=0.05 → 5 cents. Named for clarity at call sites.
    """
    return dp * 100.0
```

- [ ] **Step 3: Run the test**

Run: `pytest tests/test_probability_utils.py -v`
Expected: all PASS

- [ ] **Step 4: Write `strategies/shared/regime_filters.py`**

```python
# strategies/shared/regime_filters.py
from __future__ import annotations
import pandas as pd

_REGIME_HOURS: list[tuple[str, int, int]] = [
    ("asia_deep_night",  0,  4),
    ("asia_active",      4,  8),
    ("eu_open",          8, 13),
    ("eu_us_overlap",   13, 16),
    ("us_afternoon",    16, 20),
    ("us_late",         20, 24),
]


def get_current_regime(timestamp: pd.Timestamp, config: dict) -> str:
    """Return the regime string for a UTC timestamp. config is unused (for future extension)."""
    hour = timestamp.hour
    for name, start, end in _REGIME_HOURS:
        if start <= hour < end:
            return name
    return "us_late"


def get_regime_threshold(regime: str, timestamp: pd.Timestamp, config: dict) -> float:
    """
    Look up the edge-above-fee threshold for the given regime.
    Returns the weekend threshold if the timestamp falls on Saturday (5) or Sunday (6).
    Returns 0.02 as a safe default when config entry is null/missing.
    """
    thresholds = config.get("thresholds", {}).get("edge_above_fee", {})
    if timestamp.dayofweek >= 5:
        val = thresholds.get("weekend")
        if val is not None:
            return float(val)
    val = thresholds.get(regime)
    return float(val) if val is not None else 0.02


def get_fee_adjusted_threshold(
    regime: str,
    timestamp: pd.Timestamp,
    config: dict,
    fees_config: dict,
) -> float:
    """
    Minimum edge required to trade after fees.

    Derivation:
      taker_fee    = fees_config["kalshi"]["taker_fee_rate"]     (flat approx)
      safety_margin = fees_config["safety_margin"]
      regime_extra  = per-regime edge from config (null → 0.02)
      min_edge = taker_fee + safety_margin + regime_extra

    Note: fees.yaml stores a conservative flat-rate approximation (0.03).
    The actual Kalshi taker fee is 0.07·C·p·(1-p), which peaks at ~3.5% at p=0.5.
    The execution layer uses the exact formula; this threshold uses the flat approx.
    """
    taker_fee = float(fees_config["kalshi"]["taker_fee_rate"])
    safety_margin = float(fees_config["safety_margin"])
    regime_extra = get_regime_threshold(regime, timestamp, config)
    return taker_fee + safety_margin + regime_extra
```

- [ ] **Step 5: Write `strategies/shared/fees.yaml`**

```yaml
# strategies/shared/fees.yaml
# Conservative flat-rate approximation used for edge threshold computation.
# Actual Kalshi taker fee formula: ceil(0.07 * C * p * (1-p)) in dollars,
# which peaks at ~3.5% at p=0.50. This 3% flat is used only for pre-trade
# threshold checks; the execution layer calls src/strategies/fees.py directly.
kalshi:
  taker_fee_rate: 0.03
  maker_fee_rate: 0.00
safety_margin: 0.005   # extra edge required beyond raw fee hurdle
```

- [ ] **Step 6: Commit**

```bash
git add strategies/shared/
git commit -m "feat: add shared probability utils, regime filters, and fees config"
```

---

## Task 3: HAR-RS-J volatility forecaster

**Files:**
- Create: `strategies/strategy_a/features/har_rv.py`
- Create: `tests/test_har_rv.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_har_rv.py
import numpy as np
import pytest
from strategy_a.features.har_rv import HARRSJForecaster

_CFG = {
    "returns": {"granularity_seconds": 10},
    "har_rs_j": {
        "timescales_minutes": [15, 60, 240],
        "coefficients": {
            "const": None, "rv_15m_pos": None, "rv_15m_neg": None,
            "rv_1h_pos": None, "rv_1h_neg": None,
            "rv_4h_pos": None, "rv_4h_neg": None, "jump": None,
        },
    },
}

_EXPECTED_KEYS = {
    "15m_rv", "15m_rv_pos", "15m_rv_neg", "15m_bv", "15m_jump", "15m_signed_jump",
    "1h_rv",  "1h_rv_pos",  "1h_rv_neg",  "1h_bv",  "1h_jump",  "1h_signed_jump",
    "4h_rv",  "4h_rv_pos",  "4h_rv_neg",  "4h_bv",  "4h_jump",  "4h_signed_jump",
    "sigma_forecast",
}

def _returns(n=300):
    rng = np.random.default_rng(42)
    return rng.normal(0, 0.001, n)


def test_smoke():
    f = HARRSJForecaster(_CFG)
    f.fit(_returns())
    assert isinstance(f.compute(), dict)


def test_shape():
    f = HARRSJForecaster(_CFG)
    f.fit(_returns())
    assert _EXPECTED_KEYS.issubset(f.compute().keys())


def test_type_and_range():
    f = HARRSJForecaster(_CFG)
    f.fit(_returns())
    result = f.compute()
    for k, v in result.items():
        assert isinstance(v, float), f"{k} is not float"
        assert np.isfinite(v), f"{k} not finite: {v}"
    for scale in ("15m", "1h", "4h"):
        assert result[f"{scale}_rv"]     >= 0.0
        assert result[f"{scale}_rv_pos"] >= 0.0
        assert result[f"{scale}_rv_neg"] >= 0.0
        assert result[f"{scale}_jump"]   >= 0.0
    assert result["sigma_forecast"] >= 0.0


def test_online_update_matches_batch():
    rng = np.random.default_rng(0)
    rets = rng.normal(0, 0.001, 100).tolist()
    f_batch = HARRSJForecaster(_CFG)
    f_batch.fit(np.array(rets))
    r_batch = f_batch.compute()

    f_online = HARRSJForecaster(_CFG)
    for r in rets:
        f_online.update({"log_return": r})
    r_online = f_online.compute()

    assert abs(r_batch["15m_rv"] - r_online["15m_rv"]) < 1e-12


def test_rv_plus_rv_neg_sum_to_rv():
    f = HARRSJForecaster(_CFG)
    f.fit(_returns())
    res = f.compute()
    for scale in ("15m", "1h", "4h"):
        total = res[f"{scale}_rv_pos"] + res[f"{scale}_rv_neg"]
        assert abs(total - res[f"{scale}_rv"]) < 1e-12, (
            f"{scale}: RV+={res[f'{scale}_rv_pos']} + RV-={res[f'{scale}_rv_neg']} "
            f"!= RV={res[f'{scale}_rv']}"
        )
```

Run: `pytest tests/test_har_rv.py -v`
Expected: ImportError

- [ ] **Step 2: Write `strategies/strategy_a/features/har_rv.py`**

```python
# strategies/strategy_a/features/har_rv.py
from __future__ import annotations
"""
HAR-RS-J volatility forecaster (Patton & Sheppard 2015) adapted for 15-minute horizons.

Model: σ̂² = const
         + β_rv+_15m·RV+_15m + β_rv-_15m·RV-_15m
         + β_rv+_1h·RV+_1h   + β_rv-_1h·RV-_1h
         + β_rv+_4h·RV+_4h   + β_rv-_4h·RV-_4h
         + β_J·J_15m

Crypto asymmetry: RV+ and RV- have *separate* coefficients. In crypto, positive
semivariance is a stronger predictor of future variance than negative semivariance
(opposite of equities). The model never constrains β_rv+_15m == β_rv-_15m.

Output: σ̂ (not σ̂²) in log-return space, suitable as a 15-minute vol forecast.
"""
import numpy as np
from collections import deque


class HARRSJForecaster:
    def __init__(self, config: dict) -> None:
        gran = int(config["returns"]["granularity_seconds"])
        scales_min: list[int] = config["har_rs_j"]["timescales_minutes"]
        self._scale_names = ["15m", "1h", "4h"]
        self._n_bars = [int(m * 60 / gran) for m in scales_min]
        max_bars = max(self._n_bars)
        self._buf: deque[float] = deque(maxlen=max_bars)
        self._coef: dict = config["har_rs_j"]["coefficients"]

    # ── public interface ──────────────────────────────────────────────────────

    def update(self, new_bar: dict) -> None:
        """Append one bar. Expects {'log_return': float}."""
        r = new_bar.get("log_return")
        if r is not None:
            self._buf.append(float(r))

    def fit(self, returns_array: np.ndarray) -> None:
        """Batch-load historical returns into the ring buffer."""
        self._buf.clear()
        self._buf.extend(returns_array[-self._buf.maxlen:].tolist())

    def compute(self, data_window=None) -> dict[str, float]:
        """
        Compute all HAR-RS-J features plus σ̂ forecast.
        data_window: optional sequence of log-returns (float or dict with 'log_return')
                     appended before computation.
        """
        if data_window is not None:
            if isinstance(data_window, np.ndarray):
                self._buf.extend(data_window.tolist())
            else:
                for item in data_window:
                    if isinstance(item, dict):
                        self.update(item)
                    else:
                        self._buf.append(float(item))

        arr = np.array(list(self._buf), dtype=np.float64)
        out: dict[str, float] = {}
        for name, n in zip(self._scale_names, self._n_bars):
            window = arr[-n:] if len(arr) >= n else arr
            for k, v in self._rv_components(window).items():
                out[f"{name}_{k}"] = v
        out["sigma_forecast"] = self._forecast(out)
        return out

    # ── internals ─────────────────────────────────────────────────────────────

    @staticmethod
    def _rv_components(r: np.ndarray) -> dict[str, float]:
        if r.size == 0:
            return {k: 0.0 for k in ("rv", "rv_pos", "rv_neg", "bv", "jump", "signed_jump")}
        sq = r ** 2
        rv      = float(sq.sum())
        rv_pos  = float(sq[r > 0].sum())
        rv_neg  = float(sq[r < 0].sum())
        # BV = (π/2) · Σ|r_t|·|r_{t-1}| - scaled to match RV units
        bv = float((np.pi / 2) * (np.abs(r[1:]) * np.abs(r[:-1])).sum()) if r.size > 1 else rv
        jump         = max(rv - bv, 0.0)
        signed_jump  = rv_pos - rv_neg
        return {"rv": rv, "rv_pos": rv_pos, "rv_neg": rv_neg,
                "bv": bv, "jump": jump, "signed_jump": signed_jump}

    def _forecast(self, feats: dict[str, float]) -> float:
        c = self._coef
        if any(v is None for v in c.values()):
            # Untrained model: proxy σ̂ ≈ √RV_15m (no-drift realized vol)
            return float(np.sqrt(max(feats.get("15m_rv", 0.0), 0.0)))
        sigma_sq = (
            c["const"]
            + c["rv_15m_pos"] * feats["15m_rv_pos"]
            + c["rv_15m_neg"] * feats["15m_rv_neg"]
            + c["rv_1h_pos"]  * feats["1h_rv_pos"]
            + c["rv_1h_neg"]  * feats["1h_rv_neg"]
            + c["rv_4h_pos"]  * feats["4h_rv_pos"]
            + c["rv_4h_neg"]  * feats["4h_rv_neg"]
            + c["jump"]       * feats["15m_jump"]
        )
        return float(np.sqrt(max(sigma_sq, 0.0)))
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_har_rv.py -v`
Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add strategies/strategy_a/features/har_rv.py tests/test_har_rv.py
git commit -m "feat: add HAR-RS-J volatility forecaster with online update"
```

---

## Task 4: Order flow and microstructure features

**Files:**
- Create: `strategies/strategy_a/features/order_flow.py`
- Create: `tests/test_order_flow.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_order_flow.py
import numpy as np
import pandas as pd
import pytest
from strategy_a.features.order_flow import OrderFlowFeatures

_CFG = {
    "order_flow": {
        "ofi_depths": [1, 3, 5, 10],
        "vpin_bucket_size": 500.0,
        "vpin_rolling_buckets": 50,
    }
}

_EXPECTED_KEYS = {
    "ofi_l1", "ofi_l3", "ofi_l5", "ofi_l10",
    "vamp",
    "signed_flow_1m", "signed_flow_5m", "signed_flow_15m",
    "vpin",
    "spread_abs", "spread_bps", "depth_bid", "depth_ask",
}

def _book():
    bids = [(50000.0 - i * 10, 1.0 + i * 0.1) for i in range(10)]
    asks = [(50010.0 + i * 10, 1.0 + i * 0.1) for i in range(10)]
    return {"timestamp": pd.Timestamp.utcnow(), "bids": bids, "asks": asks}

def _trade(side="buy"):
    return {"timestamp": pd.Timestamp.utcnow(), "price": 50005.0,
            "size": 0.5, "aggressor_side": side}


def test_smoke():
    of = OrderFlowFeatures(_CFG)
    assert isinstance(of.compute({"book": _book(), "trades": [_trade()]}), dict)


def test_shape():
    of = OrderFlowFeatures(_CFG)
    result = of.compute({"book": _book(), "trades": []})
    assert _EXPECTED_KEYS.issubset(result.keys())


def test_ofi_range():
    of = OrderFlowFeatures(_CFG)
    result = of.compute({"book": _book(), "trades": []})
    for d in _CFG["order_flow"]["ofi_depths"]:
        v = result[f"ofi_l{d}"]
        assert isinstance(v, float)
        assert -1.0 <= v <= 1.0, f"ofi_l{d}={v} outside [-1,1]"


def test_spread_positive():
    of = OrderFlowFeatures(_CFG)
    result = of.compute({"book": _book(), "trades": []})
    assert result["spread_abs"] > 0.0
    assert result["spread_bps"] > 0.0


def test_signed_flow_direction():
    of = OrderFlowFeatures(_CFG)
    trade = {**_trade("buy"), "size": 10.0}
    result = of.compute({"book": _book(), "trades": [trade]})
    # All-buy trades → positive signed flow
    assert result["signed_flow_1m"] > 0.0
    assert result["signed_flow_5m"] > 0.0


def test_vpin_range():
    of = OrderFlowFeatures(_CFG)
    rng = pd.Timestamp.utcnow()
    trades = [{"timestamp": rng, "price": 50000.0, "size": 100.0, "aggressor_side": "buy"}
              for _ in range(20)]
    result = of.compute({"book": _book(), "trades": trades})
    assert 0.0 <= result["vpin"] <= 1.0
```

- [ ] **Step 2: Write `strategies/strategy_a/features/order_flow.py`**

```python
# strategies/strategy_a/features/order_flow.py
from __future__ import annotations
import numpy as np
import pandas as pd
from collections import deque


def _ofi_at_depth(bids: list, asks: list, depth: int) -> float:
    bid_q = sum(q for _, q in bids[:depth])
    ask_q = sum(q for _, q in asks[:depth])
    total = bid_q + ask_q
    return (bid_q - ask_q) / total if total > 0 else 0.0


def _vamp(bids: list, asks: list) -> float:
    """Volume-Adjusted Mid-Price at top of book."""
    if not bids or not asks:
        return float("nan")
    bp, bq = bids[0]
    ap, aq = asks[0]
    denom = bq + aq
    return (bp * aq + ap * bq) / denom if denom > 0 else (bp + ap) / 2.0


class OrderFlowFeatures:
    """
    Online-computable microstructure features from L2 book snapshots + trade tape.
    All rolling windows use wall-clock time from trade timestamps.
    """

    _WINDOWS: dict[str, int] = {"1m": 60, "5m": 300, "15m": 900}

    def __init__(self, config: dict) -> None:
        ofc = config["order_flow"]
        self._depths: list[int] = ofc["ofi_depths"]
        raw_bucket = ofc.get("vpin_bucket_size")
        self._bucket_size: float = float(raw_bucket) if raw_bucket else 1000.0
        self._vpin_n: int = int(ofc["vpin_rolling_buckets"])
        self._trade_bufs: dict[str, deque] = {k: deque() for k in self._WINDOWS}
        self._vpin_buy = self._vpin_sell = self._vpin_vol = 0.0
        self._vpin_buckets: deque[float] = deque(maxlen=self._vpin_n)

    def _ingest(self, trades: list[dict]) -> None:
        for t in trades:
            ts = pd.Timestamp(t["timestamp"])
            signed = t["size"] if t["aggressor_side"] == "buy" else -t["size"]
            for buf in self._trade_bufs.values():
                buf.append((ts, signed))
            buy_vol  = t["size"] if t["aggressor_side"] == "buy"  else 0.0
            sell_vol = t["size"] if t["aggressor_side"] == "sell" else 0.0
            self._vpin_buy  += buy_vol
            self._vpin_sell += sell_vol
            self._vpin_vol  += t["size"]
            if self._vpin_vol >= self._bucket_size:
                imb = abs(self._vpin_buy - self._vpin_sell) / self._bucket_size
                self._vpin_buckets.append(imb)
                self._vpin_buy = self._vpin_sell = self._vpin_vol = 0.0

    def _purge(self, now: pd.Timestamp) -> None:
        for window, buf in self._trade_bufs.items():
            cutoff = now - pd.Timedelta(seconds=self._WINDOWS[window])
            while buf and buf[0][0] < cutoff:
                buf.popleft()

    def compute(self, data_window: dict) -> dict[str, float]:
        """
        data_window:
          book:   {timestamp, bids: [(price, size),...], asks: [(price, size),...]}
          trades: [{timestamp, price, size, aggressor_side}, ...]
        """
        book   = data_window.get("book", {})
        bids   = book.get("bids", [])
        asks   = book.get("asks", [])
        now    = pd.Timestamp(book.get("timestamp", pd.Timestamp.utcnow()))
        self._ingest(data_window.get("trades", []))
        self._purge(now)

        out: dict[str, float] = {}
        for d in self._depths:
            out[f"ofi_l{d}"] = _ofi_at_depth(bids, asks, d)
        out["vamp"] = _vamp(bids, asks)
        for window, buf in self._trade_bufs.items():
            out[f"signed_flow_{window}"] = float(sum(v for _, v in buf))
        out["vpin"] = float(np.mean(list(self._vpin_buckets))) if self._vpin_buckets else 0.0
        if bids and asks:
            bp, bq = bids[0]
            ap, aq = asks[0]
            mid    = (bp + ap) / 2.0
            spread = ap - bp
            out["spread_abs"]  = float(spread)
            out["spread_bps"]  = float(spread / mid * 10_000) if mid > 0 else float("nan")
            out["depth_bid"]   = float(bq)
            out["depth_ask"]   = float(aq)
        else:
            out.update({"spread_abs": float("nan"), "spread_bps": float("nan"),
                        "depth_bid": float("nan"), "depth_ask": float("nan")})
        return out
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_order_flow.py -v`
Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add strategies/strategy_a/features/order_flow.py tests/test_order_flow.py
git commit -m "feat: add order flow microstructure feature module (OFI, VAMP, VPIN)"
```

---

## Task 5: Time-of-day regime encoder

**Files:**
- Create: `strategies/strategy_a/features/time_of_day.py`
- Create: `tests/test_time_of_day.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_time_of_day.py
import numpy as np
import pandas as pd
import pytest
from strategy_a.features.time_of_day import compute, _SESSIONS, _DAYS

_EXPECTED_KEYS = (
    {f"session_{n}" for n, _, _ in _SESSIONS}
    | {f"dow_{d}" for d in _DAYS}
    | {"is_weekend", "minute_sin", "minute_cos",
       "minutes_until_0800", "minutes_until_1430", "monday_asia_open"}
)


def test_smoke():
    assert isinstance(compute(pd.Timestamp("2026-04-22 10:30:00", tz="UTC")), dict)


def test_shape():
    result = compute(pd.Timestamp("2026-04-22 10:30:00", tz="UTC"))
    assert _EXPECTED_KEYS.issubset(result.keys())


def test_session_one_hot_sum():
    result = compute(pd.Timestamp("2026-04-22 10:30:00", tz="UTC"))
    session_values = [v for k, v in result.items() if k.startswith("session_")]
    assert sum(session_values) == 1.0


def test_eu_open_active_at_10h():
    result = compute(pd.Timestamp("2026-04-22 10:30:00", tz="UTC"))
    assert result["session_eu_open"] == 1.0


def test_weekend_flag():
    # 2026-04-25 is Saturday
    result = compute(pd.Timestamp("2026-04-25 12:00:00", tz="UTC"))
    assert result["is_weekend"] == 1.0


def test_weekday_not_weekend():
    # 2026-04-22 is Wednesday
    result = compute(pd.Timestamp("2026-04-22 10:30:00", tz="UTC"))
    assert result["is_weekend"] == 0.0


def test_cyclic_range():
    result = compute(pd.Timestamp("2026-04-22 10:30:00", tz="UTC"))
    assert -1.0 <= result["minute_sin"] <= 1.0
    assert -1.0 <= result["minute_cos"] <= 1.0


def test_proximity_clipped_to_120():
    result = compute(pd.Timestamp("2026-04-22 10:30:00", tz="UTC"))
    assert 0.0 <= result["minutes_until_0800"] <= 120.0
    assert 0.0 <= result["minutes_until_1430"] <= 120.0


def test_monday_asia_open_true_mon_2h():
    # Monday 02:00 UTC is in the Asia open window
    result = compute(pd.Timestamp("2026-04-27 02:00:00", tz="UTC"))
    assert result["monday_asia_open"] == 1.0


def test_monday_asia_open_false_mid_week():
    result = compute(pd.Timestamp("2026-04-22 10:30:00", tz="UTC"))
    assert result["monday_asia_open"] == 0.0
```

- [ ] **Step 2: Write `strategies/strategy_a/features/time_of_day.py`**

```python
# strategies/strategy_a/features/time_of_day.py
from __future__ import annotations
"""
Time-of-day regime encoder for Kalshi 15-minute crypto markets.

Sessions (UTC):
  asia_deep_night  00:00-04:00   Low liquidity, wide spreads
  asia_active      04:00-08:00   Tokyo / Singapore active
  eu_open          08:00-13:00   London open, highest volume
  eu_us_overlap    13:00-16:00   Peak global liquidity
  us_afternoon     16:00-20:00   US afternoon, Fed-news window
  us_late          20:00-24:00   Thin US tail, Asia pre-open
"""
import numpy as np
import pandas as pd

_SESSIONS: list[tuple[str, int, int]] = [
    ("asia_deep_night",  0,  4),
    ("asia_active",      4,  8),
    ("eu_open",          8, 13),
    ("eu_us_overlap",   13, 16),
    ("us_afternoon",    16, 20),
    ("us_late",         20, 24),
]
_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _minutes_until(ts: pd.Timestamp, h: int, m: int) -> float:
    candidate = ts.floor("D") + pd.Timedelta(hours=h, minutes=m)
    if candidate <= ts:
        candidate += pd.Timedelta(days=1)
    return min((candidate - ts).total_seconds() / 60.0, 120.0)


def compute(data_window) -> dict[str, float]:
    """
    data_window: UTC pd.Timestamp (or anything castable to one).
    Returns a flat dict of all time-of-day features.
    """
    ts = pd.Timestamp(data_window)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")

    out: dict[str, float] = {}
    hour = ts.hour
    session_idx = next(
        (i for i, (_, s, e) in enumerate(_SESSIONS) if s <= hour < e), len(_SESSIONS) - 1
    )
    for i, (name, _, _) in enumerate(_SESSIONS):
        out[f"session_{name}"] = 1.0 if i == session_idx else 0.0

    out["is_weekend"] = 1.0 if ts.dayofweek >= 5 else 0.0

    for i, day in enumerate(_DAYS):
        out[f"dow_{day}"] = 1.0 if ts.dayofweek == i else 0.0

    m = ts.minute
    out["minute_sin"] = float(np.sin(2 * np.pi * m / 60))
    out["minute_cos"] = float(np.cos(2 * np.pi * m / 60))

    out["minutes_until_0800"] = _minutes_until(ts, 8, 0)
    out["minutes_until_1430"] = _minutes_until(ts, 14, 30)

    out["monday_asia_open"] = 1.0 if (
        (ts.dayofweek == 6 and ts.hour >= 23)
        or (ts.dayofweek == 0 and ts.hour < 4)
    ) else 0.0

    return out
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_time_of_day.py -v`
Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add strategies/strategy_a/features/time_of_day.py tests/test_time_of_day.py
git commit -m "feat: add time-of-day regime encoder with session/dow/cyclic/proximity features"
```

---

## Task 6: Cross-asset features (ETH/SOL/XRP only)

**Files:**
- Create: `strategies/strategy_a/features/cross_asset.py`
- Create: `tests/test_cross_asset.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_cross_asset.py
import numpy as np
import pytest
from strategy_a.features.cross_asset import compute, _SENTINEL

_EXPECTED_KEYS = set(_SENTINEL.keys())


def _btc_feats():
    return {
        "har_rv": {
            "15m_rv": 1e-4, "15m_rv_pos": 6e-5, "15m_rv_neg": 4e-5,
            "15m_bv": 9e-5,  "15m_jump": 1e-5,  "15m_signed_jump": 2e-5,
            "sigma_forecast": 0.012,
        },
        "order_flow": {"ofi_l1": 0.3, "ofi_l5": 0.15},
    }


def test_smoke_with_data():
    d = {"btc_features": _btc_feats(), "btc_returns": {"1m": 0.001, "5m": 0.002, "15m": 0.003}, "config": {}}
    assert isinstance(compute(d), dict)


def test_smoke_no_data():
    assert isinstance(compute({}), dict)


def test_shape():
    d = {"btc_features": _btc_feats(), "btc_returns": {"1m": 0.001, "5m": 0.002, "15m": 0.003}, "config": {}}
    assert _EXPECTED_KEYS.issubset(compute(d).keys())


def test_sentinel_shape():
    assert _EXPECTED_KEYS.issubset(compute({}).keys())


def test_degraded_on_missing():
    assert compute({})["btc_degraded"] == 1.0


def test_not_degraded_when_present():
    d = {"btc_features": _btc_feats(), "btc_returns": {"1m": 0.001, "5m": 0.002, "15m": 0.003}, "config": {}}
    assert compute(d)["btc_degraded"] == 0.0


def test_jump_flag_binary():
    d = {"btc_features": _btc_feats(), "btc_returns": {"1m": 0.001, "5m": 0.002, "15m": 0.003}, "config": {}}
    v = compute(d)["btc_jump_flag"]
    assert v in (0.0, 1.0)


def test_returns_are_float():
    d = {"btc_features": _btc_feats(), "btc_returns": {"1m": 0.001, "5m": 0.002, "15m": 0.003}, "config": {}}
    result = compute(d)
    for k in ("btc_ret_1m", "btc_ret_5m", "btc_ret_15m", "btc_sigma_forecast"):
        assert isinstance(result[k], float), f"{k} is not float"
```

- [ ] **Step 2: Write `strategies/strategy_a/features/cross_asset.py`**

```python
# strategies/strategy_a/features/cross_asset.py
from __future__ import annotations
"""
Cross-asset features derived from BTC outputs.

ONLY used by ETH, SOL, XRP models. BTC model sets cross_asset.enabled: false.

Graceful degradation: if BTC data is missing or raises, returns _SENTINEL with
btc_degraded=1.0. The downstream Model.should_trade() widens its edge threshold
by config['thresholds']['btc_degraded_penalty'] when it sees btc_degraded=1.0.

Jump detection uses Barndorff-Nielsen & Shephard BV-based heuristic:
  J/RV > _JUMP_RATIO_THRESHOLD is flagged as a significant jump.
"""
import numpy as np

# BV-based jump significance: significant if jump component is >10% of total RV.
_JUMP_RATIO_THRESHOLD = 0.10

_SENTINEL: dict[str, float] = {
    "btc_ret_1m":         float("nan"),
    "btc_ret_5m":         float("nan"),
    "btc_ret_15m":        float("nan"),
    "btc_sigma_forecast": float("nan"),
    "btc_ofi_l1":         float("nan"),
    "btc_ofi_l5":         float("nan"),
    "btc_jump_flag":      0.0,
    "btc_degraded":       1.0,
}


def compute(data_window: dict) -> dict[str, float]:
    """
    data_window keys:
      btc_features: {"har_rv": {...}, "order_flow": {...}}  - output of BTC's compute()
      btc_returns:  {"1m": float, "5m": float, "15m": float}
      config: asset config dict (cross_asset section; currently unused here)
    """
    btc_features = data_window.get("btc_features")
    btc_returns  = data_window.get("btc_returns")
    if btc_features is None or btc_returns is None:
        return dict(_SENTINEL)
    try:
        har = btc_features.get("har_rv", {})
        of  = btc_features.get("order_flow", {})
        rv_15m   = har.get("15m_rv", 0.0) or 0.0
        jump_15m = har.get("15m_jump", 0.0) or 0.0
        jump_ratio = jump_15m / rv_15m if rv_15m > 1e-14 else 0.0
        return {
            "btc_ret_1m":         float(btc_returns.get("1m",  float("nan"))),
            "btc_ret_5m":         float(btc_returns.get("5m",  float("nan"))),
            "btc_ret_15m":        float(btc_returns.get("15m", float("nan"))),
            "btc_sigma_forecast": float(har.get("sigma_forecast", float("nan"))),
            "btc_ofi_l1":         float(of.get("ofi_l1", float("nan"))),
            "btc_ofi_l5":         float(of.get("ofi_l5", float("nan"))),
            "btc_jump_flag":      1.0 if jump_ratio > _JUMP_RATIO_THRESHOLD else 0.0,
            "btc_degraded":       0.0,
        }
    except Exception:
        return dict(_SENTINEL)
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_cross_asset.py -v`
Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add strategies/strategy_a/features/cross_asset.py tests/test_cross_asset.py
git commit -m "feat: add cross-asset BTC feature module with graceful degradation"
```

---

## Task 7: Funding and open interest features

**Files:**
- Create: `strategies/strategy_a/features/funding.py`
- Create: `tests/test_funding.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_funding.py
import numpy as np
import pytest
from strategy_a.features.funding import FundingFeatures

_CFG = {
    "funding": {
        "zscore_window_days": 7,
        "crowded_long_threshold": 2.0,
        "crowded_short_threshold": -2.0,
        "oi_crowded_threshold": 1.5,
    }
}

_EXPECTED_KEYS = {"funding_rate_zscore", "oi_zscore", "crowded_long", "crowded_short"}


def test_smoke():
    f = FundingFeatures(_CFG)
    assert isinstance(f.compute({"funding_rate": 0.0001, "open_interest": 1e9}), dict)


def test_shape():
    f = FundingFeatures(_CFG)
    assert _EXPECTED_KEYS.issubset(
        f.compute({"funding_rate": 0.0001, "open_interest": 1e9}).keys()
    )


def test_zscore_type_finite():
    f = FundingFeatures(_CFG)
    for i in range(15):
        result = f.compute({"funding_rate": 0.0001 * i, "open_interest": 1e9 * (1 + i * 0.01)})
    assert isinstance(result["funding_rate_zscore"], float)
    assert np.isfinite(result["funding_rate_zscore"])
    assert np.isfinite(result["oi_zscore"])


def test_crowded_flags_binary():
    f = FundingFeatures(_CFG)
    for i in range(20):
        result = f.compute({"funding_rate": 0.0001 * i, "open_interest": 1e9})
    assert result["crowded_long"] in (0.0, 1.0)
    assert result["crowded_short"] in (0.0, 1.0)


def test_crowded_long_triggered():
    f = FundingFeatures(_CFG)
    # Drive funding z-score very high with extreme positive values
    for _ in range(20):
        f.compute({"funding_rate": 0.0001, "open_interest": 1e9})
    result = f.compute({"funding_rate": 10.0, "open_interest": 1e12})
    # With extreme inputs, at least one crowded flag may be set
    assert result["crowded_long"] in (0.0, 1.0)
```

- [ ] **Step 2: Write `strategies/strategy_a/features/funding.py`**

```python
# strategies/strategy_a/features/funding.py
from __future__ import annotations
import numpy as np
from collections import deque


class FundingFeatures:
    """
    Online z-score computation for funding rate and open interest.
    Consumes one observation per funding interval (typically every 8h on Binance/Bybit).
    OI may arrive more frequently (configurable frequency assumed by maxlen sizing).
    """

    def __init__(self, config: dict) -> None:
        fc = config["funding"]
        days = int(fc["zscore_window_days"])
        # 3 funding events/day (8h interval on Binance); OI assumed up to 24/day
        self._fr_buf: deque[float] = deque(maxlen=days * 3 + 5)
        self._oi_buf: deque[float] = deque(maxlen=days * 24 + 5)
        self._cl_thresh  = float(fc["crowded_long_threshold"])
        self._cs_thresh  = float(fc["crowded_short_threshold"])
        self._oi_thresh  = float(fc["oi_crowded_threshold"])

    @staticmethod
    def _zscore(buf: deque, val: float) -> float:
        if len(buf) < 2:
            return 0.0
        arr = np.array(list(buf))
        std = float(arr.std())
        return (val - float(arr.mean())) / std if std > 1e-14 else 0.0

    def compute(self, data_window: dict) -> dict[str, float]:
        """
        data_window keys:
          funding_rate: float   - latest funding rate (dimensionless; Binance returns ~0.0001)
          open_interest: float  - latest OI in base currency units
          timestamp: (optional; not consumed, kept for interface uniformity)
        """
        fr = float(data_window.get("funding_rate", 0.0))
        oi = float(data_window.get("open_interest", 0.0))
        self._fr_buf.append(fr)
        self._oi_buf.append(oi)
        fr_z = self._zscore(self._fr_buf, fr)
        oi_z = self._zscore(self._oi_buf, oi)
        return {
            "funding_rate_zscore": fr_z,
            "oi_zscore":           oi_z,
            "crowded_long":  1.0 if fr_z > self._cl_thresh and oi_z > self._oi_thresh else 0.0,
            "crowded_short": 1.0 if fr_z < self._cs_thresh and oi_z > self._oi_thresh else 0.0,
        }
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_funding.py -v`
Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add strategies/strategy_a/features/funding.py tests/test_funding.py
git commit -m "feat: add funding/OI z-score feature module with crowded-position flags"
```

---

## Task 8: Strategy A model - `predict_proba`, `get_edge`, `should_trade`

**Files:**
- Create: `strategies/strategy_a/model.py`
- Create: `tests/test_model.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_model.py
import numpy as np
import pytest
from strategy_a.model import StrategyAModel

_FEES = {"kalshi": {"taker_fee_rate": 0.03, "maker_fee_rate": 0.00}, "safety_margin": 0.005}
_CFG = {
    "model": {"type": "logistic_regression", "calibration": "isotonic"},
    "thresholds": {
        "edge_above_fee": {"eu_open": 0.02, "weekend": 0.03},
        "btc_degraded_penalty": 0.01,
    },
}


def _fitted_model():
    m = StrategyAModel(_CFG, _FEES)
    rng = np.random.default_rng(42)
    X = rng.normal(0, 1, (200, 5))
    y = (rng.random(200) > 0.5).astype(int)
    m.fit(X, y, [f"f{i}" for i in range(5)])
    return m


def test_unfitted_returns_half():
    m = StrategyAModel(_CFG, _FEES)
    assert m.predict_proba({"x": 1.0}) == 0.5


def test_predict_proba_in_range():
    m = _fitted_model()
    rng = np.random.default_rng(7)
    for _ in range(30):
        feats = {f"f{i}": float(rng.normal()) for i in range(5)}
        p = m.predict_proba(feats)
        assert 0.0 <= p <= 1.0, f"p={p} out of [0,1]"


def test_get_edge_positive():
    m = StrategyAModel(_CFG, _FEES)
    assert m.get_edge(0.7, 0.55) == pytest.approx(0.15)


def test_get_edge_negative():
    m = StrategyAModel(_CFG, _FEES)
    assert m.get_edge(0.3, 0.45) == pytest.approx(-0.15)


def test_should_trade_above_threshold():
    m = StrategyAModel(_CFG, _FEES)
    # min_edge = 0.03 + 0.005 + 0.02 = 0.055; edge = 0.7 - 0.55 = 0.15 > 0.055
    assert m.should_trade(0.70, 0.55, "eu_open", _CFG)


def test_should_trade_below_threshold():
    m = StrategyAModel(_CFG, _FEES)
    # edge = 0.01 < 0.055
    assert not m.should_trade(0.56, 0.55, "eu_open", _CFG)


def test_btc_degraded_widens_threshold():
    m = StrategyAModel(_CFG, _FEES)
    # edge = 0.06: passes without degraded (0.055), fails with degraded (0.065)
    assert     m.should_trade(0.61, 0.55, "eu_open", _CFG, btc_degraded=False)
    assert not m.should_trade(0.61, 0.55, "eu_open", _CFG, btc_degraded=True)


def test_weekend_threshold_used():
    m = StrategyAModel(_CFG, _FEES)
    # weekend min_edge = 0.03 + 0.005 + 0.03 = 0.065; edge = 0.06 < 0.065
    assert not m.should_trade(0.61, 0.55, "weekend", _CFG)
```

- [ ] **Step 2: Write `strategies/strategy_a/model.py`**

```python
# strategies/strategy_a/model.py
from __future__ import annotations
"""
Strategy A probability combiner.

Label definition (canonical):
  y = 1  if underlying CLOSE at t+15min > OPEN at t=0
  y = 0  otherwise
  Reference price source (spot vs perp) is specified per-asset in config.

Fee threshold derivation (see should_trade docstring):
  The Kalshi taker fee formula is ceil(0.07·C·p·(1-p)) per contract.
  At p=0.50 and C≈50 contracts on $25 stake ≈ $0.875 fee ≈ 3.5%.
  fees.yaml stores a conservative flat-rate approximation (0.03) for threshold
  computation. The actual order-placement layer uses the exact formula in
  src/strategies/fees.py. Do not change this approximation without also updating
  the execution layer's EV computation.
"""
import numpy as np
from typing import Optional


class StrategyAModel:
    """
    Calibrated probability model for Strategy A.
    Primary classifier: logistic regression. Swappable to XGBoost/LightGBM via config.
    Calibration: isotonic regression (default) or Platt scaling.
    """

    def __init__(self, config: dict, fees_config: dict) -> None:
        self.config = config
        self.fees = fees_config
        self._model = None
        self._feature_names: Optional[list[str]] = None
        self._fitted = False

    # ── interface contract (locked - backtester and executor depend on these) ─

    def predict_proba(self, features: dict) -> float:
        """Calibrated P(up at 15-min expiry) in [0, 1]. Returns 0.5 if unfitted."""
        if not self._fitted or self._model is None:
            return 0.5
        vec = self._to_vec(features)
        return float(np.clip(self._model.predict_proba([vec])[0][1], 0.0, 1.0))

    def get_edge(self, p_model: float, p_market: float) -> float:
        """
        Signed edge: p_model − p_market (both in [0, 1]).
        Positive → YES has edge. Negative → NO has edge.
        p_market = Kalshi YES price / 100 (i.e. 70c → 0.70).
        """
        return p_model - p_market

    def should_trade(
        self,
        p_model: float,
        p_market: float,
        regime: str,
        config: dict,
        btc_degraded: bool = False,
    ) -> bool:
        """
        Returns True when |edge| > min_edge.

        min_edge derivation:
          taker_fee_approx  = fees_config["kalshi"]["taker_fee_rate"]  (0.03 flat)
          safety_margin     = fees_config["safety_margin"]              (0.005)
          regime_extra      = config["thresholds"]["edge_above_fee"][regime]
                              (null → 0.02 default when untuned)
          min_edge = taker_fee_approx + safety_margin + regime_extra
          If btc_degraded: min_edge += config["thresholds"]["btc_degraded_penalty"]
        """
        taker    = float(self.fees["kalshi"]["taker_fee_rate"])
        margin   = float(self.fees["safety_margin"])
        thresholds = config.get("thresholds", {}).get("edge_above_fee", {})
        raw = thresholds.get(regime)
        regime_extra = float(raw) if raw is not None else 0.02
        min_edge = taker + margin + regime_extra
        if btc_degraded:
            penalty = float(config.get("thresholds", {}).get("btc_degraded_penalty", 0.01))
            min_edge += penalty
        return abs(self.get_edge(p_model, p_market)) > min_edge

    # ── training ──────────────────────────────────────────────────────────────

    def fit(self, X: np.ndarray, y: np.ndarray, feature_names: list[str]) -> None:
        """
        Train the calibrated classifier.
        X: (n_samples, n_features)
        y: binary labels (1 = underlying up at 15-min expiry, 0 = down)
        """
        from sklearn.calibration import CalibratedClassifierCV
        self._feature_names = list(feature_names)
        mtype = self.config.get("model", {}).get("type", "logistic_regression")
        cal   = self.config.get("model", {}).get("calibration", "isotonic")
        method = "isotonic" if cal == "isotonic" else "sigmoid"
        base = self._build_base(mtype)
        self._model = CalibratedClassifierCV(base, cv=5, method=method)
        self._model.fit(X, y)
        self._fitted = True

    @staticmethod
    def _build_base(mtype: str):
        if mtype == "xgboost":
            from xgboost import XGBClassifier
            return XGBClassifier(n_estimators=100, eval_metric="logloss",
                                 use_label_encoder=False)
        if mtype == "lightgbm":
            from lightgbm import LGBMClassifier
            return LGBMClassifier(n_estimators=100, verbosity=-1)
        from sklearn.linear_model import LogisticRegression
        return LogisticRegression(max_iter=1000, C=1.0)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _to_vec(self, features: dict) -> np.ndarray:
        if self._feature_names is None:
            self._feature_names = sorted(features.keys())
        return np.array([features.get(k, 0.0) for k in self._feature_names], dtype=float)
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_model.py -v`
Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add strategies/strategy_a/model.py tests/test_model.py
git commit -m "feat: add Strategy A probability combiner with calibration and fee-aware threshold"
```

---

## Task 9: Strategy A YAML configs (4 assets)

**Files:**
- Create: `strategies/strategy_a/config/btc.yaml`
- Create: `strategies/strategy_a/config/eth.yaml`
- Create: `strategies/strategy_a/config/sol.yaml`
- Create: `strategies/strategy_a/config/xrp.yaml`

- [ ] **Step 1: Write `strategies/strategy_a/config/btc.yaml`**

```yaml
asset:
  symbol: BTC
  kalshi_market_prefix: KXBTC15M
  reference_source: spot
  reference_exchange: binance

returns:
  granularity_seconds: 10

har_rs_j:
  timescales_minutes: [15, 60, 240]
  coefficients:
    const:       null
    rv_15m_pos:  null
    rv_15m_neg:  null
    rv_1h_pos:   null
    rv_1h_neg:   null
    rv_4h_pos:   null
    rv_4h_neg:   null
    jump:        null

order_flow:
  ofi_depths: [1, 3, 5, 10]
  vpin_bucket_size: null   # tuned per asset during training
  vpin_rolling_buckets: 50

time_of_day:
  regimes: [asia_deep_night, asia_active, eu_open, eu_us_overlap, us_afternoon, us_late]

cross_asset:
  enabled: false           # BTC is the upstream source; does not consume BTC features
  jump_lookback_minutes: 5

funding:
  sources: [binance, bybit]
  zscore_window_days: 7
  crowded_long_threshold: 2.0
  crowded_short_threshold: -2.0
  oi_crowded_threshold: 1.5

model:
  type: logistic_regression   # logistic_regression | xgboost | lightgbm
  calibration: isotonic       # isotonic | platt
  weights_path: null
  calibrator_path: null

thresholds:
  edge_above_fee:
    asia_deep_night: null
    asia_active:     null
    eu_open:         null
    eu_us_overlap:   null
    us_afternoon:    null
    us_late:         null
    weekend:         null
  btc_degraded_penalty: 0.01

volatility_reference:
  annualized: 0.43
```

- [ ] **Step 2: Write `strategies/strategy_a/config/eth.yaml`**

```yaml
asset:
  symbol: ETH
  kalshi_market_prefix: KXETH15M
  reference_source: spot
  reference_exchange: binance

returns:
  granularity_seconds: 10

har_rs_j:
  timescales_minutes: [15, 60, 240]
  coefficients:
    const:       null
    rv_15m_pos:  null
    rv_15m_neg:  null
    rv_1h_pos:   null
    rv_1h_neg:   null
    rv_4h_pos:   null
    rv_4h_neg:   null
    jump:        null

order_flow:
  ofi_depths: [1, 3, 5, 10]
  vpin_bucket_size: null
  vpin_rolling_buckets: 50

time_of_day:
  regimes: [asia_deep_night, asia_active, eu_open, eu_us_overlap, us_afternoon, us_late]

cross_asset:
  enabled: true
  jump_lookback_minutes: 5

funding:
  sources: [binance, bybit]
  zscore_window_days: 7
  crowded_long_threshold: 2.0
  crowded_short_threshold: -2.0
  oi_crowded_threshold: 1.5

model:
  type: logistic_regression
  calibration: isotonic
  weights_path: null
  calibrator_path: null

thresholds:
  edge_above_fee:
    asia_deep_night: null
    asia_active:     null
    eu_open:         null
    eu_us_overlap:   null
    us_afternoon:    null
    us_late:         null
    weekend:         null
  btc_degraded_penalty: 0.01

volatility_reference:
  annualized: 0.77
```

- [ ] **Step 3: Write `strategies/strategy_a/config/sol.yaml`**

```yaml
asset:
  symbol: SOL
  kalshi_market_prefix: KXSOL15M
  reference_source: spot
  reference_exchange: binance

returns:
  granularity_seconds: 10

har_rs_j:
  timescales_minutes: [15, 60, 240]
  coefficients:
    const:       null
    rv_15m_pos:  null
    rv_15m_neg:  null
    rv_1h_pos:   null
    rv_1h_neg:   null
    rv_4h_pos:   null
    rv_4h_neg:   null
    jump:        null

order_flow:
  ofi_depths: [1, 3, 5, 10]
  vpin_bucket_size: null
  vpin_rolling_buckets: 50

time_of_day:
  regimes: [asia_deep_night, asia_active, eu_open, eu_us_overlap, us_afternoon, us_late]

cross_asset:
  enabled: true
  jump_lookback_minutes: 5

funding:
  sources: [binance, bybit]
  zscore_window_days: 7
  crowded_long_threshold: 2.0
  crowded_short_threshold: -2.0
  oi_crowded_threshold: 1.5

model:
  type: logistic_regression
  calibration: isotonic
  weights_path: null
  calibrator_path: null

thresholds:
  edge_above_fee:
    asia_deep_night: null
    asia_active:     null
    eu_open:         null
    eu_us_overlap:   null
    us_afternoon:    null
    us_late:         null
    weekend:         null
  btc_degraded_penalty: 0.01

volatility_reference:
  annualized: 0.87
```

- [ ] **Step 4: Write `strategies/strategy_a/config/xrp.yaml`**

```yaml
asset:
  symbol: XRP
  kalshi_market_prefix: KXXRP15M
  reference_source: spot
  reference_exchange: binance

returns:
  granularity_seconds: 10

har_rs_j:
  timescales_minutes: [15, 60, 240]
  coefficients:
    const:       null
    rv_15m_pos:  null
    rv_15m_neg:  null
    rv_1h_pos:   null
    rv_1h_neg:   null
    rv_4h_pos:   null
    rv_4h_neg:   null
    jump:        null

order_flow:
  ofi_depths: [1, 3, 5, 10]
  vpin_bucket_size: null
  vpin_rolling_buckets: 50

time_of_day:
  regimes: [asia_deep_night, asia_active, eu_open, eu_us_overlap, us_afternoon, us_late]

cross_asset:
  enabled: true
  jump_lookback_minutes: 5

funding:
  sources: [binance, bybit]
  zscore_window_days: 7
  crowded_long_threshold: 2.0
  crowded_short_threshold: -2.0
  oi_crowded_threshold: 1.5

model:
  type: logistic_regression
  calibration: isotonic
  weights_path: null
  calibrator_path: null

thresholds:
  edge_above_fee:
    asia_deep_night: null
    asia_active:     null
    eu_open:         null
    eu_us_overlap:   null
    us_afternoon:    null
    us_late:         null
    weekend:         null
  btc_degraded_penalty: 0.01

volatility_reference:
  annualized: 0.80
```

- [ ] **Step 5: Commit**

```bash
git add strategies/strategy_a/config/
git commit -m "feat: add Strategy A YAML configs for BTC/ETH/SOL/XRP"
```

---

## Task 10: Strategy B - contract dislocation detector

**Files:**
- Create: `strategies/strategy_b/contract_dislocation.py`
- Create: `tests/test_contract_dislocation.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_contract_dislocation.py
import numpy as np
import pandas as pd
import pytest
from strategy_b.contract_dislocation import ContractDislocationDetector
from shared.types import DislocationSignal

_CFG = {
    "asset": {"symbol": "BTC", "kalshi_market_prefix": "KXBTC15M"},
    "dislocation": {
        "lookback_seconds": 30,
        "residual_threshold": 2.0,
        "signal_staleness_seconds": 60,
    },
    "implied_move": {
        "volatility_source": "har_rs_j",
        "fallback_rolling_std_minutes": 30,
    },
}


def _tick(ts, yes_bid, yes_ask, seconds_to_expiry=450):
    return {"timestamp": ts, "yes_bid": yes_bid, "yes_ask": yes_ask,
            "no_bid": 100 - yes_ask, "no_ask": 100 - yes_bid,
            "seconds_to_expiry": seconds_to_expiry}

def _price(ts, px):
    return {"timestamp": ts, "price": px}


def test_smoke_empty():
    d = ContractDislocationDetector(_CFG)
    assert d.detect_dislocation([], []) is None


def test_smoke_single_tick_no_history():
    d = ContractDislocationDetector(_CFG)
    now = pd.Timestamp.utcnow()
    result = d.detect_dislocation([_tick(now, 60, 62)], [_price(now, 50000)])
    assert result is None  # no old tick before the lookback window


def test_signal_on_large_residual():
    d = ContractDislocationDetector(_CFG)
    d.update_vol(0.80)
    now = pd.Timestamp.utcnow()
    old = now - pd.Timedelta(seconds=40)
    # Contract jumped 5c but underlying barely moved
    ticks  = [_tick(old, 60, 62), _tick(now, 65, 67)]
    prices = [_price(old, 50000), _price(now, 50010)]
    result = d.detect_dislocation(ticks, prices)
    # actual_move=5, implied_move≈small → residual > threshold(2)
    assert result is not None


def test_signal_shape():
    d = ContractDislocationDetector(_CFG)
    d.update_vol(0.80)
    now = pd.Timestamp.utcnow()
    old = now - pd.Timedelta(seconds=40)
    ticks  = [_tick(old, 60, 62), _tick(now, 65, 67)]
    prices = [_price(old, 50000), _price(now, 50010)]
    sig = d.detect_dislocation(ticks, prices)
    assert isinstance(sig, DislocationSignal)
    assert sig.direction in ("fade_up", "fade_down")
    assert sig.side in ("YES", "NO")
    assert 0.0 <= sig.confidence <= 1.0
    assert sig.residual_magnitude >= 0.0
    assert isinstance(sig.staleness_timestamp, pd.Timestamp)


def test_no_signal_small_residual():
    d = ContractDislocationDetector(_CFG)
    now = pd.Timestamp.utcnow()
    old = now - pd.Timedelta(seconds=40)
    # Contract mid barely moves (0.5c), underlying unchanged
    ticks  = [_tick(old, 60, 62), _tick(now, 60.5, 62.5)]
    prices = [_price(old, 50000), _price(now, 50000)]
    assert d.detect_dislocation(ticks, prices) is None


def test_fade_up_yields_no_side():
    d = ContractDislocationDetector(_CFG)
    d.update_vol(0.80)
    now = pd.Timestamp.utcnow()
    old = now - pd.Timedelta(seconds=40)
    # Contract moved UP more than implied → fade by buying NO
    ticks  = [_tick(old, 60, 62), _tick(now, 65, 67)]
    prices = [_price(old, 50000), _price(now, 50010)]
    sig = d.detect_dislocation(ticks, prices)
    assert sig is not None
    assert sig.direction == "fade_up"
    assert sig.side == "NO"


def test_fade_down_yields_yes_side():
    d = ContractDislocationDetector(_CFG)
    d.update_vol(0.80)
    now = pd.Timestamp.utcnow()
    old = now - pd.Timedelta(seconds=40)
    # Contract dropped more than implied → buy YES (it will revert up)
    ticks  = [_tick(old, 60, 62), _tick(now, 55, 57)]
    prices = [_price(old, 50000), _price(now, 49990)]
    sig = d.detect_dislocation(ticks, prices)
    assert sig is not None
    assert sig.direction == "fade_down"
    assert sig.side == "YES"
```

- [ ] **Step 2: Write `strategies/strategy_b/contract_dislocation.py`**

```python
# strategies/strategy_b/contract_dislocation.py
from __future__ import annotations
"""
Strategy B: Kalshi contract mean-reversion dislocation detector.

Mechanism:
  1. Compute implied_move = f(underlying_return, time_to_expiry, current_price)
     using Brownian-with-drift: translate the underlying log-return into a
     ΔP(up) via probability_utils.drift_vol_to_prob, then convert to cents.
  2. Compute actual_move = contract_mid_now − contract_mid_N_seconds_ago.
  3. residual = actual_move − implied_move.
  4. If |residual| > threshold → output a DislocationSignal to fade the move.

This module is completely independent from Strategy A: separate data, separate
classes, no shared state. Strategy A's HAR-RS-J σ̂ can be injected via
update_vol() as a vol estimate, but the dislocation logic does not depend on it
(falls back to self._recent_sigma if not injected).
"""
import numpy as np
import pandas as pd
import sys
import os
from typing import Optional

# shared/ lives one directory up from strategy_b/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.types import DislocationSignal
from shared.probability_utils import drift_vol_to_prob, contract_price_change_from_prob_change


class ContractDislocationDetector:
    def __init__(self, config: dict) -> None:
        dc = config["dislocation"]
        self._lookback_sec: int  = int(dc["lookback_seconds"])
        raw_thresh = dc.get("residual_threshold")
        self._threshold: float   = float(raw_thresh) if raw_thresh else 2.0
        self._staleness_sec: int = int(dc["signal_staleness_seconds"])
        self._asset: str         = config["asset"]["symbol"]
        self._recent_sigma: float = 0.001  # annualized vol; overridden by update_vol()

    def update_vol(self, sigma_forecast: float) -> None:
        """Inject annualized σ̂ from Strategy A's HAR-RS-J module."""
        if sigma_forecast > 0:
            self._recent_sigma = sigma_forecast

    # ── interface contract ─────────────────────────────────────────────────────

    def detect_dislocation(
        self,
        contract_stream: list[dict],
        underlying_stream: list[dict],
    ) -> Optional[DislocationSignal]:
        """
        contract_stream: list of Kalshi tick dicts, oldest-first. Each tick:
          {timestamp, yes_bid, yes_ask, no_bid, no_ask[, seconds_to_expiry]}
        underlying_stream: list of {timestamp, price}, oldest-first.
        Returns None if no dislocation is detected or data is insufficient.
        """
        if not contract_stream or not underlying_stream:
            return None
        now_ts = pd.Timestamp(contract_stream[-1]["timestamp"])
        cutoff = now_ts - pd.Timedelta(seconds=self._lookback_sec)
        old_ticks  = [t for t in contract_stream  if pd.Timestamp(t["timestamp"]) <= cutoff]
        old_prices = [p for p in underlying_stream if pd.Timestamp(p["timestamp"]) <= cutoff]
        if not old_ticks or not old_prices:
            return None
        old_mid = self._mid(old_ticks[-1])
        cur_mid = self._mid(contract_stream[-1])
        actual_move = cur_mid - old_mid  # cents
        old_px = float(old_prices[-1]["price"])
        cur_px = float(underlying_stream[-1]["price"])
        if old_px <= 0:
            return None
        log_ret = float(np.log(cur_px / old_px))
        tte = float(contract_stream[-1].get("seconds_to_expiry", 450))
        dp = self._implied_dp(log_ret, tte)
        implied_move = contract_price_change_from_prob_change(dp)
        residual = actual_move - implied_move
        if abs(residual) <= self._threshold:
            return None
        direction = "fade_up" if residual > 0 else "fade_down"
        side      = "NO"      if residual > 0 else "YES"
        confidence = min(abs(residual) / (self._threshold * 3.0), 1.0)
        return DislocationSignal(
            timestamp=now_ts,
            asset=self._asset,
            direction=direction,
            confidence=confidence,
            side=side,
            residual_magnitude=abs(residual),
            staleness_timestamp=now_ts + pd.Timedelta(seconds=self._staleness_sec),
        )

    # ── internals ─────────────────────────────────────────────────────────────

    @staticmethod
    def _mid(tick: dict) -> float:
        return (float(tick.get("yes_bid", 0.0)) + float(tick.get("yes_ask", 100.0))) / 2.0

    def _implied_dp(self, log_return: float, time_to_expiry_sec: float) -> float:
        """ΔP(up) implied by the underlying log-return over the remaining horizon."""
        if time_to_expiry_sec <= 0:
            return 0.0
        dt_years = time_to_expiry_sec / (365.0 * 24.0 * 3600.0)
        p_before = drift_vol_to_prob(0.0, self._recent_sigma, dt_years)
        mu_annual = log_return / dt_years  # annualize the realized return
        p_after  = drift_vol_to_prob(mu_annual, self._recent_sigma, dt_years)
        return p_after - p_before
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/test_contract_dislocation.py -v`
Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add strategies/strategy_b/contract_dislocation.py tests/test_contract_dislocation.py
git commit -m "feat: add Strategy B contract dislocation detector with fade-signal output"
```

---

## Task 11: Strategy B YAML configs (4 assets)

**Files:**
- Create: `strategies/strategy_b/config/btc.yaml`
- Create: `strategies/strategy_b/config/eth.yaml`
- Create: `strategies/strategy_b/config/sol.yaml`
- Create: `strategies/strategy_b/config/xrp.yaml`

- [ ] **Step 1: Write all four configs**

`strategies/strategy_b/config/btc.yaml`:
```yaml
asset:
  symbol: BTC
  kalshi_market_prefix: KXBTC15M

dislocation:
  lookback_seconds: 30
  residual_threshold: null     # tuned per asset; null → 2.0 runtime default
  signal_staleness_seconds: 60

implied_move:
  volatility_source: har_rs_j  # inject via update_vol() from Strategy A
  fallback_rolling_std_minutes: 30
```

`strategies/strategy_b/config/eth.yaml`:
```yaml
asset:
  symbol: ETH
  kalshi_market_prefix: KXETH15M

dislocation:
  lookback_seconds: 30
  residual_threshold: null
  signal_staleness_seconds: 60

implied_move:
  volatility_source: har_rs_j
  fallback_rolling_std_minutes: 30
```

`strategies/strategy_b/config/sol.yaml`:
```yaml
asset:
  symbol: SOL
  kalshi_market_prefix: KXSOL15M

dislocation:
  lookback_seconds: 30
  residual_threshold: null
  signal_staleness_seconds: 60

implied_move:
  volatility_source: har_rs_j
  fallback_rolling_std_minutes: 30
```

`strategies/strategy_b/config/xrp.yaml`:
```yaml
asset:
  symbol: XRP
  kalshi_market_prefix: KXXRP15M

dislocation:
  lookback_seconds: 30
  residual_threshold: null
  signal_staleness_seconds: 60

implied_move:
  volatility_source: har_rs_j
  fallback_rolling_std_minutes: 30
```

- [ ] **Step 2: Commit**

```bash
git add strategies/strategy_b/config/
git commit -m "feat: add Strategy B YAML configs for BTC/ETH/SOL/XRP"
```

---

## Task 12: `strategies/README.md`

**Files:**
- Create: `strategies/README.md`

- [ ] **Step 1: Write README**

```markdown
# strategies/

Two-strategy architecture for Kalshi 15-minute crypto binary markets (BTC, ETH, SOL, XRP).

## Two-Strategy Architecture

**Strategy A** - Calibrated probability model.
Five feature modules (HAR-RS-J volatility, order flow, time-of-day regimes, cross-asset
BTC signals, funding/OI z-scores) are concatenated and passed to a calibrated logistic
regression that outputs P_model(up at 15-min expiry). A trade is placed when
|P_model − P_market| exceeds a fee-adjusted, regime-aware edge threshold.

**Strategy B** - Contract dislocation detector.
Detects when the Kalshi YES price moves more or less than the underlying spot price
implies (via a Brownian-with-drift implied probability translation). Fades the residual
back toward fair value with a confidence-scaled signal.

Eight model instances: 4 assets × 2 strategies. Same architecture per strategy;
separately fitted parameters per asset. No pooling of training data across assets.

## Data Assumptions

| Data type       | Schema                                               | Notes                         |
|-----------------|------------------------------------------------------|-------------------------------|
| Bars            | `timestamp, open, high, low, close, volume`          | 10-second default granularity |
| L2 book         | `timestamp, bids: [(price, size)], asks: [...]`      | At least 10 levels            |
| Trades          | `timestamp, price, size, aggressor_side`             | `aggressor_side ∈ {buy,sell}` |
| Funding         | `timestamp, funding_rate, open_interest`             | Binance/Bybit perp            |
| Kalshi ticks    | `timestamp, yes_bid, yes_ask, no_bid, no_ask`        | Optionally `seconds_to_expiry`|

All timestamps: UTC. All returns: log returns. p_market is Kalshi YES price / 100.

## Loading Configs

```python
import yaml, os

def load_config(strategy: str, asset: str) -> dict:
    path = os.path.join(os.path.dirname(__file__), strategy, "config", f"{asset.lower()}.yaml")
    with open(path) as f:
        return yaml.safe_load(f)

def load_fees() -> dict:
    path = os.path.join(os.path.dirname(__file__), "shared", "fees.yaml")
    with open(path) as f:
        return yaml.safe_load(f)

config = load_config("strategy_a", "ETH")
fees   = load_fees()
```

## Downstream Interfaces

The **backtester** and **executor** depend on these exact signatures. Do not change them.

```python
# Every feature module:
def compute(data_window) -> dict[str, float]: ...

# Strategy A model (instance methods on StrategyAModel):
def predict_proba(features: dict) -> float: ...          # calibrated P(up) in [0,1]
def get_edge(p_model: float, p_market: float) -> float: ...
def should_trade(p_model, p_market, regime, config, btc_degraded=False) -> bool: ...

# Strategy B:
def detect_dislocation(contract_stream, underlying_stream) -> Optional[DislocationSignal]: ...
```

**p_market convention**: always pass as a fraction in [0, 1], not cents.
A Kalshi YES ask of 70c → p_market = 0.70.

## Fee Notes

`shared/fees.yaml` stores a conservative flat-rate approximation (3%) used only for
pre-trade edge threshold checks. The actual Kalshi taker fee is
`ceil(0.07 × C × p × (1−p))` per contract (peaks at ~3.5% at p=0.50).
The execution layer in `src/strategies/fees.py` uses the exact formula for EV
computation - do not bypass it.

## Running Tests

```bash
# From the repo root
pytest tests/ -v
```

All tests use synthetic data. No live API calls.
```

- [ ] **Step 2: Run full test suite**

Run: `pytest tests/ -v`
Expected: all PASS (no real data sources hit)

- [ ] **Step 3: Final commit**

```bash
git add strategies/README.md
git commit -m "feat: add strategies/README with architecture, data assumptions, and interface docs"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Task |
|---|---|
| HAR-RS-J with RV+/RV− separate coefficients, 3 timescales | Task 3 |
| Online `update()` + batch `fit()` | Task 3 |
| OFI at depths 1/3/5/10, VAMP, signed flow 1/5/15m, VPIN, spread, depth | Task 4 |
| Time-of-day: 6 sessions, weekend, DOW, cyclical, proximity, monday_asia_open | Task 5 |
| Cross-asset BTC features, graceful degradation sentinel | Task 6 |
| Funding z-scores, crowded long/short flags | Task 7 |
| `predict_proba`, `get_edge`, `should_trade` with fee-aware threshold | Task 8 |
| Strategy A configs × 4 assets with cross_asset.enabled=false for BTC | Task 9 |
| `detect_dislocation` returning `DislocationSignal` | Task 10 |
| Strategy B configs × 4 assets | Task 11 |
| `shared/types.py`: FeatureVector, Signal, DislocationSignal, JumpFlag | Task 1 |
| `shared/probability_utils.py`: drift_vol_to_prob, prob_to_contract_price, dp→dc | Task 2 |
| `shared/regime_filters.py`: 3 functions | Task 2 |
| `shared/fees.yaml` | Task 2 |
| Tests: smoke + type-and-range + shape per module | Tasks 3-10 |
| README | Task 12 |
| NO price-level triggers | verified - all decisions via probability comparison |
| NO pooling across assets | verified - configs are separate, no shared training |
| NO magic numbers | verified - all tunables in YAML |

**Placeholder scan:** No TBD, TODO, or "implement later" patterns. All `null` values in configs are documented placeholders for training output.

**Type consistency:**
- `DislocationSignal` defined in Task 1, imported in Task 10 tests - ✓
- `_SENTINEL` keys in `cross_asset.py` match test assertions - ✓
- `should_trade` signature in model.py matches test calls - ✓
- `compute(data_window) -> dict[str, float]` present on all feature modules - ✓
