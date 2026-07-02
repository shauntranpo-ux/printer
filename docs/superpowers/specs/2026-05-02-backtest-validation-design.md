# Backtest Validation System - Design Spec
**Date:** 2026-05-02  
**Status:** Approved - pending implementation plan  
**Goal:** Determine with statistical rigor whether the D3-hybrid strategy has real edge or is overfitted to historical data.

---

## Problem Statement

The bot has WFA, CPCV, Monte Carlo, and bootstrap infrastructure already built. What it lacks is the statistical layer that answers: *"Is the Sharpe ratio I'm seeing real, given how many configurations were tested to produce it?"* The existing backtests can show profitable results even for pure-noise strategies if enough parameter combinations are tried. This spec adds five layers that jointly answer the profitability question.

---

## Architecture

Five sequential layers. Each emits a `PASS / CONDITIONAL / FAIL` verdict. A fail does not block later layers - it flags a root cause to investigate.

```
Historical 1m bars + Kalshi snapshots
          │
          ▼
┌─────────────────────────────────────┐
│ Layer 1: Signal Validation          │
│   IC, ICIR, t-stat per sub-signal   │
│   IC decay curve at lags 1,2,4,8   │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│ Layer 2: Strategy Simulation        │
│   Replay decide() + fill model      │
│   Lookahead audit                   │
│   Shuffled-signal null (1000 iter)  │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│ Layer 3: WFA Significance           │
│   Deflated Sharpe Ratio (DSR)       │
│   Probability of Backtest           │
│   Overfitting (PBO)                 │
│   Minimum Backtest Length (MinBTL)  │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│ Layer 4: Permutation Test           │
│   Shuffle trade outcomes 10,000×    │
│   Full shuffle + 10-trade block     │
│   p-value against null              │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│ Layer 5: Regime Robustness          │
│   3 vol terciles × 3 VR regimes     │
│   Session slice (Asia/London/US)    │
│   Minimum 30 trades per cell        │
└──────────────┬──────────────────────┘
               │
               ▼
     research_report.md  +  research.json
```

**Entry point:** `python backtesting/research.py --asset BTC [--layers 1,2,3,4,5]`

**Output files:**
- `backtesting/output/research/{asset}/research_report.md` - human-readable verdict
- `backtesting/output/research/{asset}/research.json` - machine-readable per-layer results
- `backtesting/output/research/{asset}/ic_curves.png` - Layer 1 IC decay plots
- `backtesting/output/research/{asset}/regime_heatmap.png` - Layer 5 Sharpe grid

---

## Layer 1 - Signal Validation (IC / ICIR)

### Purpose
Test each D3 sub-signal in isolation before trusting the ensemble. Identifies dead-weight components that add noise without predictive value.

### Signals tested
All sub-signals feeding into `compute_15m_signal()`:
- `supertrend_direction`
- `bs_probability` (Black-Scholes)
- `momentum_delta`
- `exhaustion_fade`
- `ratio_divergence`
- `rolling_beta`
- `variance_ratio`
- `volume_spike`

### Label construction
For each 15-min window at timestamp T with strike K:
```
outcome = 1 if price(T + 15min) > K else 0
```
Labels built from historical 1m bars. Strike extracted from Kalshi ticker.

### Metrics

| Metric | Formula | Pass threshold |
|--------|---------|----------------|
| IC | Spearman(predicted_p_yes, outcome) | > 0.03 |
| ICIR | mean(IC) / std(IC) over 30-bar rolling windows | > 0.30 |
| IC t-stat | IC × √N | > 2.0 |

**Note on binary IC ranges:** Because Kalshi outcomes are binary (not continuous returns), IC values are naturally smaller than in equity factor research. IC = 0.03 is meaningful here. Do not apply the continuous-market standard of 0.05.

**IC decay curve:** Compute IC at lags +1, +2, +4, +8 candles (15, 30, 60, 120 min ahead). A genuine predictive signal decays slowly. A noise signal hits IC ≈ 0 at lag +2.

### Implementation
New file: `backtesting/research/signal_extractor.py`
- Runs each sub-signal independently against historical bars
- Returns `{signal_name: {ic, icir, t_stat, ic_decay: [lag1, lag2, lag4, lag8]}}`

### Gate criterion
- **PASS:** IC t-stat > 2.0 AND ICIR > 0.30
- **CONDITIONAL:** t-stat > 1.5 OR ICIR > 0.20
- **FAIL:** t-stat ≤ 1.5

If 3+ sub-signals fail: flag ensemble as unreliable. Later layers should be interpreted with caution.

### Example output
```
Signal              IC      ICIR    t-stat  Verdict
────────────────────────────────────────────────────
supertrend_dir      0.067   0.61    3.4     PASS
bs_probability      0.041   0.38    2.1     PASS
momentum_delta      0.012   0.11    0.6     FAIL
exhaustion_fade    -0.003   0.04    0.2     FAIL
ratio_divergence    0.055   0.52    2.8     PASS
rolling_beta        0.031   0.29    1.6     CONDITIONAL
variance_ratio      0.044   0.41    2.3     PASS
volume_spike        0.009   0.08    0.5     FAIL
```

---

## Layer 2 - Strategy Simulation + Null Hypothesis Test

### Purpose
Replay the actual `decide()` pipeline on historical data. Prove the edge comes from the signal, not from the EV gate, position sizing, or lucky market timing.

### Lookahead audit (prerequisite)
Before running either simulation, audit these known lookahead risks:

1. **AssetCalibrator** - does it refit on training folds only, or on the full dataset? Must use training-fold-only calibration within WFA windows.
2. **realized_vol_1min** - does the rolling vol window use any future bars? Must be strictly backward-looking at decision time T.
3. **Kalshi AMM simulator** - are prices at time T+ε used for the T decision? Must use only prices available at T.

Any lookahead found must be fixed before continuing. A corrupted engine invalidates all downstream layers.

### Run A - Real strategy replay
Feed each 15-min window through `BaseStrategy.decide()` with the existing fill model (slippage + latency from `fill_model.py`). Record every trade: timestamp, side, entry_cents, exit_cents, P&L.

### Run B - Shuffled-signal null (1000 iterations)
Keep all filters (EV gate, vol gate, entry range, fill model) identical. Randomize only `st_side` (draw from {yes, no} at 50/50) and `signal_raw_p_yes` (sample from the empirical distribution of real p_yes values). Measures: "does the directional signal add value beyond what the gates alone produce?"

### Gate criterion
```
p-value = fraction of null iterations where null_sharpe ≥ real_sharpe

p < 0.05  → PASS
p < 0.10  → CONDITIONAL
p ≥ 0.10  → FAIL
```

### Example output
```
BTC  Real Sharpe: 1.43
     Null 95th%:  0.61    p-value: 0.009   → PASS
ETH  Real Sharpe: 1.21
     Null 95th%:  1.09    p-value: 0.042   → PASS (marginal)
SOL  Real Sharpe: 0.88
     Null 95th%:  0.84    p-value: 0.12    → FAIL
```

---

## Layer 3 - WFA Significance (DSR + PBO + MinBTL)

### Purpose
Ask whether the WFA Sharpe is real after accounting for the number of parameter configurations tested to produce it.

### 3a - Deflated Sharpe Ratio (DSR)

Standard Sharpe is optimistically biased when many configs are tested and the best is kept. DSR deflates for:
- Non-normality of trade returns (skew, kurtosis)
- Length of backtest (fewer observations = less reliable)  
- Number of independent trials tested (every EV sweep config counts)

**Implementation:** Follow AFML Chapter 14 exactly (Bailey & López de Prado 2014). Use `scipy.stats` for the normal CDF. Estimate `num_trials` from the EV sweep CSVs in `backtesting/output/` - acknowledge this is an approximation since configs are not fully independent.

**Pass threshold:** DSR > 0.95 (95% confident true Sharpe > 0 after adjusting for multiple testing)

### 3b - Probability of Backtest Overfitting (PBO)

Requires running multiple strategy variants (minimum 5 EV threshold configs) through CPCV. For each CPCV fold combination:
- Identify which config ranked best in-sample
- Record whether that same config ranked above median out-of-sample
- PBO = fraction of fold combinations where it did not

**Pass threshold:** PBO < 0.25

### 3c - Minimum Backtest Length (MinBTL)

How many years of data are needed to reject H0 (SR = 0) at α = 0.05?

```
MinBTL ≈ (Z_α / SR)² years    where Z_0.05 = 1.645

SR = 1.0  →  MinBTL ≈ 2.7 years
SR = 1.5  →  MinBTL ≈ 1.2 years
SR = 2.0  →  MinBTL ≈ 0.7 years
```

**Pass threshold:** MinBTL ≤ years of data used in backtest

### Example output
```
Asset   Raw SR   DSR    PBO    MinBTL   Data avail   Verdict
──────────────────────────────────────────────────────────────
BTC      1.43   0.97   0.18   1.2yr    3.1yr        PASS
ETH      1.21   0.81   0.31   1.8yr    2.4yr        CONDITIONAL
SOL      0.89   0.61   0.44   3.4yr    1.8yr        FAIL
```

---

## Layer 4 - Permutation Test (Trade-Level)

### Purpose
Layer 2 shuffles the signal. Layer 4 shuffles the outcomes. These are complementary:
- Layer 2: "Does the signal add value?" (randomize signal, keep outcomes real)
- Layer 4: "Could this P&L have happened by luck?" (keep signal real, randomize outcomes)

### Procedure
Take completed trade P&L list. Shuffle the dollar P&L amounts 10,000 times (preserving entry timestamps). Recompute Sharpe each time. Build null distribution.

### Two shuffle variants run in parallel

| Variant | Method | Purpose |
|---------|--------|---------|
| Full shuffle | All P&L values randomly reordered | Tests pure aggregate luck |
| Block shuffle | 10-trade rolling blocks shuffled | Preserves within-window autocorrelation |

Block shuffle is more conservative. Use block shuffle p-value as the primary result.

### Minimum trades
Significance at p < 0.05 requires sufficient trades. Minimum depends on win rate:

```
Win rate 55% → ~500 trades minimum
Win rate 60% → ~100 trades
Win rate 65% → ~50 trades
```

If trade count is below the minimum for the observed win rate, report `INSUFFICIENT_DATA` rather than FAIL.

### Gate criterion
```
p < 0.05  → PASS
p < 0.10  → CONDITIONAL
p ≥ 0.10  → FAIL or INSUFFICIENT_DATA
```

One-tailed test (we only care if Sharpe > null).

### Example output
```
BTC  Trades: 312   Win rate: 59%   Min needed: ~120   ✓
     Real Sharpe: 1.43
     Null median: 0.03   Null 95th%: 0.48
     p-value (block): 0.004   → PASS

DOGE Trades: 23    Win rate: 57%   Min needed: ~300   ✗
     → INSUFFICIENT_DATA (need ~277 more trades)
```

---

## Layer 5 - Regime Robustness

### Purpose
Determine whether the edge holds across different market conditions or is concentrated in one specific regime.

### Regime Axis 1 - Volatility (BTC reference)
```
Low vol   → bottom tercile of 30-day BTC realized vol
Mid vol   → middle tercile
High vol  → top tercile
```

### Regime Axis 2 - Trend vs. Mean-Reversion (Variance Ratio Test)
Uses Lo-MacKinlay (1988) variance ratio test on a 60-bar rolling window. Chosen over Hurst exponent because it is reliable at short window lengths (30-60 bars) whereas Hurst requires 100+ observations for stability.

```
VR < 0.90  → mean-reverting (price oscillates around strike)
0.90-1.10  → random walk
VR > 1.10  → trending (price moves persistently)
```

### Crossed grid: 3 vol × 3 VR = 9 cells per asset
Minimum 30 trades per cell for Sharpe to be reported. Cells below 30 trades reported as `LOW_DATA`.

### Session slice
Crypto markets run 24/7. Split by session:
```
Asia session:    20:00-04:00 ET
London session:  04:00-09:00 ET
US session:      09:00-20:00 ET
```

### Gate criterion
```
PASS        → Sharpe > 0 in ≥ 6/9 cells, no cell < −1.0
CONDITIONAL → Sharpe > 0 in 4-5 cells; report which regimes to avoid
FAIL        → Sharpe positive in ≤ 3 cells (regime-dependent, fragile)
```

### Example output
```
BTC Regime Breakdown (Sharpe by cell):

              Low vol    Mid vol    High vol
Trending       1.82       1.44       0.91
Random walk    0.71       0.38      -0.22
Mean-revert   -0.14       0.12       0.55

Session slice:
  Asia:    SR=0.92  (87 trades)
  London:  SR=1.31  (54 trades)
  US:      SR=1.68  (171 trades)

Finding: Edge concentrated in low/mid-vol trending regimes.
         Weakens in high-vol mean-revert. Conditional pass.
         Consider config flag to disable in VR<0.90 + high-vol.
```

---

## Final Report Structure

`research_report.md` sections:
1. **Verdict summary** - overall PASS / CONDITIONAL / FAIL with one-line reason per layer
2. **Layer 1** - IC table + decay curves
3. **Layer 2** - null distribution plot + lookahead audit findings
4. **Layer 3** - DSR / PBO / MinBTL table
5. **Layer 4** - permutation p-values + trade count check
6. **Layer 5** - regime heatmap + session breakdown
7. **Recommendations** - which configs/regimes to enable or disable based on findings

---

## What's Out of Scope

- Building new signal components (only evaluating existing D3 signals)
- Changing strategy parameters based on results (separate config PR)
- Hourly market strategy validation (15m only)
- Multi-asset portfolio-level backtesting (per-asset only)
