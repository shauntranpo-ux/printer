# FINAL VERDICT: 15-Minute Kalshi Crypto Binary Strategies

**Date:** 2026-05-07
**Strategies:** S1 (PRINTER_BRAIN / strategy_brain_s1), S2 (FifteenMinStrategy / strategy_brain_s2)
**Verdict: NO EXTRACTABLE EDGE**

---

## Diagnosis Chain

### Step 2A - S1 Silence (VOL_GATE_DOMINANT)

S1 (PRINTER_BRAIN) has produced zero live trades. Root cause: two gates in series.

- **Vol gate** (`vol_ratio = rv x sqrtmins_left / abs_pct >= 1.80`) fires first. For near-ATM markets
  (dist_row 0, abs_pct < 0.1%) BTC vol_ratio is 6-7x, well above threshold. Gate passes - but
  those cells have median EV of -4.2% (negative after fee).
- **EV gate** (`ev = bv3_win_prob - entry/100 - 0.07 >= min_ev_base_s1`) blocks the remainder.

Median S1 EV across all windows: **-4.2%**. Only 4.3% of windows clear the 8% EV gate.
Correct conclusion: the gate is working as designed. S1 is silent because it has nothing to trade.

### Step 2B - S2 Calibration (INSUFFICIENT_DATA / NEGATIVE_EV)

S2 (FifteenMinStrategy) has 90 DB rows, of which 78 are synthetic test data (`KXBTC-TEST-N`
market IDs, not real Kalshi format). Real S2 trades: **12** (Phase 1, 2026-03-31 only).

- 5 settled: **0 wins, 5 losses** (all stop_loss exits). Win rate: 0.0%.
- 7 voided: unconfirmed fills.
- Calibration gap: **+45.8pp** (mean model confidence 45.8%, actual win rate 0%).
- PnL: **-$30.43** on real trades.

5 settled trades is not a calibration dataset. Verdict: insufficient data for calibration,
but directionally negative.

### Step 2C - Data Integrity (SYNTHETIC_DATA)

78 of 90 S2 rows are synthetic. Market IDs `KXBTC-TEST-0` through `KXBTC-TEST-77` match
no real Kalshi market format. All have null confidence_score, identical 72c YES entries,
1-contract size, and exact-hour timestamps. Generator script not in repo or git history.
These rows excluded from all analysis. Corrected S2 real count: 12.

### Step 2D - S1 Edge Existence Check (EDGE_EXISTS_AT - INVALIDATED by 2H)

OOS scan of available 2026 settlement data (2026-02-25 to 2026-05-03, 67 days) found
12 qualifying cells at dist_row 3-4 (0.3-0.5% from strike), t_min 9-13. These cells
showed positive EV (+0.3% to +3.0%) and positive Sharpe in the BV3 OOS scan.

**This result was invalidated by Step 2H.**

### Step 2E - Narrow Holdout (WITHIN-PERIOD STABILITY ONLY)

4-slice holdout across the same 67-day 2026 window. Not a true holdout - all 4 slices
within one market regime. 8 of 12 cells survived as ROBUST or LIKELY_ROBUST.

**This result was invalidated by Step 2H.**

### Step 2F - Asset Concentration

Edge appeared generic (all 4 assets had qualifying cells). ETH heaviest (5 cells, avg EV +2.28%).
BTC disagreed with S2 live paper (expected: S1 is continuation, S2 is contrarian).
Critical finding: 6 of 8 surviving cells fail the 1.80 vol gate - whitelist would be a no-op.

### Step 2H - TRUE Multi-Regime Holdout (NO EDGE - DEFINITIVE)

Synthetic settlements generated from `{ASSET}_1m_extended.parquet` (BTC back to 2017-08-17).
Full 2024-01-01 to 2026-04-09 window, 4 equal slices (~7 months each):

| Slice | Period | Key events |
|-------|--------|------------|
| 1 | 2024-01-01 to 2024-07-15 | BTC halving April 2024 |
| 2 | 2024-07-15 to 2025-01-26 | BTC ATH ~$99k Nov 2024 |
| 3 | 2025-01-26 to 2025-08-09 | Post-ATH correction |
| 4 | 2025-08-09 to 2026-04-09 | Includes live Kalshi data period |

**Result: 0 of 8 candidate cells survive.**

- BTC r3t10, r3t11, r4t10: negative EV in all 4 slices (REGIME_LUCK)
- ETH r3t13: alternating signs (QUESTIONABLE - noise)
- ETH r4t9, r4t10, r4t11: negative EV in 3-4 slices (REGIME_LUCK)
- XRP r3t12: alternating signs (QUESTIONABLE - noise)

The 10-week 2026 window happened to be favorable. It does not represent the distribution.

---

## Conclusion

**The 15-minute Kalshi crypto binary market with the BV3+momentum+velocity feature set
has no extractable edge over a 2+ year, multi-regime OOS window.**

The BV3 empirical win-rate table was built on historical BTC price continuation - the
tendency for price to stay on the same side of a reference strike over a 15-minute window.
In backtesting, the signal is real: prices do persist. But:

1. The Kalshi market price already reflects this - it prices continuation probability
   efficiently. The 7% fee eliminates any residual edge.
2. The signal degrades across regimes. In high-vol periods (halving, ATH cycles) the
   continuation persistence shifts enough to make BV3 predictions stale.
3. S2 (contrarian FifteenMinStrategy) failed in the opposite direction - betting against
   persistence when persistence is the dominant regime.

This confirms the section11 framework prediction: in a fee-efficient binary prediction
market with a liquid underlying, simple feature-set strategies (price distance + momentum
+ velocity) do not generate durable alpha.

---

### Step 3A - Gamma Zone Scan (NO EDGE - STRUCTURAL)

Hypothesis: the deep ITM/OTM late-window zone (T=1-3 min, dist >= 1.0% from strike) might
show edge by selling residual volatility at expiry - betting a 1%+ gap won't reverse.

Results across 35 cells with n >= 100 (BTC/ETH/SOL/XRP, 2024-01-01 to 2026-04-09):

- **Empirical win rates: 99.7-100%**. The continuation signal is near-certain in this zone.
- **Entry price proxy (BV3): caps at 99c** - BV3 predicts 1.000 for all extreme cells.
- **EV: exactly $0.00 or negative for every cell.** The structural constraint:
  - At 99c entry: profit-if-win = $0.01. Kalshi taker fee = `ceil(0.07 x 0.99 x 0.01)` = **$0.01**.
  - Net profit-if-win = **$0.00**. EV = $0.00 at 100% win rate; negative below.
- **Zero preliminary survivors** (n >= 100, gap <= 10pp, EV > 0, Sharpe > 0).
- **Fill risk:** Hourly Kalshi ladder data shows `yes_ask = 1.00` for deep-ITM strikes. No
  15-minute orderbook depth data exists; all gamma zone cells flagged FILL_RISK.

This is a mathematical result, not a data artifact. The $0.01 minimum fee exactly cancels
the $0.01 maximum profit at 99c entry. No amount of additional data changes this.

---

## Conclusion

**No extractable edge anywhere in 15-minute Kalshi crypto binary markets with the BV3
+ momentum + velocity feature set. All zones tested:**

| Zone tested | T range | Dist range | Result |
|-------------|---------|------------|--------|
| Directional (Steps 2D-2H) | 9-13 min | 0.3-0.5% | Regime-luck artifact only |
| Gamma (Step 3A) | 1-3 min | >=1.0% | Fee structure prevents profit |
| Near-ATM (Step 2A) | all | <0.1% | Median EV -4.2% |

The project formally moves to `docs/diagnosis/NEXT_DIRECTIONS.md`.

---

## Status

| Strategy | DB trades | OOS edge | Status |
|----------|-----------|----------|--------|
| S1 (PRINTER_BRAIN) | 0 live | None across regimes | **Dead** |
| S2 (FifteenMinStrategy) | 12 real | 0/5 settled wins | **Dead** |
| Gamma zone (3A spike) | 0 real | Fee-blocked | **Dead** |

`bot_enabled` set to `false` in `config.json`.
Both strategy calls remain in `bot_loops.py` with warning comments (code preserved for archival).
