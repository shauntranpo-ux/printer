# Dead Code Cleanup + Research Signal Extractor Fix - Design Spec
**Date:** 2026-05-03  
**Status:** Approved  
**Goal:** Delete all pre-ultraplan strategy code; rewrite the research signal extractor to test the five actual voters from `compute_15m_signal`.

---

## Problem

The codebase contains two separate problems:

1. **Dead code:** 18 old signal files and 5 old per-asset strategy files survive from before the ultraplan rewrite. None are imported by the live bot. They mislead readers and bloat the test suite.

2. **Wrong backtest signals:** `backtesting/research/signal_extractor.py` was written against a now-obsolete signal list (supertrend, exhaustion_fade, ratio_divergence, rolling_beta, variance_ratio, volume_spike). These are not what `compute_15m_signal` computes. IC results from these signals are meaningless for validating the live strategy.

---

## What the Live Strategy Actually Uses

`FifteenMinStrategy` → `BaseStrategy.decide()` → `compute_15m_signal()`:

| Voter | Signal | Direction |
|-------|--------|-----------|
| V1 | `compute_bs_p_yes` | +1 if BS p_yes > 0.5 |
| V2 | MTF momentum (5/15/30-bar avg return) | **inverted** - negative momentum → YES |
| V3 | RSI deviation from 50 | **inverted** - oversold → YES |
| V4 | Bollinger z-score | **inverted** - below lower band → YES |
| V5 | MTF magnitude soft confirmation | **inverted** - large negative move → YES |

Requires 3-of-5 agreement. All math is self-contained in `fifteen_min_signal.py`.

---

## Part 1 - Delete Dead Code

### Files to delete from `src/strategies/signals/`
- supertrend.py
- exhaustion_fade.py
- ratio_divergence.py
- rolling_beta.py
- variance_ratio.py
- volume_spike.py
- btc_context.py
- correlation_monitor.py
- event_calendar.py
- kalshi_velocity.py
- taper.py
- intraday_signals.py
- solana_health.py
- funding_dispersion.py
- beta_cache.py
- idiosyncratic_detector.py
- btc_diurnal_obi.py
- session_clock.py

### Files to delete from `src/strategies/`
- btc_strategy.py
- sol_strategy.py
- xrp_strategy.py
- doge_strategy.py
- baseline.py

### Files to delete from `tests/strategies/`
- test_signals.py (tests only deleted signal files)
- test_supertrend.py (tests deleted supertrend.py)

### Files to update
- `bot.py`: remove import branches for BTCStrategy, SOLStrategy, XRPStrategy, DOGEStrategy - keep only FifteenMinStrategy instantiation paths
- `src/strategies/__init__.py` (if it re-exports deleted modules): remove dead exports

### Files NOT touched (ultraplan - untouched)
- `fifteen_min_strategy.py`, `fifteen_min_signal.py`, `base.py`
- `black_scholes.py`, `features.py`, `calibration.py`
- `ev.py`, `skip_layer.py`, `fees.py`, `feature_builder.py`
- `time_windows.py` (used by bot.py for trading window scheduling)

---

## Part 2 - Rewrite Research Signal Extractor

**File:** `backtesting/research/signal_extractor.py`

Replace the 8 old signal names and their extractors with the 5 actual voters:

```python
SIGNAL_NAMES = [
    'v1_bs_prob',
    'v2_mtf_momentum',
    'v3_rsi',
    'v4_bollinger',
    'v5_mtf_magnitude',
]
```

Each extractor produces a per-bar probability array (float in [0, 1]):
- `v1_bs_prob`: BS p_yes directly (continuous, from black_scholes.py)
- `v2_mtf_momentum`: `0.65 if mtf < -T else (0.35 if mtf > T else 0.5)` - inverted
- `v3_rsi`: `0.65 if rsi_dev < -T else (0.35 if rsi_dev > T else 0.5)` - inverted
- `v4_bollinger`: `0.65 if boll_z < -T else (0.35 if boll_z > T else 0.5)` - inverted
- `v5_mtf_magnitude`: `0.65 if mtf < -T/2 else (0.35 if mtf > T/2 else 0.5)` - inverted

Thresholds from `fifteen_min_signal.py` per-asset dicts: `_MTF_THRESHOLDS`, `_RSI_THRESHOLDS`, `_BOLL_THRESHOLDS`. Default to BTC values when asset not found.

The helper functions `_multi_tf_mom`, `_rsi`, `_boll_zscore` are copied (not imported) from `fifteen_min_signal.py` into `signal_extractor.py` to keep the extractor self-contained for batch bar processing.

Update tests in `tests/backtesting/research/test_signal_extractor.py` to use the 5 new signal names.

---

## Part 3 - Re-run Validation

After the extractor rewrite, run:
```
python backtesting/research_cli.py --asset BTC --layers 1 --iters 1000
```

Layer 1 results with the correct signals are the primary deliverable. Layers 2-5 continue to use the trade log and are unaffected by this change.

---

## Out of Scope
- Modifying `compute_15m_signal` itself
- Changing thresholds or strategy parameters
- Deleting backtesting scripts in `scripts/` that reference old signals (harmless historical artifacts)
- Deleting old tests for backtest engine, CPCV, WFA (those test infrastructure, not the dead signals)
