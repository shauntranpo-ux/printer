# Backtesting Run Summary

**Run date:** 2026-04-22
**Run start:** ~11:13 UTC
**Run aborted:** ~12:11 UTC
**Wall-clock elapsed:** ~58 minutes
**Execution mode:** single-threaded, sequential throughout (OMP/OpenBLAS/MKL/NUMEXPR all set to 1)

---

## Status Table

| Asset | Strategy | Train | CPCV | WFA | MC | Backtest | Report |
|-------|----------|-------|------|-----|----|----------|--------|
| BTC   | A        | ✅    | ✅   | ❌  | ❌ | ❌ FAILED (SIGTERM/EXIT:143, no trades) | —    |
| BTC   | B        | —     | —    | —   | —  | —        | —      |
| ETH   | A        | —     | —    | —   | —  | —        | —      |
| ETH   | B        | —     | —    | —   | —  | —        | —      |
| SOL   | A        | —     | —    | —   | —  | —        | —      |
| SOL   | B        | —     | —    | —   | —  | —        | —      |
| XRP   | A        | —     | —    | —   | —  | —        | —      |
| XRP   | B        | —     | —    | —   | —  | —        | —      |

`—` = not reached due to upstream abort

---

## Step-by-Step Log (BTC / Strategy A)

### Step 1 — Preflight ✅
- All four parquet files confirmed present under `data/historical/`
- Dry-run passed (19/19 tests, all 8 report artifacts generated)
- CLI help text verified for train / validate / backtest / report / all / dry-run

### Step 2 — Train ✅
- Command: `python backtesting/cli.py train --asset btc --strategy a`
- 4,553,049 bars loaded from `BTC_1m_extended.parquet`
- HAR-RS-J coefficients fitted via OLS on 303,520 feature rows
- Sidecar written: `strategies/strategy_a/config/btc.fitted.yaml`
- Model + calibrator written to `backtesting/output/models/`

### Step 3 — CPCV ✅
- Command: `python backtesting/cli.py validate --asset btc --strategy a --method cpcv`
- Runtime: ~33 minutes (1.5M predictions × 5 calibrated estimators ≈ 800 µs/call)
- Output: `backtesting/output/validation/btc/a/cpcv.json` (4,559 bytes)
- Note: p_market hardcoded to 0.5 — no live Kalshi tick feed wired (known TODO)

### Step 4a — WFA ❌ FAILED (pre-existing bug)
- Command: `python backtesting/cli.py validate --asset btc --strategy a --method wfa`
- Error: `KeyError: 'train_start_date'` in `backtesting/walk_forward.py:175`
- Root cause: `walk_forward.py` references a config key absent from `backtest.yaml`
- No output artifact produced

### Step 4b — MC ❌ FAILED (pre-existing bug)
- Command: `python backtesting/cli.py validate --asset btc --strategy a --method mc`
- Error: `monte_carlo_adapter.py` invokes `stress_test.py --st-iters 200 --st-max-slippage 20`
  but `stress_test.py` expects `--iters` and `--max-slip` (argument name mismatch)
- No output artifact produced

### Step 5 — Backtest ❌ ABORTED (O(n²) performance bug)
- Command: `python backtesting/cli.py backtest --asset btc --strategy a`
- PID 11836, started ~11:50:40 UTC, killed ~12:11 UTC (21 minutes elapsed)
- Progress at kill time: ~16k / 303,936 windows (~91 windows/sec)
- Estimated time-to-completion at that rate: ~55 additional minutes (one instance alone)
- **Root cause:** O(n²) in `backtesting/data/aligner.py` → `iter_windows`
  - `events_seen` list grows unboundedly — window i receives all ~16×i prior events
  - `backtest_engine.py:96` does a linear filter over that list per window:
    `bars_events = [e for e in events_before if e.event_type == "bar"]`
  - Total work across 303k windows ≈ 7.4 × 10¹¹ Python iterations
  - Extrapolated: ~2 hrs/instance, ~20 hrs for all 8 — infeasible
- No trades output, no report generated

---

## Failure Log

| # | Step | File | Error |
|---|------|------|-------|
| 1 | WFA | `backtesting/walk_forward.py:175` | `KeyError: 'train_start_date'` |
| 2 | MC | `backtesting/monte_carlo_adapter.py` (subprocess) | `stress_test.py` unrecognized args `--st-iters`, `--st-max-slippage` |
| 3 | Backtest | `backtesting/data/aligner.py` → `iter_windows` | O(n²): growing `events_seen` list + linear scan in `backtest_engine.py:96` |

---

## Partial Metrics (BTC / Strategy A — CPCV only)

| Metric | Value |
|--------|-------|
| CPCV splits | 15 (6 groups, k=2 test, 30-min embargo) |
| p_market assumption | 0.50 (placeholder) |
| Feature rows | 303,520 |
| Training bars | 4,553,049 |
| CPCV output | `backtesting/output/validation/btc/a/cpcv.json` |

No backtest trade log, Sharpe, win-rate, or equity curve available for any asset.

---

## Red Flags

1. **O(n²) backtest engine is a hard blocker.** `iter_windows` + `backtest_engine.py:96` makes full runs infeasible. Must fix before any production backtest.

2. **WFA is broken for all assets.** `walk_forward.py:175` will `KeyError` on every call until `train_start_date` is added to config or the lookup is patched.

3. **MC is broken for all assets.** `monte_carlo_adapter.py` subprocess uses wrong arg names. Every MC validation silently fails.

4. **CPCV edge estimates are meaningless.** `p_market=0.5` hardcoded — edge computed against a coin flip, not real Kalshi prices. Results cannot inform go/no-go decisions until live tick data is wired.

---

## Artifacts Produced

| Artifact | Path |
|----------|------|
| BTC/A HAR sidecar | `strategies/strategy_a/config/btc.fitted.yaml` |
| BTC/A model + calibrator | `backtesting/output/models/` |
| BTC/A CPCV output | `backtesting/output/validation/btc/a/cpcv.json` |

No per-asset reports, no comparison report.

---

## Three Fixes Required Before Next Run

### Fix A — `aligner.py` O(n²) `iter_windows`

`iter_windows` must yield only the events relevant to the current window, not the full accumulated history. Minimal fix: cap the yielded slice to the last 16 elements (one 15-min window of 1m bars):

```python
# in iter_windows, replace:
yield window_ts, events_seen
# with:
yield window_ts, events_seen[-window_bars:]   # e.g. window_bars=16
```

The correct fix is to yield `events[lo:hi]` for the exact window span using a two-pointer approach, so `backtest_engine.py:96` operates on a bounded list at all times.

### Fix B — `walk_forward.py:175` KeyError

Add to `backtesting/configs/backtest.yaml` under `data:`:
```yaml
train_start_date: "2020-01-01"
```
or change `walk_forward.py:175` to fall back to `data.get("start_date")`.

### Fix C — `monte_carlo_adapter.py` wrong arg names

Change the subprocess call from:
```
stress_test.py --st-iters 200 --st-max-slippage 20
```
to:
```
stress_test.py --iters 200 --max-slip 20
```

---

## Single-Threaded Execution Confirmation

All commands ran with:
```
OMP_NUM_THREADS=1  OPENBLAS_NUM_THREADS=1
MKL_NUM_THREADS=1  NUMEXPR_NUM_THREADS=1
```
No subprocess spawning (except existing MC adapter), no asyncio, no threading in pipeline code.
Sequential order maintained: train → validate → backtest → report, BTC first.
