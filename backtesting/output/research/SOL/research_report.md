# SOL — Backtest Validation Report

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
Real Sharpe: 1.301  Null 95th%: 0.706  p-value: 0.0010

**Lookahead findings:**
- [WARN] C:\Users\alxnt\kalshi-bot\backtesting\research\..\..\backtesting\output\models\sol_calibrated_model.pkl exists — verify calibrator was fit on training fold only, not full dataset, within WFA windows.
- [WARN] feature_builder.py may reference future bars (found shift(- or "future").

## Layer 3 — WFA Significance
DSR: 1.000  PBO: 0.000  MinBTL: 1.6yr (have 5.6yr)

## Layer 4 — Permutation Test
Trades: 137194  Win rate: 52.4%  p-value (block): 0.0000

## Layer 5 — Regime Robustness

| Regime | Sharpe |
|--------|--------|
| high_mean_reverting | 1.657 |
| high_random | 0.617 |
| high_trending | 0.204 |
| low_mean_reverting | 3.463 |
| low_random | 2.523 |
| low_trending | 2.527 |
| mid_mean_reverting | -0.673 |
| mid_random | -0.275 |
| mid_trending | 1.573 |

| Session | Sharpe |
|---------|--------|
| Asia | 1.175 |
| London | 0.222 |
| US | 1.884 |
