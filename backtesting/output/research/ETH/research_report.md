# ETH — Backtest Validation Report

**Overall verdict: PASS**

## Layer Summary

| Layer | Verdict |
|-------|---------|
| Layer 2 — Null Hypothesis | ✓ PASS |
| Layer 3 — WFA Significance | ✓ PASS |
| Layer 4 — Permutation Test | ✓ PASS |
| Layer 5 — Regime Robustness | ✓ PASS |

---

## Layer 2 — Null Hypothesis
Real Sharpe: 6.072  Null 95th%: 0.636  p-value: 0.0000

**Lookahead findings:**
- [WARN] C:\Users\alxnt\kalshi-bot\backtesting\research\..\..\backtesting\output\models\eth_calibrated_model.pkl exists — verify calibrator was fit on training fold only, not full dataset, within WFA windows.
- [WARN] feature_builder.py may reference future bars (found shift(- or "future").

## Layer 3 — WFA Significance
DSR: 1.000  PBO: 0.000  MinBTL: 0.1yr (have 6.6yr)

## Layer 4 — Permutation Test
Trades: 155964  Win rate: 53.9%  p-value (block): 0.0000

## Layer 5 — Regime Robustness

| Regime | Sharpe |
|--------|--------|
| high_mean_reverting | 6.064 |
| high_random | 7.823 |
| high_trending | 4.407 |
| low_mean_reverting | 4.239 |
| low_random | 6.579 |
| low_trending | 5.994 |
| mid_mean_reverting | 6.417 |
| mid_random | 7.835 |
| mid_trending | 5.883 |

| Session | Sharpe |
|---------|--------|
| Asia | 4.332 |
| London | 8.046 |
| US | 6.455 |
