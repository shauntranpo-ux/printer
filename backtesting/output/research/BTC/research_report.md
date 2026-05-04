# BTC — Backtest Validation Report

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
Real Sharpe: 4.307  Null 95th%: 0.575  p-value: 0.0000

**Lookahead findings:**
- [WARN] C:\Users\alxnt\kalshi-bot\backtesting\research\..\..\backtesting\output\models\btc_calibrated_model.pkl exists — verify calibrator was fit on training fold only, not full dataset, within WFA windows.
- [WARN] feature_builder.py may reference future bars (found shift(- or "future").

## Layer 3 — WFA Significance
DSR: 1.000  PBO: 0.000  MinBTL: 0.1yr (have 8.7yr)

## Layer 4 — Permutation Test
Trades: 208353  Win rate: 53.4%  p-value (block): 0.0000

## Layer 5 — Regime Robustness

| Regime | Sharpe |
|--------|--------|
| high_mean_reverting | 6.504 |
| high_random | 6.107 |
| high_trending | 1.559 |
| low_mean_reverting | 3.971 |
| low_random | 2.145 |
| low_trending | 2.173 |
| mid_mean_reverting | 5.635 |
| mid_random | 4.505 |
| mid_trending | 3.410 |

| Session | Sharpe |
|---------|--------|
| Asia | 2.715 |
| London | 4.634 |
| US | 5.322 |
