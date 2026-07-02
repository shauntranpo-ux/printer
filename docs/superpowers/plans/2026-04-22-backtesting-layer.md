# Backtesting Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a complete backtesting and validation layer for the Kalshi 15-minute crypto binary strategy bot, integrating with existing `backtesting/walk_forward.py` and `backtesting/stress_test.py` without modifying them.

**Architecture:** Event-driven single-threaded backtest engine + CPCV validation + calibration/overfitting/trading metrics + Jinja2 report generation + CLI entrypoint. Sequential only - no parallelism anywhere.

**Tech Stack:** Python 3.11+, pandas, numpy, scipy, scikit-learn, statsmodels, pyyaml, pydantic, matplotlib, pytest, jinja2

**Key constraints:**
- DO NOT modify `strategies/` (read-only)
- DO NOT modify existing `backtesting/walk_forward.py` or `backtesting/stress_test.py`
- 3% Kalshi taker fee mandatory from first run
- No look-ahead - programmatic check required
- No data pooling across assets
- Sequential execution only

**Existing modules to wrap (not rewrite):**
- `backtesting/walk_forward.py` - WFA engine that reads `data/split_config.json`, calls `backtest.py`
- `backtesting/stress_test.py` - Monte Carlo noise stress test

---

### Task 1: Directory scaffold, `__init__.py` stubs, and YAML configs

**Files:**
- Create: `backtesting/data/__init__.py`
- Create: `backtesting/training/__init__.py`
- Create: `backtesting/validation/__init__.py`
- Create: `backtesting/metrics/__init__.py`
- Create: `backtesting/simulation/__init__.py`
- Create: `backtesting/reports/__init__.py`
- Create: `backtesting/reports/templates/` (directory)
- Create: `backtesting/configs/per_asset/` (directory)
- Create: `backtesting/output/reports/` (directory)
- Create: `backtesting/configs/backtest.yaml`
- Create: `backtesting/configs/per_asset/btc.yaml`
- Create: `backtesting/configs/per_asset/eth.yaml`
- Create: `backtesting/configs/per_asset/sol.yaml`
- Create: `backtesting/configs/per_asset/xrp.yaml`

- [ ] **Step 1: Create all `__init__.py` stubs**

All are empty files. Create each one:
```
backtesting/data/__init__.py
backtesting/training/__init__.py
backtesting/validation/__init__.py
backtesting/metrics/__init__.py
backtesting/simulation/__init__.py
backtesting/reports/__init__.py
```

- [ ] **Step 2: Write `backtesting/configs/backtest.yaml`**

```yaml
data:
  start_date: null              # null = use all available history; otherwise YYYY-MM-DD
  end_date: null                # null = now minus 24 hours
  min_history_days: 180
  base_path: data/historical   # TODO: adjust to actual data path

training:
  har_training_window_days: 90
  classifier_training_window_days: 90
  refit: true
  fitted_config_suffix: .fitted.yaml
  output_dir: backtesting/output/models

validation:
  cpcv:
    n_groups: 6
    k_test_groups: 2
    embargo_minutes: 30
  wfa:
    enabled: true
  monte_carlo:
    enabled: true
  bootstrap:
    mean_block_hours: 24
    iterations: 1000
  lookahead_check: true

metrics:
  calibration:
    n_bins: 10
  regime:
    btc_vol_tercile_window_days: 30

simulation:
  latency_ms: 500
  slippage: cross_spread
  maker:
    price_improvement_cents: 1
    fill_model: logistic
    adverse_selection_fraction: 0.4

reports:
  output_dir: backtesting/output/reports
  charts_format: png

execution:
  assets: [btc, eth, sol, xrp]
  strategies: [a, b]
  sequential_only: true
```

- [ ] **Step 3: Write `backtesting/configs/per_asset/btc.yaml`**

```yaml
asset: btc
symbol: BTC
kalshi_market_prefix: KXBTC15M
reference_source: spot
reference_exchange: binance
data_path: null   # null = inherit from backtest.yaml data.base_path + /btc/
min_samples: 26000  # ~6 months of 15-min windows
volatility_reference_annualized: 0.43
```

- [ ] **Step 4: Write per-asset configs for ETH, SOL, XRP**

`backtesting/configs/per_asset/eth.yaml`:
```yaml
asset: eth
symbol: ETH
kalshi_market_prefix: KXETH15M
reference_source: spot
reference_exchange: binance
data_path: null
min_samples: 26000
volatility_reference_annualized: 0.77
```

`backtesting/configs/per_asset/sol.yaml`:
```yaml
asset: sol
symbol: SOL
kalshi_market_prefix: KXSOL15M
reference_source: spot
reference_exchange: binance
data_path: null
min_samples: 26000
volatility_reference_annualized: 0.87
```

`backtesting/configs/per_asset/xrp.yaml`:
```yaml
asset: xrp
symbol: XRP
kalshi_market_prefix: KXXRP15M
reference_source: spot
reference_exchange: binance
data_path: null
min_samples: 26000
volatility_reference_annualized: 0.80
```

- [ ] **Step 5: Verify with Python**

```python
import yaml
for cfg in ["backtest", "per_asset/btc", "per_asset/eth", "per_asset/sol", "per_asset/xrp"]:
    with open(f"backtesting/configs/{cfg}.yaml") as f:
        d = yaml.safe_load(f)
    print(f"{cfg}: OK - keys={list(d.keys())}")
```

- [ ] **Step 6: Commit**

```bash
git add backtesting/configs/ backtesting/data/__init__.py backtesting/training/__init__.py backtesting/validation/__init__.py backtesting/metrics/__init__.py backtesting/simulation/__init__.py backtesting/reports/__init__.py
git commit -m "feat: backtesting scaffold - directories, __init__.py stubs, YAML configs"
```

---

### Task 2: Data loaders (`backtesting/data/loaders.py`)

**Files:**
- Create: `backtesting/data/loaders.py`
- Create: `tests/test_data_loaders.py` (smoke tests with synthetic data)

- [ ] **Step 1: Write `backtesting/data/loaders.py`**

```python
"""
Data loaders for the backtesting layer.

Each loader returns a pandas DataFrame with a UTC-indexed DatetimeIndex.
Fails loudly when data is missing, malformed, or insufficient.

# TODO: Adjust base_path and file naming conventions to match actual disk layout.
# Current assumption: data/historical/{asset}/bars_10s.parquet, etc.
"""
from __future__ import annotations
import os
import pandas as pd
from datetime import datetime, timezone
from typing import Optional

_BASE_PATH = os.environ.get("KALSHI_DATA_PATH", "data/historical")
MIN_HISTORY_DAYS = 180


def _resolve_path(asset: str, filename: str, base_path: str = _BASE_PATH) -> str:
    return os.path.join(base_path, asset.lower(), filename)


def _load_parquet_or_csv(path: str) -> pd.DataFrame:
    """Load parquet if available, fall back to CSV. Raises FileNotFoundError if neither."""
    parquet_path = path if path.endswith(".parquet") else path + ".parquet"
    csv_path = path if path.endswith(".csv") else path + ".csv"
    if os.path.exists(parquet_path):
        return pd.read_parquet(parquet_path)
    if os.path.exists(csv_path):
        return pd.read_csv(csv_path, parse_dates=["timestamp"])
    raise FileNotFoundError(
        f"No data file found at {parquet_path} or {csv_path}. "
        f"# TODO: Verify data path matches actual disk layout."
    )


def _enforce_utc(df: pd.DataFrame, ts_col: str = "timestamp") -> pd.DataFrame:
    """Ensure timestamp column is UTC-aware. Raises if timezone is wrong."""
    if ts_col not in df.columns:
        raise ValueError(f"Missing '{ts_col}' column in DataFrame")
    col = pd.to_datetime(df[ts_col])
    if col.dt.tz is None:
        col = col.dt.tz_localize("UTC")
    else:
        col = col.dt.tz_convert("UTC")
    df = df.copy()
    df[ts_col] = col
    return df.sort_values(ts_col).reset_index(drop=True)


def _check_history_length(df: pd.DataFrame, asset: str, ts_col: str = "timestamp") -> None:
    """Raise if history is shorter than MIN_HISTORY_DAYS."""
    if df.empty:
        raise ValueError(f"[{asset}] Loaded DataFrame is empty.")
    span = (df[ts_col].max() - df[ts_col].min()).days
    if span < MIN_HISTORY_DAYS:
        raise ValueError(
            f"[{asset}] Insufficient history: {span} days < {MIN_HISTORY_DAYS} days minimum. "
            f"Extend historical data before running backtests."
        )


def _filter_date_range(
    df: pd.DataFrame,
    start: Optional[str],
    end: Optional[str],
    ts_col: str = "timestamp",
) -> pd.DataFrame:
    if start:
        df = df[df[ts_col] >= pd.Timestamp(start, tz="UTC")]
    if end:
        df = df[df[ts_col] <= pd.Timestamp(end, tz="UTC")]
    return df.reset_index(drop=True)


def load_bars(
    asset: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    base_path: str = _BASE_PATH,
    check_min_history: bool = True,
) -> pd.DataFrame:
    """
    Load underlying price bars (10-second granularity by default).

    Returns DataFrame with columns:
        timestamp (UTC), open, high, low, close, volume

    # TODO: Adjust filename 'bars_10s' to match actual disk layout.
    """
    path = _resolve_path(asset, "bars_10s", base_path)
    df = _load_parquet_or_csv(path)
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"[{asset}] bars missing columns: {missing}")
    df = _enforce_utc(df)
    df = _filter_date_range(df, start_date, end_date)
    if check_min_history:
        _check_history_length(df, asset)
    return df


def load_l2_snapshots(
    asset: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    base_path: str = _BASE_PATH,
) -> pd.DataFrame:
    """
    Load L2 book snapshots.

    Returns DataFrame with columns:
        timestamp (UTC), bids (list of [price, size] pairs), asks (list of [price, size] pairs)

    # TODO: Adjust filename 'l2_snapshots' to match actual disk layout.
    """
    path = _resolve_path(asset, "l2_snapshots", base_path)
    df = _load_parquet_or_csv(path)
    required = {"timestamp", "bids", "asks"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"[{asset}] l2_snapshots missing columns: {missing}")
    return _enforce_utc(df)


def load_trades(
    asset: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    base_path: str = _BASE_PATH,
) -> pd.DataFrame:
    """
    Load trade tape.

    Returns DataFrame with columns:
        timestamp (UTC), price, size, aggressor_side ('buy' or 'sell')

    # TODO: Adjust filename 'trades' to match actual disk layout.
    """
    path = _resolve_path(asset, "trades", base_path)
    df = _load_parquet_or_csv(path)
    required = {"timestamp", "price", "size", "aggressor_side"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"[{asset}] trades missing columns: {missing}")
    df = _enforce_utc(df)
    if not df["aggressor_side"].isin(["buy", "sell"]).all():
        bad = df[~df["aggressor_side"].isin(["buy", "sell"])]["aggressor_side"].unique()
        raise ValueError(f"[{asset}] trades has invalid aggressor_side values: {bad}")
    return df


def load_funding(
    asset: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    base_path: str = _BASE_PATH,
) -> pd.DataFrame:
    """
    Load funding rate and open interest data.

    Returns DataFrame with columns:
        timestamp (UTC), funding_rate, open_interest

    # TODO: Adjust filename 'funding' to match actual disk layout.
    """
    path = _resolve_path(asset, "funding", base_path)
    df = _load_parquet_or_csv(path)
    required = {"timestamp", "funding_rate", "open_interest"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"[{asset}] funding missing columns: {missing}")
    return _enforce_utc(df)


def load_kalshi_ticks(
    asset: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    base_path: str = _BASE_PATH,
) -> pd.DataFrame:
    """
    Load Kalshi contract tick data.

    Returns DataFrame with columns:
        timestamp (UTC), yes_bid, yes_ask, no_bid, no_ask,
        seconds_to_expiry (optional)

    If data is not available for a window, the engine skips that window
    rather than interpolating.

    # TODO: Adjust filename 'kalshi_ticks' to match actual disk layout.
    """
    path = _resolve_path(asset, "kalshi_ticks", base_path)
    try:
        df = _load_parquet_or_csv(path)
    except FileNotFoundError:
        # Kalshi contract history may not be available for all assets/periods
        import warnings
        warnings.warn(
            f"[{asset}] No Kalshi tick data found at {path}. "
            f"Windows without tick data will be skipped.",
            stacklevel=2,
        )
        return pd.DataFrame(columns=["timestamp", "yes_bid", "yes_ask", "no_bid", "no_ask"])
    required = {"timestamp", "yes_bid", "yes_ask", "no_bid", "no_ask"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"[{asset}] kalshi_ticks missing columns: {missing}")
    return _enforce_utc(df)
```

- [ ] **Step 2: Write `tests/test_data_loaders.py`**

```python
"""Smoke tests for data loaders using synthetic in-memory data."""
import os
import tempfile
import pandas as pd
import numpy as np
import pytest
from backtesting.data.loaders import (
    _enforce_utc, _check_history_length, _filter_date_range, load_bars
)


def _make_bars_df(n_days=200):
    """Synthetic 10s bars spanning n_days."""
    ts = pd.date_range("2025-01-01", periods=n_days * 6 * 24, freq="10s", tz="UTC")
    rng = np.random.default_rng(0)
    prices = 50000 + rng.normal(0, 100, len(ts)).cumsum()
    return pd.DataFrame({
        "timestamp": ts,
        "open": prices,
        "high": prices + rng.uniform(0, 10, len(ts)),
        "low": prices - rng.uniform(0, 10, len(ts)),
        "close": prices + rng.normal(0, 5, len(ts)),
        "volume": rng.uniform(0.1, 5.0, len(ts)),
    })


def test_enforce_utc_naive():
    df = pd.DataFrame({"timestamp": ["2025-01-01 00:00:00", "2025-01-02 00:00:00"]})
    out = _enforce_utc(df)
    assert out["timestamp"].dt.tz is not None
    assert str(out["timestamp"].dt.tz) == "UTC"


def test_enforce_utc_already_utc():
    ts = pd.date_range("2025-01-01", periods=3, freq="h", tz="UTC")
    df = pd.DataFrame({"timestamp": ts})
    out = _enforce_utc(df)
    assert str(out["timestamp"].dt.tz) == "UTC"


def test_check_history_length_passes():
    df = _make_bars_df(200)
    _check_history_length(df, "BTC")  # should not raise


def test_check_history_length_fails():
    df = _make_bars_df(30)  # only 30 days
    with pytest.raises(ValueError, match="Insufficient history"):
        _check_history_length(df, "BTC")


def test_filter_date_range():
    df = _make_bars_df(200)
    df = _enforce_utc(df)
    out = _filter_date_range(df, "2025-03-01", "2025-04-01")
    assert out["timestamp"].min() >= pd.Timestamp("2025-03-01", tz="UTC")
    assert out["timestamp"].max() <= pd.Timestamp("2025-04-01", tz="UTC")


def test_load_bars_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_bars("FAKE_ASSET", base_path="/nonexistent/path", check_min_history=False)
```

- [ ] **Step 3: Run tests**

```
python -m pytest tests/test_data_loaders.py -v
```
Expected: 6 tests pass (load_bars file-not-found test passes since no data on disk).

- [ ] **Step 4: Commit**

```bash
git add backtesting/data/loaders.py tests/test_data_loaders.py
git commit -m "feat: add data loaders with UTC enforcement and min-history guard"
```

---

### Task 3: Label builder (`backtesting/data/label_builder.py`)

**Files:**
- Create: `backtesting/data/label_builder.py`
- Create: `tests/test_label_builder.py`

- [ ] **Step 1: Write `backtesting/data/label_builder.py`**

```python
"""
Label builder for Kalshi 15-minute binary markets.

y = 1 if reference_price_close > reference_price_open  (underlying CLOSE > OPEN at t+15min)
y = 0 otherwise

Uses log returns internally; labels use the same reference price source
(spot or perp) specified in the per-asset config.

Missing data in the label window: window is DROPPED, not imputed.
"""
from __future__ import annotations
import pandas as pd
import numpy as np
from typing import Optional


WINDOW_MINUTES = 15


def build_labels(
    bars: pd.DataFrame,
    reference_source: str = "spot",  # "spot" or "perp" - from per-asset config
    window_minutes: int = WINDOW_MINUTES,
    drop_incomplete: bool = True,
) -> pd.DataFrame:
    """
    Build binary labels from 10-second OHLCV bars.

    Args:
        bars: DataFrame with columns [timestamp, open, high, low, close, volume].
              Timestamp must be UTC-aware.
        reference_source: "spot" or "perp" - informational only; caller ensures
                          the correct bars were loaded.
        window_minutes: length of each binary window (default 15).
        drop_incomplete: if True, drop windows with any missing data.

    Returns:
        DataFrame with columns:
            timestamp (window open time, UTC),
            label (0 or 1),
            reference_price_open,
            reference_price_close,
            log_return (ln(close/open))
    """
    if bars.empty:
        return pd.DataFrame(columns=[
            "timestamp", "label", "reference_price_open",
            "reference_price_close", "log_return"
        ])

    bars = bars.copy().sort_values("timestamp")
    _validate_bars(bars)

    # Resample to window_minutes-minute buckets using the OPEN price of the first
    # 10s bar and CLOSE price of the last 10s bar within each window.
    bars = bars.set_index("timestamp")
    freq = f"{window_minutes}min"

    open_prices  = bars["open"].resample(freq, label="left", closed="left").first()
    close_prices = bars["close"].resample(freq, label="left", closed="left").last()
    bar_counts   = bars["close"].resample(freq, label="left", closed="left").count()

    # Expected bar count per window (10s bars in window_minutes minutes)
    expected_bars = window_minutes * 60 // 10

    result = pd.DataFrame({
        "timestamp":             open_prices.index,
        "reference_price_open":  open_prices.values,
        "reference_price_close": close_prices.values,
        "bar_count":             bar_counts.values,
    })

    if drop_incomplete:
        result = result[result["bar_count"] == expected_bars].copy()

    result = result[result["reference_price_open"] > 0].copy()
    result = result[result["reference_price_close"] > 0].copy()

    result["log_return"] = np.log(
        result["reference_price_close"] / result["reference_price_open"]
    )
    result["label"] = (result["reference_price_close"] > result["reference_price_open"]).astype(int)

    return result[["timestamp", "label", "reference_price_open",
                   "reference_price_close", "log_return"]].reset_index(drop=True)


def _validate_bars(bars: pd.DataFrame) -> None:
    required = {"timestamp", "open", "close"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(f"bars missing required columns: {missing}")
    if bars["timestamp"].dt.tz is None:
        raise ValueError("bars['timestamp'] must be UTC-aware. Call _enforce_utc first.")


def align_labels_to_signals(
    labels: pd.DataFrame,
    signal_times: pd.DatetimeIndex,
) -> pd.DataFrame:
    """
    Left-join labels onto signal_times so each signal window has its label.
    Signals without a label in the window are dropped.
    """
    labels = labels.copy()
    labels = labels.set_index("timestamp")
    result = labels.reindex(signal_times).dropna(subset=["label"])
    result["label"] = result["label"].astype(int)
    return result.reset_index().rename(columns={"index": "timestamp"})
```

- [ ] **Step 2: Write `tests/test_label_builder.py`**

```python
import pandas as pd
import numpy as np
import pytest
from backtesting.data.label_builder import build_labels


def _make_bars(n_windows=50, bars_per_window=90, start_price=50000.0, seed=42):
    """90 bars per 15-min window (10s × 90 = 900s = 15min)."""
    rng = np.random.default_rng(seed)
    total = n_windows * bars_per_window
    ts = pd.date_range("2025-01-01 00:00:00", periods=total, freq="10s", tz="UTC")
    prices = start_price + rng.normal(0, 50, total).cumsum()
    prices = np.clip(prices, 1000, None)
    return pd.DataFrame({
        "timestamp": ts,
        "open": prices,
        "high": prices + rng.uniform(0, 10, total),
        "low":  prices - rng.uniform(0, 10, total),
        "close": prices + rng.normal(0, 20, total),
        "volume": rng.uniform(0.1, 5.0, total),
    })


def test_output_columns():
    bars = _make_bars()
    out = build_labels(bars)
    assert set(out.columns) == {"timestamp", "label", "reference_price_open",
                                "reference_price_close", "log_return"}


def test_label_binary():
    bars = _make_bars()
    out = build_labels(bars)
    assert out["label"].isin([0, 1]).all()


def test_label_matches_price_comparison():
    bars = _make_bars()
    out = build_labels(bars)
    expected = (out["reference_price_close"] > out["reference_price_open"]).astype(int)
    pd.testing.assert_series_equal(out["label"], expected, check_names=False)


def test_log_return_sign_matches_label():
    bars = _make_bars()
    out = build_labels(bars)
    # label=1 → log_return > 0; label=0 → log_return <= 0
    assert (out.loc[out["label"] == 1, "log_return"] > 0).all()
    assert (out.loc[out["label"] == 0, "log_return"] <= 0).all()


def test_incomplete_window_dropped():
    bars = _make_bars(n_windows=5)
    # Remove 5 bars from the first window to make it incomplete
    bars = bars.iloc[5:].copy()
    out = build_labels(bars, drop_incomplete=True)
    # First window should be dropped
    assert len(out) < 5


def test_empty_bars_returns_empty():
    empty = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    empty["timestamp"] = pd.to_datetime(empty["timestamp"]).dt.tz_localize("UTC")
    out = build_labels(empty)
    assert out.empty


def test_timestamps_are_utc():
    bars = _make_bars()
    out = build_labels(bars)
    assert out["timestamp"].dt.tz is not None
    assert str(out["timestamp"].dt.tz) == "UTC"
```

- [ ] **Step 3: Run tests**

```
python -m pytest tests/test_label_builder.py -v
```
Expected: 7 tests pass.

- [ ] **Step 4: Commit**

```bash
git add backtesting/data/label_builder.py tests/test_label_builder.py
git commit -m "feat: add label builder for 15-min binary windows"
```

---

### Task 4: Data aligner (`backtesting/data/aligner.py`)

**Files:**
- Create: `backtesting/data/aligner.py`
- Create: `tests/test_data_alignment.py`

- [ ] **Step 1: Write `backtesting/data/aligner.py`**

```python
"""
Event stream aligner for the backtesting engine.

Produces a sorted, typed stream of timestamped events from multiple input
DataFrames. The engine consumes one event at a time; the aligner ensures
each event only sees data strictly before its timestamp.
"""
from __future__ import annotations
import pandas as pd
from dataclasses import dataclass, field
from typing import Iterator, Literal


EventType = Literal["bar", "l2", "trade", "funding", "kalshi_tick", "label"]


@dataclass(order=True)
class Event:
    timestamp: pd.Timestamp
    event_type: EventType = field(compare=False)
    payload: dict = field(compare=False)


def _validate_utc(df: pd.DataFrame, name: str, ts_col: str = "timestamp") -> None:
    if df.empty:
        return
    if df[ts_col].dt.tz is None:
        raise ValueError(f"[{name}] timestamps must be UTC-aware. Found tz-naive.")
    if str(df[ts_col].dt.tz) != "UTC":
        raise ValueError(f"[{name}] timestamps must be UTC, found {df[ts_col].dt.tz}.")


def build_event_stream(
    bars: pd.DataFrame,
    labels: pd.DataFrame,
    l2_snapshots: pd.DataFrame | None = None,
    trades: pd.DataFrame | None = None,
    funding: pd.DataFrame | None = None,
    kalshi_ticks: pd.DataFrame | None = None,
) -> list[Event]:
    """
    Merge all input DataFrames into a single sorted event list.

    Every DataFrame must have a UTC-aware 'timestamp' column.
    Returns events sorted by timestamp ascending.
    No silent timezone drift - validates UTC for every input.
    """
    events: list[Event] = []

    for name, df, etype, cols in [
        ("bars",         bars,         "bar",         None),
        ("labels",       labels,       "label",       None),
        ("l2_snapshots", l2_snapshots, "l2",          None),
        ("trades",       trades,       "trade",       None),
        ("funding",      funding,      "funding",     None),
        ("kalshi_ticks", kalshi_ticks, "kalshi_tick", None),
    ]:
        if df is None or df.empty:
            continue
        _validate_utc(df, name)
        for row in df.itertuples(index=False):
            ts = row.timestamp if isinstance(row.timestamp, pd.Timestamp) else pd.Timestamp(row.timestamp)
            payload = {k: getattr(row, k) for k in df.columns if k != "timestamp"}
            events.append(Event(timestamp=ts, event_type=etype, payload=payload))

    events.sort(key=lambda e: e.timestamp)
    return events


def iter_windows(
    events: list[Event],
    label_timestamps: pd.DatetimeIndex,
) -> Iterator[tuple[pd.Timestamp, list[Event]]]:
    """
    Yield (window_open_time, events_strictly_before_window_open) for each label.

    The engine gets only events with timestamp < window_open to prevent look-ahead.
    """
    events_seen: list[Event] = []
    event_idx = 0
    n = len(events)

    for window_ts in sorted(label_timestamps):
        # Advance the pointer to include all events strictly before window_ts
        while event_idx < n and events[event_idx].timestamp < window_ts:
            events_seen.append(events[event_idx])
            event_idx += 1
        yield window_ts, list(events_seen)
```

- [ ] **Step 2: Write `tests/test_data_alignment.py`**

```python
import pandas as pd
import pytest
from backtesting.data.aligner import build_event_stream, iter_windows, Event


def _ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC")


def _df(timestamps, **cols):
    d = {"timestamp": [_ts(t) for t in timestamps]}
    d.update(cols)
    return pd.DataFrame(d)


def test_event_stream_sorted():
    bars = _df(["2025-01-01 00:10:00", "2025-01-01 00:00:00"], open=[1.0, 2.0], close=[1.1, 2.1],
               high=[1.2, 2.2], low=[0.9, 1.9], volume=[10.0, 20.0])
    labels = _df(["2025-01-01 00:15:00"], label=[1], reference_price_open=[50000.0],
                 reference_price_close=[50100.0], log_return=[0.002])
    events = build_event_stream(bars, labels)
    timestamps = [e.timestamp for e in events]
    assert timestamps == sorted(timestamps)


def test_event_types_present():
    bars = _df(["2025-01-01 00:00:00"], open=[1.0], close=[1.1], high=[1.2], low=[0.9], volume=[5.0])
    labels = _df(["2025-01-01 00:15:00"], label=[1], reference_price_open=[50000.0],
                 reference_price_close=[50100.0], log_return=[0.002])
    events = build_event_stream(bars, labels)
    types = {e.event_type for e in events}
    assert "bar" in types
    assert "label" in types


def test_timezone_naive_raises():
    bars_naive = pd.DataFrame({
        "timestamp": pd.to_datetime(["2025-01-01 00:00:00"]),  # tz-naive
        "open": [1.0], "close": [1.1], "high": [1.2], "low": [0.9], "volume": [5.0]
    })
    labels = _df(["2025-01-01 00:15:00"], label=[1], reference_price_open=[1.0],
                 reference_price_close=[1.1], log_return=[0.001])
    with pytest.raises(ValueError, match="UTC-aware"):
        build_event_stream(bars_naive, labels)


def test_iter_windows_no_lookahead():
    bars = _df([
        "2025-01-01 00:00:00",
        "2025-01-01 00:05:00",
        "2025-01-01 00:10:00",
        "2025-01-01 00:20:00",  # after second window
    ], open=[1.0]*4, close=[1.1]*4, high=[1.2]*4, low=[0.9]*4, volume=[5.0]*4)
    labels = _df([
        "2025-01-01 00:15:00",
        "2025-01-01 00:30:00",
    ], label=[1, 0], reference_price_open=[1.0, 1.1],
       reference_price_close=[1.1, 1.05], log_return=[0.001, -0.001])
    events = build_event_stream(bars, labels)
    label_index = pd.DatetimeIndex([_ts("2025-01-01 00:15:00"), _ts("2025-01-01 00:30:00")])
    windows = list(iter_windows(events, label_index))

    # First window at 00:15: should see bars at 00:00 and 00:05 and 00:10 only
    ts1, evts1 = windows[0]
    bar_times = [e.timestamp for e in evts1 if e.event_type == "bar"]
    assert all(t < ts1 for t in bar_times)

    # Second window at 00:30: should also see the bar at 00:20
    ts2, evts2 = windows[1]
    bar_times2 = [e.timestamp for e in evts2 if e.event_type == "bar"]
    assert all(t < ts2 for t in bar_times2)
    # 00:20 bar should appear in second window's history
    assert _ts("2025-01-01 00:20:00") in bar_times2


def test_none_inputs_skipped():
    bars = _df(["2025-01-01 00:00:00"], open=[1.0], close=[1.1], high=[1.2], low=[0.9], volume=[5.0])
    labels = _df(["2025-01-01 00:15:00"], label=[1], reference_price_open=[1.0],
                 reference_price_close=[1.1], log_return=[0.001])
    events = build_event_stream(bars, labels, l2_snapshots=None, trades=None)
    types = {e.event_type for e in events}
    assert "l2" not in types
    assert "trade" not in types
```

- [ ] **Step 3: Run tests**

```
python -m pytest tests/test_data_alignment.py -v
```
Expected: 5 tests pass.

- [ ] **Step 4: Commit**

```bash
git add backtesting/data/aligner.py tests/test_data_alignment.py
git commit -m "feat: add event-stream aligner with no-lookahead window iteration"
```

---

### Task 5: HAR-RS-J fitter (`backtesting/training/har_fitter.py`)

**Files:**
- Create: `backtesting/training/har_fitter.py`

- [ ] **Step 1: Write `backtesting/training/har_fitter.py`**

```python
"""
HAR-RS-J coefficient fitter for the backtesting training pipeline.

Fits HAR-RS-J coefficients using OLS on a rolling training window.
Writes results to a sidecar file: strategies/strategy_a/config/{asset}.fitted.yaml
Does NOT modify the original config under strategies/.

Reference: Patton & Sheppard (2015) - separate RV+/RV- coefficients for crypto
(RV+ predicts future variance more strongly than RV- in crypto, unlike equities).
"""
from __future__ import annotations
import os
import numpy as np
import pandas as pd
import yaml
from typing import Optional


_FITTED_CONFIG_DIR = os.path.join("strategies", "strategy_a", "config")
TIMESCALES_MINUTES = [15, 60, 240]


def _bipower_variation(log_returns: np.ndarray) -> float:
    """BV = (π/2) · Σ|r[1:]||r[:-1]|"""
    if len(log_returns) < 2:
        return 0.0
    return float((np.pi / 2.0) * np.sum(np.abs(log_returns[1:]) * np.abs(log_returns[:-1])))


def _rv_components(log_returns: np.ndarray) -> dict[str, float]:
    """Compute RV, RV+, RV-, BV, jump, signed_jump."""
    rv = float(np.sum(log_returns ** 2))
    rv_pos = float(np.sum(log_returns[log_returns > 0] ** 2))
    rv_neg = float(np.sum(log_returns[log_returns < 0] ** 2))
    bv = _bipower_variation(log_returns)
    jump = max(rv - bv, 0.0)
    signed_jump = rv_pos - rv_neg
    return {
        "rv": rv, "rv_pos": rv_pos, "rv_neg": rv_neg,
        "bv": bv, "jump": jump, "signed_jump": signed_jump,
    }


def _bars_in_window(granularity_seconds: int, window_minutes: int) -> int:
    return window_minutes * 60 // granularity_seconds


def build_har_features(
    log_returns: np.ndarray,
    granularity_seconds: int = 10,
    timescales_minutes: list[int] = TIMESCALES_MINUTES,
) -> pd.DataFrame:
    """
    Compute rolling HAR-RS-J feature matrix from a time series of log returns.

    Returns DataFrame where each row is a 15-min window, columns are:
        rv_{t}_pos, rv_{t}_neg for each timescale t, plus jump_{15m}
    The target column is rv_15m (next window's realized variance).
    """
    rows = []
    step = _bars_in_window(granularity_seconds, timescales_minutes[0])  # 15-min step

    max_lookback = _bars_in_window(granularity_seconds, max(timescales_minutes))

    for i in range(max_lookback, len(log_returns) - step + 1, step):
        window_log_rets = log_returns[i:i + step]
        target_rv = float(np.sum(window_log_rets ** 2))

        row: dict[str, float] = {"rv_target": target_rv}
        for t_min in timescales_minutes:
            n_bars = _bars_in_window(granularity_seconds, t_min)
            hist_rets = log_returns[i - n_bars:i]
            comps = _rv_components(hist_rets)
            label = f"rv_{t_min}m"
            row[f"{label}_pos"] = comps["rv_pos"]
            row[f"{label}_neg"] = comps["rv_neg"]
        # Jump from the shortest timescale
        short_n = _bars_in_window(granularity_seconds, timescales_minutes[0])
        short_rets = log_returns[i - short_n:i]
        row["jump_15m"] = _rv_components(short_rets)["jump"]
        rows.append(row)

    return pd.DataFrame(rows)


def fit_har_rsj(
    log_returns: np.ndarray,
    granularity_seconds: int = 10,
    timescales_minutes: list[int] = TIMESCALES_MINUTES,
) -> dict[str, float | None]:
    """
    Fit HAR-RS-J coefficients via OLS.

    Returns dict with keys:
        const, rv_15m_pos, rv_15m_neg, rv_1h_pos, rv_1h_neg,
        rv_4h_pos, rv_4h_neg, jump
    Returns all-None if insufficient data.
    """
    feat_df = build_har_features(log_returns, granularity_seconds, timescales_minutes)
    if len(feat_df) < 30:
        return {
            "const": None, "rv_15m_pos": None, "rv_15m_neg": None,
            "rv_1h_pos": None, "rv_1h_neg": None,
            "rv_4h_pos": None, "rv_4h_neg": None, "jump": None,
        }

    y = feat_df["rv_target"].values
    feature_cols = [c for c in feat_df.columns if c != "rv_target"]
    X_raw = feat_df[feature_cols].values
    X = np.column_stack([np.ones(len(X_raw)), X_raw])

    # OLS: β = (X'X)^-1 X'y
    # TODO: swap to WLS or Huber regression by replacing this block
    try:
        coeffs, residuals, rank, sv = np.linalg.lstsq(X, y, rcond=None)
    except np.linalg.LinAlgError:
        return {k: None for k in ["const", "rv_15m_pos", "rv_15m_neg",
                                   "rv_1h_pos", "rv_1h_neg", "rv_4h_pos",
                                   "rv_4h_neg", "jump"]}

    t_labels = [f"rv_{t}m" for t in timescales_minutes]
    names = (
        ["const"]
        + [f"{lbl}_pos" for lbl in t_labels]
        + [f"{lbl}_neg" for lbl in t_labels]
        + ["jump"]
    )
    # Reorder to match expected output schema
    coeff_map: dict[str, float] = {}
    for name, val in zip(feature_cols, coeffs[1:]):
        coeff_map[name] = float(val)
    coeff_map["const"] = float(coeffs[0])

    return {
        "const":      coeff_map.get("const"),
        "rv_15m_pos": coeff_map.get("rv_15m_pos"),
        "rv_15m_neg": coeff_map.get("rv_15m_neg"),
        "rv_1h_pos":  coeff_map.get("rv_1h_pos"),
        "rv_1h_neg":  coeff_map.get("rv_1h_neg"),
        "rv_4h_pos":  coeff_map.get("rv_4h_pos"),
        "rv_4h_neg":  coeff_map.get("rv_4h_neg"),
        "jump":       coeff_map.get("jump_15m"),
    }


def write_fitted_config(
    asset: str,
    coefficients: dict[str, float | None],
    extra_meta: dict | None = None,
    output_dir: str = _FITTED_CONFIG_DIR,
    suffix: str = ".fitted.yaml",
) -> str:
    """
    Write fitted HAR-RS-J coefficients to a sidecar YAML file.
    Does NOT touch the original strategies/strategy_a/config/{asset}.yaml.

    Returns the path written.
    """
    out_path = os.path.join(output_dir, f"{asset.lower()}{suffix}")
    data = {
        "asset": asset.upper(),
        "fitted_by": "backtesting.training.har_fitter",
        "har_rs_j": {"coefficients": {k: (float(v) if v is not None else None)
                                       for k, v in coefficients.items()}},
    }
    if extra_meta:
        data["meta"] = extra_meta
    os.makedirs(output_dir, exist_ok=True)
    with open(out_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False)
    return out_path
```

- [ ] **Step 2: Commit**

```bash
git add backtesting/training/har_fitter.py
git commit -m "feat: add HAR-RS-J OLS fitter and sidecar config writer"
```

---

### Task 6: Model fitter (`backtesting/training/model_fitter.py`) and pipeline (`backtesting/training/pipeline.py`)

**Files:**
- Create: `backtesting/training/model_fitter.py`
- Create: `backtesting/training/pipeline.py`

- [ ] **Step 1: Write `backtesting/training/model_fitter.py`**

```python
"""
Strategy A model fitter.

Fits a calibrated classifier (logistic regression / XGBoost / LightGBM)
and saves weights and calibrator to disk for later loading.

The StrategyAModel.load() interface expects:
    config["model"]["weights_path"]    -> path to pickled feature_names list
    config["model"]["calibrator_path"] -> path to pickled calibrated model
"""
from __future__ import annotations
import os
import pickle
import numpy as np
import pandas as pd
import yaml
from typing import Optional


def fit_and_save(
    X: np.ndarray,
    y: np.ndarray,
    feature_names: list[str],
    asset: str,
    model_config: dict,
    fees_config: dict,
    output_dir: str = "backtesting/output/models",
    refit: bool = True,
) -> tuple[str, str]:
    """
    Fit a calibrated classifier and save to disk.

    Returns:
        (weights_path, calibrator_path) - paths to the saved artifacts.

    weights_path:    pickle of feature_names list
    calibrator_path: pickle of the fitted CalibratedClassifierCV object
    """
    os.makedirs(output_dir, exist_ok=True)
    weights_path    = os.path.join(output_dir, f"{asset.lower()}_feature_names.pkl")
    calibrator_path = os.path.join(output_dir, f"{asset.lower()}_calibrated_model.pkl")

    if not refit and os.path.exists(weights_path) and os.path.exists(calibrator_path):
        return weights_path, calibrator_path

    from strategies.strategy_a.model import StrategyAModel  # strategies/ is read-only
    model = StrategyAModel(model_config, fees_config)
    model.fit(X, y, feature_names)

    with open(weights_path, "wb") as f:
        pickle.dump(model._feature_names, f)
    with open(calibrator_path, "wb") as f:
        pickle.dump(model._model, f)

    return weights_path, calibrator_path


def load_model(
    asset: str,
    model_config: dict,
    fees_config: dict,
    output_dir: str = "backtesting/output/models",
):
    """Load a pre-fitted StrategyAModel from disk."""
    import sys
    sys.path.insert(0, "strategies")  # ensure strategies/ is importable
    from strategies.strategy_a.model import StrategyAModel  # noqa

    weights_path    = model_config.get("weights_path") or os.path.join(output_dir, f"{asset.lower()}_feature_names.pkl")
    calibrator_path = model_config.get("calibrator_path") or os.path.join(output_dir, f"{asset.lower()}_calibrated_model.pkl")

    model = StrategyAModel(model_config, fees_config)
    model.load(weights_path=weights_path, calibrator_path=calibrator_path)
    return model
```

- [ ] **Step 2: Write `backtesting/training/pipeline.py`**

```python
"""
Training pipeline orchestration.

Runs sequentially: BTC first (ETH/SOL/XRP cross-asset features depend on it),
then ETH, SOL, XRP. Never runs in parallel.

Entry point:
    from backtesting.training.pipeline import run_training_pipeline
    run_training_pipeline(global_config, assets=["btc", "eth", "sol", "xrp"])
"""
from __future__ import annotations
import os
import sys
import logging
import numpy as np
import pandas as pd
import yaml
from typing import Optional

logger = logging.getLogger(__name__)
_ASSET_ORDER = ["btc", "eth", "sol", "xrp"]  # BTC must come first


def _load_yaml(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _build_feature_matrix(
    bars: pd.DataFrame,
    asset: str,
    asset_config: dict,
    global_config: dict,
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """
    Build (X, y, feature_names) from raw bar data.

    Features: only HAR-RS-J variance forecast for now.
    # TODO: integrate order_flow, time_of_day, cross_asset, funding features
    # once data loaders for those sources are confirmed.
    """
    from backtesting.training.har_fitter import build_har_features, fit_har_rsj
    from backtesting.data.label_builder import build_labels

    log_rets = np.log(bars["close"] / bars["open"]).values
    feat_df = build_har_features(log_rets)
    if feat_df.empty or len(feat_df) < 50:
        raise ValueError(f"[{asset}] Insufficient feature rows: {len(feat_df)}")

    labels_df = build_labels(bars)
    if labels_df.empty:
        raise ValueError(f"[{asset}] No labels built from bars.")

    # Align feature rows to label timestamps (approximate - features lead labels by 1 window)
    n = min(len(feat_df), len(labels_df))
    feat_df = feat_df.iloc[-n:]
    labels_df = labels_df.iloc[-n:]

    feature_cols = [c for c in feat_df.columns if c != "rv_target"]
    X = feat_df[feature_cols].values
    y = labels_df["label"].values
    return X.astype(float), y.astype(int), feature_cols


def run_training_pipeline(
    global_config: dict,
    assets: Optional[list[str]] = None,
    per_asset_config_dir: str = "backtesting/configs/per_asset",
    fees_config_path: str = "strategies/shared/fees.yaml",
) -> dict[str, dict]:
    """
    Train models for all assets sequentially. Returns a dict of fit summaries.

    Args:
        global_config: contents of backtesting/configs/backtest.yaml
        assets: list of asset names to train; default is all 4 in BTC-first order
    """
    sys.path.insert(0, "strategies")  # make strategies/ importable

    if assets is None:
        assets = _ASSET_ORDER
    else:
        # Ensure BTC-first ordering
        assets = sorted(assets, key=lambda a: _ASSET_ORDER.index(a.lower()) if a.lower() in _ASSET_ORDER else 99)

    fees_config = _load_yaml(fees_config_path)
    train_cfg = global_config.get("training", {})
    output_dir = train_cfg.get("output_dir", "backtesting/output/models")
    refit = train_cfg.get("refit", True)

    summaries: dict[str, dict] = {}

    for asset in assets:
        logger.info(f"Training asset: {asset.upper()}")
        per_asset_cfg_path = os.path.join(per_asset_config_dir, f"{asset.lower()}.yaml")
        asset_config = _load_yaml(per_asset_cfg_path)

        # Load bars - skip if no data
        try:
            from backtesting.data.loaders import load_bars
            data_cfg = global_config.get("data", {})
            bars = load_bars(
                asset=asset,
                start_date=data_cfg.get("start_date"),
                end_date=data_cfg.get("end_date"),
                check_min_history=True,
            )
        except (FileNotFoundError, ValueError) as exc:
            logger.warning(f"[{asset}] Skipping training: {exc}")
            summaries[asset] = {"status": "skipped", "reason": str(exc)}
            continue

        # Build feature matrix
        try:
            X, y, feature_names = _build_feature_matrix(bars, asset, asset_config, global_config)
        except ValueError as exc:
            logger.warning(f"[{asset}] Feature build failed: {exc}")
            summaries[asset] = {"status": "skipped", "reason": str(exc)}
            continue

        # Model config from strategy_a/config/{asset}.yaml
        strategy_cfg_path = os.path.join("strategies", "strategy_a", "config", f"{asset.lower()}.yaml")
        if not os.path.exists(strategy_cfg_path):
            logger.warning(f"[{asset}] Strategy config not found: {strategy_cfg_path}")
            summaries[asset] = {"status": "skipped", "reason": f"missing {strategy_cfg_path}"}
            continue
        model_config = _load_yaml(strategy_cfg_path)

        from backtesting.training.model_fitter import fit_and_save
        try:
            wp, cp = fit_and_save(
                X=X, y=y, feature_names=feature_names,
                asset=asset, model_config=model_config, fees_config=fees_config,
                output_dir=output_dir, refit=refit,
            )
        except Exception as exc:
            logger.error(f"[{asset}] Fit failed: {exc}")
            summaries[asset] = {"status": "error", "reason": str(exc)}
            continue

        # HAR fitting for sidecar config
        from backtesting.training.har_fitter import fit_har_rsj, write_fitted_config
        log_rets = np.log(bars["close"] / bars["open"]).values
        coefficients = fit_har_rsj(log_rets)
        fitted_path = write_fitted_config(
            asset=asset,
            coefficients=coefficients,
            extra_meta={"n_training_bars": len(log_rets), "n_feature_rows": len(X)},
            suffix=train_cfg.get("fitted_config_suffix", ".fitted.yaml"),
        )

        summaries[asset] = {
            "status": "ok",
            "n_samples": len(X),
            "feature_count": len(feature_names),
            "weights_path": wp,
            "calibrator_path": cp,
            "fitted_config_path": fitted_path,
            "label_balance": float(y.mean()),
        }
        logger.info(f"[{asset}] Training complete: {summaries[asset]}")

    return summaries
```

- [ ] **Step 3: Commit**

```bash
git add backtesting/training/model_fitter.py backtesting/training/pipeline.py
git commit -m "feat: add model fitter and sequential training pipeline"
```

---

### Task 7: Look-ahead checker (`backtesting/validation/lookahead_checker.py`) + CPCV (`backtesting/validation/cpcv.py`)

**Files:**
- Create: `backtesting/validation/lookahead_checker.py`
- Create: `backtesting/validation/cpcv.py`
- Create: `tests/test_lookahead_checker.py`
- Create: `tests/test_cpcv.py`

- [ ] **Step 1: Write `backtesting/validation/lookahead_checker.py`**

```python
"""
Programmatic look-ahead verification.

For every feature value computed at time t, all source data timestamps
must be strictly less than t. Fails the run if any feature violates this.
"""
from __future__ import annotations
import pandas as pd
from dataclasses import dataclass


@dataclass
class LookaheadViolation:
    feature_name: str
    decision_timestamp: pd.Timestamp
    source_timestamp: pd.Timestamp
    delta_seconds: float


def check_no_lookahead(
    decisions: list[dict],
) -> list[LookaheadViolation]:
    """
    Check that every feature in each decision record was computed from
    data strictly before the decision timestamp.

    Each decision dict must have:
        "timestamp": pd.Timestamp - the decision time
        "feature_timestamps": dict[str, pd.Timestamp] - feature name → source data timestamp

    Returns a list of violations. Empty list means pass.
    """
    violations = []
    for decision in decisions:
        decision_ts = decision.get("timestamp")
        feature_timestamps = decision.get("feature_timestamps", {})
        if decision_ts is None:
            continue
        for feat_name, src_ts in feature_timestamps.items():
            if src_ts >= decision_ts:
                violations.append(LookaheadViolation(
                    feature_name=feat_name,
                    decision_timestamp=decision_ts,
                    source_timestamp=src_ts,
                    delta_seconds=float((src_ts - decision_ts).total_seconds()),
                ))
    return violations


def assert_no_lookahead(decisions: list[dict]) -> None:
    """Raises RuntimeError if any look-ahead is detected."""
    violations = check_no_lookahead(decisions)
    if violations:
        msgs = [
            f"  {v.feature_name}: source={v.source_timestamp} >= decision={v.decision_timestamp} "
            f"(+{v.delta_seconds:.1f}s)"
            for v in violations[:10]
        ]
        raise RuntimeError(
            f"LOOK-AHEAD DETECTED in {len(violations)} feature(s):\n" + "\n".join(msgs)
        )
```

- [ ] **Step 2: Write `tests/test_lookahead_checker.py`**

```python
import pandas as pd
import pytest
from backtesting.validation.lookahead_checker import check_no_lookahead, assert_no_lookahead


def _ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC")


def test_clean_features_no_violations():
    decisions = [{
        "timestamp": _ts("2025-01-01 01:00:00"),
        "feature_timestamps": {
            "har_rv": _ts("2025-01-01 00:59:50"),
            "ofi": _ts("2025-01-01 00:59:55"),
        }
    }]
    assert check_no_lookahead(decisions) == []


def test_future_feature_detected():
    decisions = [{
        "timestamp": _ts("2025-01-01 01:00:00"),
        "feature_timestamps": {
            "bad_feature": _ts("2025-01-01 01:00:01"),  # 1 second in the future
        }
    }]
    violations = check_no_lookahead(decisions)
    assert len(violations) == 1
    assert violations[0].feature_name == "bad_feature"
    assert violations[0].delta_seconds == 1.0


def test_same_timestamp_is_violation():
    """t_source == t_decision is still a look-ahead (must be STRICTLY before)."""
    decisions = [{
        "timestamp": _ts("2025-01-01 01:00:00"),
        "feature_timestamps": {
            "exact_match": _ts("2025-01-01 01:00:00"),
        }
    }]
    violations = check_no_lookahead(decisions)
    assert len(violations) == 1


def test_assert_raises_on_violation():
    decisions = [{
        "timestamp": _ts("2025-01-01 01:00:00"),
        "feature_timestamps": {
            "future_data": _ts("2025-01-01 01:05:00"),
        }
    }]
    with pytest.raises(RuntimeError, match="LOOK-AHEAD DETECTED"):
        assert_no_lookahead(decisions)


def test_empty_decisions_passes():
    assert check_no_lookahead([]) == []
```

- [ ] **Step 3: Write `backtesting/validation/cpcv.py`**

```python
"""
Combinatorial Purged Cross-Validation (CPCV).

Reference: Bailey & Lopez de Prado (2018) - "The Probability of Backtest Overfitting."

Parameters:
    N: number of groups to split data into (default 6)
    k: number of groups used as test in each combination (default 2)
    → produces C(N, k) = C(6, 2) = 15 splits
    → produces (k · C(N,k)) / N = (2 · 15) / 6 = 5 distinct backtest paths

Purging:
    Remove any training observation whose label window overlaps the test window.
    Label horizon = 15 minutes.

Embargo:
    Drop a configurable number of minutes of observations after each test fold
    before training resumes (default 30 min, must be ≥ label horizon 15 min).
"""
from __future__ import annotations
import math
from itertools import combinations
from typing import Iterator
import numpy as np
import pandas as pd


LABEL_HORIZON_MINUTES = 15


def _n_combinations(n: int, k: int) -> int:
    return math.comb(n, k)


def _n_paths(n: int, k: int) -> int:
    """Number of distinct backtest paths = (k · C(N,k)) / N."""
    return (k * math.comb(n, k)) // n


def split_into_groups(
    timestamps: pd.DatetimeIndex,
    n_groups: int,
) -> list[pd.DatetimeIndex]:
    """Split a sorted timestamp index into N approximately equal groups."""
    splits = np.array_split(np.arange(len(timestamps)), n_groups)
    return [timestamps[s] for s in splits]


def get_cpcv_splits(
    timestamps: pd.DatetimeIndex,
    n_groups: int = 6,
    k_test_groups: int = 2,
    embargo_minutes: int = 30,
    label_horizon_minutes: int = LABEL_HORIZON_MINUTES,
) -> Iterator[tuple[pd.DatetimeIndex, pd.DatetimeIndex]]:
    """
    Yield (train_timestamps, test_timestamps) for each CPCV split.

    Purging and embargo are applied to training timestamps.
    Total splits = C(n_groups, k_test_groups).
    """
    if embargo_minutes < label_horizon_minutes:
        raise ValueError(
            f"embargo_minutes ({embargo_minutes}) must be >= label_horizon_minutes "
            f"({label_horizon_minutes})"
        )

    groups = split_into_groups(timestamps, n_groups)

    for test_group_indices in combinations(range(n_groups), k_test_groups):
        test_timestamps: pd.DatetimeIndex = pd.DatetimeIndex([]).append(
            [groups[i] for i in test_group_indices]
        ).sort_values()

        test_start = test_timestamps.min()
        test_end   = test_timestamps.max()

        # Embargo window: observations within embargo_minutes after test end
        embargo_end = test_end + pd.Timedelta(minutes=embargo_minutes)

        # Purge: remove training observations whose label window overlaps test
        # A training observation at t overlaps test if:
        #   t + label_horizon > test_start  (its label uses data that's in test)
        purge_cutoff = test_start - pd.Timedelta(minutes=label_horizon_minutes)

        train_timestamps_raw: pd.DatetimeIndex = pd.DatetimeIndex([]).append(
            [groups[i] for i in range(n_groups) if i not in test_group_indices]
        ).sort_values()

        # Apply purging: keep only t < purge_cutoff OR t > embargo_end
        mask = (train_timestamps_raw < purge_cutoff) | (train_timestamps_raw > embargo_end)
        train_timestamps = train_timestamps_raw[mask]

        yield train_timestamps, test_timestamps


def run_cpcv(
    timestamps: pd.DatetimeIndex,
    labels: np.ndarray,
    features: np.ndarray,
    feature_names: list[str],
    model_config: dict,
    fees_config: dict,
    n_groups: int = 6,
    k_test_groups: int = 2,
    embargo_minutes: int = 30,
) -> pd.DataFrame:
    """
    Run full CPCV and return a DataFrame of per-split OOS metrics.

    Returns DataFrame with columns:
        split_id, n_train, n_test, test_start, test_end,
        sharpe, win_rate, brier, log_loss, n_trades
    """
    from strategies.strategy_a.model import StrategyAModel
    from backtesting.metrics.calibration import brier_score, log_loss_score
    from backtesting.metrics.trading import sharpe_ratio

    results = []
    split_id = 0

    for train_ts, test_ts in get_cpcv_splits(
        timestamps, n_groups, k_test_groups, embargo_minutes
    ):
        split_id += 1

        # Map timestamps to indices in the original arrays
        ts_to_idx = {t: i for i, t in enumerate(timestamps)}
        train_idx = np.array([ts_to_idx[t] for t in train_ts if t in ts_to_idx])
        test_idx  = np.array([ts_to_idx[t] for t in test_ts  if t in ts_to_idx])

        if len(train_idx) < 30 or len(test_idx) < 5:
            continue

        X_train, y_train = features[train_idx], labels[train_idx]
        X_test,  y_test  = features[test_idx],  labels[test_idx]

        model = StrategyAModel(model_config, fees_config)
        model.fit(X_train, y_train, feature_names)

        p_hat = np.array([
            model.predict_proba({n: v for n, v in zip(feature_names, row)})
            for row in X_test
        ])

        bs = brier_score(y_test, p_hat)
        ll = log_loss_score(y_test, p_hat)

        # Simplified trading metrics - assume trade when |edge| > min_edge
        p_market = 0.5  # TODO: wire real Kalshi prices per window
        edges = p_hat - p_market
        trade_mask = np.abs(edges) > 0.055  # approximate min_edge
        pnls = np.where(
            trade_mask,
            np.where(edges > 0,
                     np.where(y_test == 1, 1.0 - p_hat, -p_hat),
                     np.where(y_test == 0, 1.0 - (1.0 - p_hat), -(1.0 - p_hat))),
            0.0,
        )
        fee_drag = 0.03 * np.abs(trade_mask.astype(float))
        net_pnls = pnls - fee_drag

        sr = sharpe_ratio(net_pnls[trade_mask]) if trade_mask.sum() > 1 else float("nan")
        wr = float(net_pnls[trade_mask & (net_pnls > 0)].sum() / max(trade_mask.sum(), 1))

        results.append({
            "split_id": split_id,
            "n_train": len(train_idx),
            "n_test": len(test_idx),
            "test_start": test_ts.min(),
            "test_end": test_ts.max(),
            "sharpe": sr,
            "win_rate": wr,
            "brier": bs,
            "log_loss": ll,
            "n_trades": int(trade_mask.sum()),
        })

    return pd.DataFrame(results)
```

- [ ] **Step 4: Write `tests/test_cpcv.py`**

```python
import numpy as np
import pandas as pd
import pytest
import math
from backtesting.validation.cpcv import (
    get_cpcv_splits, split_into_groups, _n_combinations, _n_paths,
    LABEL_HORIZON_MINUTES,
)


def _make_timestamps(n: int, freq_min: int = 15) -> pd.DatetimeIndex:
    return pd.date_range("2025-01-01", periods=n, freq=f"{freq_min}min", tz="UTC")


def test_split_count():
    """C(6, 2) = 15 splits."""
    ts = _make_timestamps(180)  # 180 windows ÷ 6 groups = 30 per group
    splits = list(get_cpcv_splits(ts, n_groups=6, k_test_groups=2, embargo_minutes=30))
    assert len(splits) == math.comb(6, 2)


def test_path_count_formula():
    """(k · C(N,k)) / N = (2 · 15) / 6 = 5."""
    assert _n_paths(6, 2) == 5


def test_purging_removes_overlapping_labels():
    """
    Training obs at t must be purged if t + label_horizon > test_start.
    Use a small dataset so we can verify precisely.
    """
    ts = _make_timestamps(60)  # 60 windows of 15min each = 15 hours
    splits = list(get_cpcv_splits(ts, n_groups=6, k_test_groups=2, embargo_minutes=30))
    for train_ts, test_ts in splits:
        test_start = test_ts.min()
        purge_cutoff = test_start - pd.Timedelta(minutes=LABEL_HORIZON_MINUTES)
        # No training timestamp should be in [purge_cutoff, test_start)
        if len(train_ts) == 0:
            continue
        # Training obs just before test that should have been purged
        near_test = train_ts[(train_ts >= purge_cutoff) & (train_ts < test_start)]
        assert len(near_test) == 0, (
            f"Unpurged training obs in [{purge_cutoff}, {test_start}): {near_test}"
        )


def test_embargo_removes_post_test():
    """Training obs within embargo_minutes after test_end should be removed."""
    ts = _make_timestamps(120)
    embargo_min = 45
    splits = list(get_cpcv_splits(ts, n_groups=6, k_test_groups=2, embargo_minutes=embargo_min))
    for train_ts, test_ts in splits:
        test_end = test_ts.max()
        embargo_end = test_end + pd.Timedelta(minutes=embargo_min)
        # No training timestamp should be in (test_end, embargo_end]
        in_embargo = train_ts[(train_ts > test_end) & (train_ts <= embargo_end)]
        assert len(in_embargo) == 0, (
            f"Training obs in embargo window ({test_end}, {embargo_end}]: {in_embargo}"
        )


def test_train_test_disjoint():
    ts = _make_timestamps(120)
    splits = list(get_cpcv_splits(ts, n_groups=6, k_test_groups=2, embargo_minutes=30))
    for train_ts, test_ts in splits:
        overlap = set(train_ts) & set(test_ts)
        assert len(overlap) == 0, f"Train/test overlap: {len(overlap)} timestamps"


def test_embargo_floor_enforced():
    ts = _make_timestamps(60)
    with pytest.raises(ValueError, match="embargo_minutes"):
        list(get_cpcv_splits(ts, embargo_minutes=10))  # < 15 min floor
```

- [ ] **Step 5: Run tests**

```
python -m pytest tests/test_lookahead_checker.py tests/test_cpcv.py -v
```
Expected: 5 + 5 = 10 tests pass.

- [ ] **Step 6: Commit**

```bash
git add backtesting/validation/lookahead_checker.py backtesting/validation/cpcv.py tests/test_lookahead_checker.py tests/test_cpcv.py
git commit -m "feat: add look-ahead checker and CPCV with purging/embargo"
```

---

### Task 8: Validation adapters and bootstrap (`backtesting/validation/`)

**Files:**
- Create: `backtesting/validation/wfa_adapter.py`
- Create: `backtesting/validation/monte_carlo_adapter.py`
- Create: `backtesting/validation/bootstrap.py`

- [ ] **Step 1: Write `backtesting/validation/wfa_adapter.py`**

```python
"""
Adapter wrapping the existing backtesting/walk_forward.py.

The existing module requires data/split_config.json and imports backtest.py.
This adapter normalizes its output into the same per-fold metrics schema
used by CPCV and the report builder.

# TODO: Reconcile the actual walk_forward.py interface once data/split_config.json
# and backtest.py are available. The adapter below documents the expected interface.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import pandas as pd
from typing import Optional


SCHEMA_COLUMNS = [
    "fold_id", "fold_type", "n_train", "n_test",
    "train_start", "train_end", "test_start", "test_end",
    "sharpe", "win_rate", "total_pnl", "n_trades",
]


def _normalize_wfv_report(report: dict) -> pd.DataFrame:
    """
    Convert walk_forward.py's JSON output schema to the standard per-fold schema.

    # TODO: Adjust field names when actual walk_forward.py output is inspected.
    Expected walk_forward.py output structure (from reading the module source):
        {
            "windows": [
                {
                    "window_id": int,
                    "train_pnl": float,
                    "forward_pnl": float,
                    "efficiency_ratio": float,
                    "status": "PASS" | "FAIL",
                    ...
                }
            ],
            "wfv_efficiency_ratio": float,
            "overall_status": "PASS" | "MARGINAL" | "WARN OVERFIT"
        }
    """
    rows = []
    for w in report.get("windows", []):
        rows.append({
            "fold_id":    w.get("window_id", 0),
            "fold_type":  "wfa",
            "n_train":    w.get("train_n", None),
            "n_test":     w.get("forward_n", None),
            "train_start": None,  # TODO: extract from walk_forward output if available
            "train_end":   None,
            "test_start":  None,
            "test_end":    None,
            "sharpe":     None,   # TODO: walk_forward.py doesn't compute Sharpe directly
            "win_rate":   w.get("forward_win_rate", None),
            "total_pnl":  w.get("forward_pnl", None),
            "n_trades":   w.get("forward_n_trades", None),
        })
    return pd.DataFrame(rows, columns=SCHEMA_COLUMNS)


def run_wfa(config: dict, asset: str) -> pd.DataFrame:
    """
    Run the existing walk-forward validation for an asset.

    Calls the existing walk_forward.py as a subprocess to avoid contaminating
    this session's module state.

    Returns: per-fold metrics DataFrame in standard schema.
    # TODO: Reconcile once actual integration is confirmed.
    """
    wfv_path = os.path.join("backtesting", "walk_forward.py")
    if not os.path.exists(wfv_path):
        raise FileNotFoundError(f"walk_forward.py not found at {wfv_path}")

    wfv_cfg = config.get("validation", {}).get("wfa", {})
    windows  = wfv_cfg.get("windows", 6)
    mc_sims  = wfv_cfg.get("mc_sims", 50)

    result = subprocess.run(
        [sys.executable, wfv_path,
         "--windows", str(windows),
         "--mc-sims", str(mc_sims)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"walk_forward.py failed:\n{result.stderr}")

    report_path = os.path.join("results", "wfv_report.json")
    if not os.path.exists(report_path):
        return pd.DataFrame(columns=SCHEMA_COLUMNS)

    with open(report_path) as f:
        report = json.load(f)

    return _normalize_wfv_report(report)
```

- [ ] **Step 2: Write `backtesting/validation/monte_carlo_adapter.py`**

```python
"""
Adapter wrapping the existing backtesting/stress_test.py.

# TODO: Reconcile with actual stress_test.py interface.
Expected stress_test.py output (from reading the module source):
    results/stress_test_report.json with structure:
    {
        "iterations": [...],
        "degradation_curve": [...],
        "latency_table": {...}
    }
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import pandas as pd


SCHEMA_COLUMNS = [
    "iter_id", "slippage_bps", "latency_ms",
    "total_pnl", "win_rate", "sharpe", "n_trades",
]


def _normalize_mc_report(report: dict) -> pd.DataFrame:
    """
    Convert stress_test.py JSON output to standard per-iteration schema.
    # TODO: Adjust field names when actual stress_test.py output is inspected.
    """
    rows = []
    for i, it in enumerate(report.get("iterations", [])):
        rows.append({
            "iter_id":     i,
            "slippage_bps": it.get("slippage_bps", None),
            "latency_ms":   it.get("latency_ms", None),
            "total_pnl":    it.get("total_pnl", None),
            "win_rate":     it.get("win_rate", None),
            "sharpe":       None,  # TODO: add if stress_test.py computes it
            "n_trades":     it.get("n_trades", None),
        })
    return pd.DataFrame(rows, columns=SCHEMA_COLUMNS)


def run_monte_carlo(config: dict, asset: str) -> pd.DataFrame:
    """
    Run stress_test.py as a subprocess and return normalized output.
    # TODO: Reconcile once actual integration is confirmed.
    """
    st_path = os.path.join("backtesting", "stress_test.py")
    if not os.path.exists(st_path):
        raise FileNotFoundError(f"stress_test.py not found at {st_path}")

    mc_cfg  = config.get("validation", {}).get("monte_carlo", {})
    iters   = mc_cfg.get("iterations", 200)
    max_slip = mc_cfg.get("max_slippage_bps", 20)

    result = subprocess.run(
        [sys.executable, st_path,
         "--st-iters", str(iters),
         "--st-max-slippage", str(max_slip)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"stress_test.py failed:\n{result.stderr}")

    report_path = os.path.join("results", "stress_test_report.json")
    if not os.path.exists(report_path):
        return pd.DataFrame(columns=SCHEMA_COLUMNS)

    with open(report_path) as f:
        report = json.load(f)

    return _normalize_mc_report(report)
```

- [ ] **Step 3: Write `backtesting/validation/bootstrap.py`**

```python
"""
Politis-Romano stationary block bootstrap for confidence intervals.

Used for confidence intervals on Sharpe, win rate, and expectancy.
Default mean block length: 24 hours of 15-min trades = 96 periods.

Reference: Politis & Romano (1994) - "The Stationary Bootstrap."
"""
from __future__ import annotations
import numpy as np
from typing import Callable


def _geometric_block_lengths(n: int, mean_block_length: float, rng: np.random.Generator) -> list[int]:
    """Sample block lengths from geometric distribution with mean=mean_block_length."""
    p = 1.0 / mean_block_length
    blocks = []
    total = 0
    while total < n:
        length = int(rng.geometric(p))
        blocks.append(min(length, n - total))
        total += blocks[-1]
    return blocks


def stationary_bootstrap_sample(
    data: np.ndarray,
    mean_block_length: float,
    rng: np.random.Generator,
) -> np.ndarray:
    """
    Draw one bootstrap sample of the same length as `data` using
    the Politis-Romano stationary block bootstrap.
    """
    n = len(data)
    block_lengths = _geometric_block_lengths(n, mean_block_length, rng)
    sample = np.empty(n, dtype=data.dtype)
    pos = 0
    for blen in block_lengths:
        start = int(rng.integers(0, n))
        for j in range(blen):
            sample[pos] = data[(start + j) % n]
            pos += 1
            if pos >= n:
                break
        if pos >= n:
            break
    return sample


def bootstrap_ci(
    data: np.ndarray,
    statistic: Callable[[np.ndarray], float],
    n_iterations: int = 1000,
    mean_block_hours: float = 24.0,
    periods_per_hour: float = 4.0,  # 15-min periods
    alpha: float = 0.05,
    seed: int = 42,
) -> tuple[float, float, float]:
    """
    Compute bootstrap confidence interval for a statistic.

    Returns:
        (point_estimate, lower_ci, upper_ci) at (1-alpha) confidence level.
    """
    mean_block_length = mean_block_hours * periods_per_hour
    rng = np.random.default_rng(seed)
    point = statistic(data)

    boot_stats = np.empty(n_iterations)
    for i in range(n_iterations):
        sample = stationary_bootstrap_sample(data, mean_block_length, rng)
        boot_stats[i] = statistic(sample)

    lower = float(np.percentile(boot_stats, 100 * alpha / 2))
    upper = float(np.percentile(boot_stats, 100 * (1 - alpha / 2)))
    return point, lower, upper
```

- [ ] **Step 4: Commit**

```bash
git add backtesting/validation/wfa_adapter.py backtesting/validation/monte_carlo_adapter.py backtesting/validation/bootstrap.py
git commit -m "feat: add WFA/MC adapters and stationary block bootstrap"
```

---

### Task 9: Metrics - calibration, trading, overfitting, regime

**Files:**
- Create: `backtesting/metrics/calibration.py`
- Create: `backtesting/metrics/trading.py`
- Create: `backtesting/metrics/overfitting.py`
- Create: `backtesting/metrics/regime.py`
- Create: `tests/test_calibration.py`
- Create: `tests/test_overfitting_metrics.py`

- [ ] **Step 1: Write `backtesting/metrics/calibration.py`**

```python
"""
Calibration metrics for binary probability forecasters.

For binary markets, calibration matters more than raw accuracy.
A model that outputs P(up)=0.70 should win 70% of the time.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Optional


def brier_score(y_true: np.ndarray, p_hat: np.ndarray) -> float:
    """Brier score = mean((p_hat - y_true)^2). Smaller is better. Perfect=0."""
    return float(np.mean((p_hat - y_true) ** 2))


def log_loss_score(y_true: np.ndarray, p_hat: np.ndarray, eps: float = 1e-7) -> float:
    """Binary log loss = -mean(y*log(p) + (1-y)*log(1-p))."""
    p = np.clip(p_hat, eps, 1.0 - eps)
    return float(-np.mean(y_true * np.log(p) + (1 - y_true) * np.log(1 - p)))


def expected_calibration_error(
    y_true: np.ndarray,
    p_hat: np.ndarray,
    n_bins: int = 10,
) -> float:
    """
    Expected Calibration Error (ECE).

    ECE = Σ_b (|B_b| / n) · |acc(B_b) - conf(B_b)|
    where B_b is the set of predictions in bin b.
    """
    bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
    n = len(y_true)
    ece = 0.0
    for i in range(n_bins):
        lo, hi = bin_edges[i], bin_edges[i + 1]
        mask = (p_hat >= lo) & (p_hat < hi) if i < n_bins - 1 else (p_hat >= lo) & (p_hat <= hi)
        if mask.sum() == 0:
            continue
        acc  = float(y_true[mask].mean())
        conf = float(p_hat[mask].mean())
        ece += (mask.sum() / n) * abs(acc - conf)
    return ece


def reliability_diagram_data(
    y_true: np.ndarray,
    p_hat: np.ndarray,
    n_bins: int = 10,
) -> pd.DataFrame:
    """
    Per-bin data for a reliability diagram.

    Returns DataFrame with columns:
        bin_lower, bin_upper, bin_center,
        mean_predicted, fraction_positive, count
    """
    bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
    rows = []
    for i in range(n_bins):
        lo, hi = bin_edges[i], bin_edges[i + 1]
        mask = (p_hat >= lo) & (p_hat < hi) if i < n_bins - 1 else (p_hat >= lo) & (p_hat <= hi)
        count = int(mask.sum())
        rows.append({
            "bin_lower":         round(lo, 4),
            "bin_upper":         round(hi, 4),
            "bin_center":        round((lo + hi) / 2, 4),
            "mean_predicted":    float(p_hat[mask].mean()) if count > 0 else float("nan"),
            "fraction_positive": float(y_true[mask].mean()) if count > 0 else float("nan"),
            "count":             count,
        })
    return pd.DataFrame(rows)


def calibration_summary(
    y_true: np.ndarray,
    p_hat: np.ndarray,
    n_bins: int = 10,
    regime: Optional[str] = None,
) -> dict:
    return {
        "regime":   regime or "all",
        "brier":    brier_score(y_true, p_hat),
        "log_loss": log_loss_score(y_true, p_hat),
        "ece":      expected_calibration_error(y_true, p_hat, n_bins),
        "n":        int(len(y_true)),
    }
```

- [ ] **Step 2: Write `backtesting/metrics/trading.py`**

```python
"""
Trading performance metrics.
All P&L figures are in dollars, per-trade.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Optional


def sharpe_ratio(pnls: np.ndarray, periods_per_year: float = 252 * 96.0) -> float:
    """
    Annualized Sharpe ratio from per-trade P&L series.

    periods_per_year default: 252 trading days × 96 15-min periods/day.
    """
    if len(pnls) < 2:
        return float("nan")
    mean = float(np.mean(pnls))
    std  = float(np.std(pnls, ddof=1))
    if std == 0.0:
        return float("nan")
    return float(mean / std * np.sqrt(periods_per_year))


def daily_sharpe(daily_pnls: np.ndarray, periods_per_year: float = 252.0) -> float:
    if len(daily_pnls) < 2:
        return float("nan")
    mean = float(np.mean(daily_pnls))
    std  = float(np.std(daily_pnls, ddof=1))
    if std == 0.0:
        return float("nan")
    return float(mean / std * np.sqrt(periods_per_year))


def win_rate(pnls: np.ndarray) -> float:
    if len(pnls) == 0:
        return float("nan")
    return float((pnls > 0).mean())


def expectancy(pnls: np.ndarray) -> float:
    """Mean P&L per trade (expectancy)."""
    if len(pnls) == 0:
        return float("nan")
    return float(np.mean(pnls))


def max_drawdown(cumulative_pnl: np.ndarray) -> float:
    """Maximum peak-to-trough drawdown."""
    if len(cumulative_pnl) == 0:
        return float("nan")
    running_max = np.maximum.accumulate(cumulative_pnl)
    drawdowns = cumulative_pnl - running_max
    return float(np.min(drawdowns))


def avg_drawdown_duration(cumulative_pnl: np.ndarray) -> float:
    """Average duration (in periods) of drawdown episodes."""
    if len(cumulative_pnl) < 2:
        return float("nan")
    running_max = np.maximum.accumulate(cumulative_pnl)
    in_drawdown = cumulative_pnl < running_max
    durations = []
    current = 0
    for flag in in_drawdown:
        if flag:
            current += 1
        else:
            if current > 0:
                durations.append(current)
            current = 0
    if current > 0:
        durations.append(current)
    return float(np.mean(durations)) if durations else 0.0


def fee_drag(total_fees: float, gross_pnl: float) -> float:
    if gross_pnl == 0:
        return float("nan")
    return total_fees / gross_pnl


def trading_summary(
    trade_log: pd.DataFrame,
    regime: Optional[str] = None,
) -> dict:
    """
    Compute full trading summary from a trade log DataFrame.

    Required columns: pnl, fee, side, entry_time, exit_time
    """
    if trade_log.empty:
        return {"regime": regime or "all", "n_trades": 0}

    pnls = trade_log["pnl"].values
    fees = trade_log["fee"].values if "fee" in trade_log.columns else np.zeros(len(pnls))
    net_pnls = pnls - fees
    cum_pnl = np.cumsum(net_pnls)

    return {
        "regime":            regime or "all",
        "n_trades":          len(pnls),
        "sharpe":            sharpe_ratio(net_pnls),
        "win_rate":          win_rate(net_pnls),
        "expectancy":        expectancy(net_pnls),
        "total_pnl":         float(net_pnls.sum()),
        "total_gross_pnl":   float(pnls.sum()),
        "total_fees":        float(fees.sum()),
        "fee_drag":          fee_drag(float(fees.sum()), float(pnls.sum())),
        "max_drawdown":      max_drawdown(cum_pnl),
        "avg_drawdown_dur":  avg_drawdown_duration(cum_pnl),
    }
```

- [ ] **Step 3: Write `backtesting/metrics/overfitting.py`**

```python
"""
Overfitting detection metrics.

1. Deflated Sharpe Ratio (DSR) - Bailey & Lopez de Prado (2014)
2. Probability of Backtest Overfitting (PBO) - Bailey et al. (2017)
3. Probabilistic Sharpe Ratio (PSR) - Lopez de Prado & Bailey (2012)

All three consume the CPCV Sharpe distribution.
"""
from __future__ import annotations
import numpy as np
from scipy.stats import norm
from scipy.special import comb as sp_comb
import math


def deflated_sharpe_ratio(
    sharpe_obs: float,
    n_trials: int,
    n_obs: int,
    skew: float = 0.0,
    kurt: float = 3.0,
) -> tuple[float, float]:
    """
    Deflated Sharpe Ratio (DSR).

    Accounts for selection bias when a strategy is selected from n_trials.
    Returns (dsr, p_value).

    Reference: Bailey & Lopez de Prado (2014), Eq. (8).
    Expected maximum Sharpe under the null = SR*[n_trials, n_obs].
    """
    if n_obs < 2 or n_trials < 1:
        return float("nan"), float("nan")

    # Expected maximum Sharpe from n_trials iid draws (approximation)
    euler_mascheroni = 0.5772156649
    sr_star = (
        (1.0 - euler_mascheroni) * norm.ppf(1.0 - 1.0 / n_trials)
        + euler_mascheroni * norm.ppf(1.0 - 1.0 / (n_trials * math.e))
    )

    # Adjust for skew and kurtosis
    # DSR = PSR(SR*) where PSR uses the sample distribution moments
    z = (
        (sharpe_obs - sr_star)
        * np.sqrt(n_obs - 1)
        / np.sqrt(1.0 - skew * sharpe_obs + (kurt - 1.0) / 4.0 * sharpe_obs ** 2)
    )
    dsr = float(norm.cdf(z))
    p_value = float(1.0 - dsr)
    return dsr, p_value


def probabilistic_sharpe_ratio(
    sharpe_obs: float,
    sharpe_benchmark: float,
    n_obs: int,
    skew: float = 0.0,
    kurt: float = 3.0,
) -> float:
    """
    Probabilistic Sharpe Ratio (PSR).

    P(SR > SR_benchmark | observed SR, moments).
    Returns probability in [0, 1].

    Reference: Lopez de Prado & Bailey (2012), Eq. (2).
    """
    if n_obs < 2:
        return float("nan")
    z = (
        (sharpe_obs - sharpe_benchmark)
        * np.sqrt(n_obs - 1)
        / np.sqrt(1.0 - skew * sharpe_obs + (kurt - 1.0) / 4.0 * sharpe_obs ** 2)
    )
    return float(norm.cdf(z))


def probability_of_backtest_overfitting(
    oos_sharpes: np.ndarray,
    is_sharpes: np.ndarray,
) -> float:
    """
    Probability of Backtest Overfitting (PBO).

    Combinatorially Symmetric Cross-Validation approach.
    Estimates the probability that the in-sample best configuration
    performs below median out-of-sample.

    Args:
        oos_sharpes: array of OOS Sharpe ratios across CPCV paths
        is_sharpes:  array of IS Sharpe ratios across CPCV paths
                     (must be same length as oos_sharpes)

    Returns:
        PBO in [0, 1]. Values > 0.5 indicate likely overfitting.
    """
    if len(oos_sharpes) != len(is_sharpes) or len(oos_sharpes) == 0:
        return float("nan")

    n = len(oos_sharpes)
    # IS best → index of split with highest IS Sharpe
    is_best_idx = int(np.argmax(is_sharpes))
    # OOS performance of the IS-best split
    oos_best = oos_sharpes[is_best_idx]
    # Median OOS performance
    oos_median = float(np.median(oos_sharpes))
    # PBO = fraction of paths where oos_best < oos_median
    # (single-path version; multi-path extension uses CPCV paths)
    pbo = float(oos_best < oos_median)

    # Multi-path version: use logistic regression on log(Ω) where
    # Ω = relative rank of IS-best in OOS distribution
    omega = np.searchsorted(np.sort(oos_sharpes), oos_best) / n
    # Avoid log(0) or log(1)
    omega = np.clip(omega, 1e-4, 1.0 - 1e-4)
    # PBO via logistic form
    log_odds = np.log(omega / (1.0 - omega))
    pbo_logistic = float(1.0 / (1.0 + np.exp(log_odds)))
    return pbo_logistic


def overfitting_summary(
    oos_sharpes: np.ndarray,
    is_sharpes: np.ndarray,
    n_trials: int,
    sharpe_benchmark: float = 0.0,
) -> dict:
    """Compute all three overfitting metrics."""
    if len(oos_sharpes) == 0:
        return {"dsr": float("nan"), "dsr_pvalue": float("nan"),
                "pbo": float("nan"), "psr": float("nan")}

    sharpe_obs = float(np.mean(oos_sharpes))
    n_obs = len(oos_sharpes)
    skew = float(
        np.mean((oos_sharpes - sharpe_obs) ** 3) / max(np.std(oos_sharpes) ** 3, 1e-9)
    )
    kurt = float(
        np.mean((oos_sharpes - sharpe_obs) ** 4) / max(np.std(oos_sharpes) ** 4, 1e-9)
    )

    dsr, dsr_pvalue = deflated_sharpe_ratio(sharpe_obs, n_trials, n_obs, skew, kurt)
    psr = probabilistic_sharpe_ratio(sharpe_obs, sharpe_benchmark, n_obs, skew, kurt)
    pbo = probability_of_backtest_overfitting(oos_sharpes, is_sharpes)

    return {
        "sharpe_mean_oos": sharpe_obs,
        "n_paths":         n_obs,
        "dsr":             dsr,
        "dsr_pvalue":      dsr_pvalue,
        "psr":             psr,
        "pbo":             pbo,
    }
```

- [ ] **Step 4: Write `backtesting/metrics/regime.py`**

```python
"""
Regime-conditional metric breakdowns.

Breaks all metrics down by:
  - time-of-day session (reuses strategy_a time_of_day sessions)
  - weekday vs weekend
  - month
  - BTC volatility tercile (low/mid/high)

Flags regimes where DSR < 1 or fee-inclusive Sharpe <= 0.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Optional

from backtesting.metrics.calibration import calibration_summary
from backtesting.metrics.trading import trading_summary


SESSION_HOURS = {
    "asia_deep_night": (0, 4),
    "asia_active":     (4, 9),
    "eu_open":         (9, 12),
    "eu_us_overlap":   (12, 17),
    "us_afternoon":    (17, 21),
    "us_late":         (21, 24),
}


def _get_session(hour_utc: int) -> str:
    for name, (lo, hi) in SESSION_HOURS.items():
        if lo <= hour_utc < hi:
            return name
    return "us_late"


def _btc_vol_tercile(btc_vol_series: pd.Series, window_days: int = 30) -> pd.Series:
    """Assign each observation to low/mid/high BTC vol tercile."""
    rolling_vol = btc_vol_series.rolling(window=window_days).std()
    q33 = rolling_vol.quantile(0.33)
    q67 = rolling_vol.quantile(0.67)
    return pd.cut(
        rolling_vol,
        bins=[-np.inf, q33, q67, np.inf],
        labels=["low", "mid", "high"],
    )


def compute_regime_metrics(
    trade_log: pd.DataFrame,
    y_true: Optional[np.ndarray] = None,
    p_hat: Optional[np.ndarray] = None,
    btc_vol_series: Optional[pd.Series] = None,
    btc_vol_window_days: int = 30,
) -> pd.DataFrame:
    """
    Compute per-regime metrics from a trade log.

    trade_log must have: entry_time (UTC timestamp), pnl, fee, regime
    Returns a DataFrame with one row per regime subset.
    Flags: sharpe_flag (1 if Sharpe <= 0), ece_flag (1 if ECE > 0.10)
    """
    if trade_log.empty:
        return pd.DataFrame()

    results = []

    def _add_row(mask, regime_label, scope):
        subset = trade_log[mask]
        summary = trading_summary(subset, regime=f"{scope}={regime_label}")
        row = summary.copy()
        row["scope"] = scope
        row["regime_value"] = regime_label
        row["sharpe_flag"] = 1 if (summary.get("sharpe", 1) or 1) <= 0 else 0
        results.append(row)

    # By session
    if "entry_time" in trade_log.columns:
        hours = pd.to_datetime(trade_log["entry_time"]).dt.hour
        for session, (lo, hi) in SESSION_HOURS.items():
            mask = (hours >= lo) & (hours < hi)
            if mask.sum() > 0:
                _add_row(mask, session, "session")

        # Weekday vs weekend
        dow = pd.to_datetime(trade_log["entry_time"]).dt.dayofweek
        for label, mask in [("weekday", dow < 5), ("weekend", dow >= 5)]:
            if mask.sum() > 0:
                _add_row(mask, label, "day_type")

        # By month
        months = pd.to_datetime(trade_log["entry_time"]).dt.month
        for month in sorted(months.unique()):
            mask = months == month
            if mask.sum() > 0:
                _add_row(mask, str(month), "month")

    return pd.DataFrame(results)
```

- [ ] **Step 5: Write `tests/test_calibration.py`**

```python
import numpy as np
import pytest
from backtesting.metrics.calibration import (
    brier_score, log_loss_score, expected_calibration_error,
    reliability_diagram_data,
)


def test_brier_perfect():
    y = np.array([1, 0, 1, 0])
    p = np.array([1.0, 0.0, 1.0, 0.0])
    assert brier_score(y, p) == pytest.approx(0.0)


def test_brier_constant_half_balanced():
    """Constant p=0.5 on balanced labels → Brier = 0.25."""
    y = np.array([1, 0, 1, 0, 1, 0])
    p = np.full(6, 0.5)
    assert brier_score(y, p) == pytest.approx(0.25)


def test_brier_worst_case():
    """Completely wrong predictions → Brier = 1.0."""
    y = np.array([1, 1, 0, 0])
    p = np.array([0.0, 0.0, 1.0, 1.0])
    assert brier_score(y, p) == pytest.approx(1.0)


def test_log_loss_perfect():
    y = np.array([1, 0])
    p = np.array([0.9999999, 0.0000001])
    assert log_loss_score(y, p) < 0.001


def test_ece_perfect_calibration():
    """Perfectly calibrated predictions → ECE near zero."""
    rng = np.random.default_rng(42)
    p = rng.uniform(0, 1, 1000)
    y = (rng.random(1000) < p).astype(int)
    ece = expected_calibration_error(y, p, n_bins=10)
    assert ece < 0.05  # should be close to 0 for perfect calibration


def test_ece_constant_predictions():
    """Constant p=0.5 on balanced labels → ECE ≈ 0."""
    y = np.array([1, 0] * 500)
    p = np.full(1000, 0.5)
    ece = expected_calibration_error(y, p, n_bins=10)
    assert ece < 0.01  # all predictions in one bin, acc ≈ conf ≈ 0.5


def test_reliability_diagram_shape():
    y = np.array([1, 0, 1, 0, 1, 0] * 20)
    p = np.random.default_rng(0).uniform(0, 1, 120)
    df = reliability_diagram_data(y, p, n_bins=5)
    assert len(df) == 5
    assert "fraction_positive" in df.columns
    assert "count" in df.columns
```

- [ ] **Step 6: Write `tests/test_overfitting_metrics.py`**

```python
import numpy as np
import pytest
from backtesting.metrics.overfitting import (
    deflated_sharpe_ratio, probabilistic_sharpe_ratio,
    probability_of_backtest_overfitting, overfitting_summary
)


def test_dsr_constant_series():
    """DSR of a constant Sharpe series with 1 trial → determined by the formula."""
    dsr, pvalue = deflated_sharpe_ratio(sharpe_obs=0.0, n_trials=1, n_obs=100)
    assert 0.0 <= dsr <= 1.0
    assert 0.0 <= pvalue <= 1.0


def test_dsr_high_sharpe_many_trials():
    """Very high Sharpe with many trials should give moderate DSR (selection inflated)."""
    dsr, _ = deflated_sharpe_ratio(sharpe_obs=3.0, n_trials=100, n_obs=500)
    assert 0.0 <= dsr <= 1.0


def test_dsr_negative_sharpe():
    """Negative Sharpe → DSR near 0."""
    dsr, pvalue = deflated_sharpe_ratio(sharpe_obs=-2.0, n_trials=10, n_obs=200)
    assert dsr < 0.5


def test_psr_high_negative_sharpe():
    """PSR of a very negative Sharpe series → near zero."""
    psr = probabilistic_sharpe_ratio(
        sharpe_obs=-3.0, sharpe_benchmark=0.0, n_obs=252
    )
    assert psr < 0.01


def test_psr_high_positive_sharpe():
    """PSR of a large positive Sharpe → near 1."""
    psr = probabilistic_sharpe_ratio(
        sharpe_obs=3.0, sharpe_benchmark=0.0, n_obs=252
    )
    assert psr > 0.99


def test_pbo_range():
    oos = np.array([0.5, 0.8, -0.2, 1.2, 0.3])
    is_ = np.array([1.5, 0.9, 0.3, 0.7, 1.1])
    pbo = probability_of_backtest_overfitting(oos, is_)
    assert 0.0 <= pbo <= 1.0


def test_overfitting_summary_keys():
    oos = np.array([0.5, 0.8, -0.2, 1.2, 0.3])
    is_ = np.array([1.5, 0.9, 0.3, 0.7, 1.1])
    summary = overfitting_summary(oos, is_, n_trials=5)
    assert "dsr" in summary
    assert "pbo" in summary
    assert "psr" in summary
```

- [ ] **Step 7: Run tests**

```
python -m pytest tests/test_calibration.py tests/test_overfitting_metrics.py -v
```
Expected: 6 + 7 = 13 tests pass.

- [ ] **Step 8: Commit**

```bash
git add backtesting/metrics/ tests/test_calibration.py tests/test_overfitting_metrics.py
git commit -m "feat: add calibration, trading, overfitting, and regime metrics"
```

---

### Task 10: Simulation - fill model and backtest engine

**Files:**
- Create: `backtesting/simulation/fill_model.py`
- Create: `backtesting/simulation/backtest_engine.py`
- Create: `tests/test_fill_model.py`
- Create: `tests/test_backtest_engine.py`

- [ ] **Step 1: Write `backtesting/simulation/fill_model.py`**

See spec: taker fills, maker fills (pluggable logistic fill probability), adverse selection penalty, latency.

Fee = 3% from shared/fees.yaml. Slippage = cross the spread. Latency = configurable 500ms delay.

- [ ] **Step 2: Write `backtesting/simulation/backtest_engine.py`**

See spec: event-driven, single-threaded, no vectorization. Consumes strategy signals via locked interfaces. Emits per-trade records.

- [ ] **Step 3: Write tests for both**

See spec: verify 3% fee applied, slippage crosses spread, latency respected, no look-ahead.

- [ ] **Step 4: Commit**

---

### Task 11: Reports - report builder, comparison, Jinja2 templates

**Files:**
- Create: `backtesting/reports/report_builder.py`
- Create: `backtesting/reports/comparison.py`
- Create: `backtesting/reports/templates/asset_report.md.j2`
- Create: `backtesting/reports/templates/comparison_report.md.j2`

See spec for full content requirements. Use Jinja2. Generate PNG charts with matplotlib.

---

### Task 12: CLI (`backtesting/cli.py`) and README (`backtesting/README.md`)

**Files:**
- Create: `backtesting/cli.py`
- Create: `backtesting/README.md`

CLI subcommands: train, validate, backtest, report, all, dry-run.
Every subcommand runs sequentially. No parallelism.

---

### Task 13: Dry-run verification

Run `python backtesting/cli.py dry-run --asset btc --strategy a` on a 7-day synthetic window.
Fix any issues. Verify report artifact is produced under `backtesting/output/reports/`.

---
