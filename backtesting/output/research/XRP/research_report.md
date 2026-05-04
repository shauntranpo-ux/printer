# XRP — Backtest Validation Report

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
Real Sharpe: 3.628  Null 95th%: 0.893  p-value: 0.0000

**Lookahead findings:**
- [WARN] C:\Users\alxnt\kalshi-bot\backtesting\research\..\..\backtesting\output\models\xrp_calibrated_model.pkl exists — verify calibrator was fit on training fold only, not full dataset, within WFA windows.
- [WARN] feature_builder.py may reference future bars (found shift(- or "future").

## Layer 3 — WFA Significance
DSR: 1.000  PBO: 0.000  MinBTL: 0.2yr (have 6.6yr)

## Layer 4 — Permutation Test
Trades: 100890  Win rate: 53.2%  p-value (block): 0.0000

## Layer 5 — Regime Robustness

| Regime | Sharpe |
|--------|--------|
| high_mean_reverting | 6.172 |
| high_random | 2.732 |
| high_trending | 2.394 |
| low_mean_reverting | 1.940 |
| low_random | 3.414 |
| low_trending | 2.723 |
| mid_mean_reverting | 4.052 |
| mid_random | 4.525 |
| mid_trending | 4.427 |

| Session | Sharpe |
|---------|--------|
| Asia | 3.566 |
| London | -1.115 |
| US | 5.808 |
