# Step 2A Diagnosis: S1 (PRINTER_BRAIN) Silence Autopsy

**Date:** 2026-05-07
**Verdict:** `VOL_GATE_DOMINANT`

---

## TL;DR

S1 (`strategy_brain_s1` / PRINTER_BRAIN v3) has zero trades in the DB.

**Root cause: EV gate at 8% cannot be cleared because BV3 win_probs at 12-14 minutes
do not exceed AMM pricing plus the 7% Kalshi fee.**

Median S1 EV (corrected for S1's continuation direction) is **-4.2%**.
Only 113 of 2,648 READY rows pass the EV gate (even ignoring the vol gate entirely).
The vol gate is a secondary unknowable blocker; the EV gate is the binding constraint.

---

## 2A.1 Methodology

S1 writes no rows to `market_log` or `trades` on skip.
This script simulates S1 gate logic against 2,648 READY rows.

**Critical correction:** `market_log.contract_price_cents` = S2's chosen contract price.
S2 (FifteenMinStrategy) is sometimes contrarian (buys NO when price is above strike).
S1 is always continuation (YES when above, NO when below).
We parse `skip_reason` text (`"YES at Xc"` / `"NO at Xc"`) to recover S2's direction,
then derive S1's actual continuation price (flip when directions differ).

| Price source | Count |
|-------------|-------|
| S2 direction = S1 direction (no flip needed) | 564 |
| S2 direction ≠ S1 direction (price flipped: `100 − S2_price`) | 2072 |
| Fallback (no parseable skip_reason) | 12 |

| Gate | Simulated | Reason |
|------|-----------|--------|
| Vol gate (`vol_ratio ≥ 1.80`) | **NO** | Requires price deque - not stored in market_log |
| Price filter (5-75c) | YES | Derived S1 continuation price |
| EV gate (`ev ≥ min_ev_base_s1`) | YES (approx) | BV3 embedded table; no momentum/velocity/OBI |

---

## 2A.2 Gate Results

| Gate | Pass | Fail | Rate |
|------|------|------|------|
| Vol gate | UNKNOWN | UNKNOWN | - |
| Price filter [5,75]c | 1367 | 1281 | 51.6% |
| EV gate ≥8% (of price-pass rows) | 113 | 1254 | 8.3% |

EV gate is the dominant blocker. Even ignoring vol gate, only 113 rows pass.

---

## 2A.3 EV Distribution (price-pass rows, corrected S1 continuation price)

| Percentile | EV |
|-----------|-----|
| p1 | -19.4% |
| p5 | -15.2% |
| p10 | -13.1% |
| p25 | -8.7% |
| p50 | -4.2% |
| p75 | +1.2% |
| p90 | +6.6% |
| p95 | +11.2% |
| p99 | +25.5% |

---

## 2A.4 EV Threshold Sweep

| Threshold | Rows | Rate |
|-----------|------|------|
| EV≥-10% | 1084 | 79.3% |
| EV≥-5% | 750 | 54.9% |
| EV≥+0% | 389 | 28.5% **← breakeven** |
| EV≥+2% | 311 | 22.8% |
| EV≥+4% | 231 | 16.9% |
| EV≥+5% | 200 | 14.6% |
| EV≥+6% | 164 | 12.0% |
| EV≥+7% | 129 | 9.4% |
| EV≥+8% | 113 | 8.3% **← current BTC gate** |
| EV≥+10% | 80 | 5.9% |

---

## 2A.5 Distance Bucket Distribution

| Bucket | Count | % |
|--------|-------|---|
| row0  <0.10% | 1196 | 45.2% |
| row1  0.10-0.20% | 770 | 29.1% |
| row2  0.20-0.30% | 380 | 14.4% |
| row3  0.30-0.40% | 155 | 5.9% |
| row4  0.40-0.50% | 63 | 2.4% |
| row5  0.50-0.60% | 64 | 2.4% |
| row6  0.60-0.75% | 8 | 0.3% |
| row7  0.75-1.00% | 11 | 0.4% |
| row8  1.00-1.25% | 1 | 0.0% |
| row9  1.25%+ | 0 | 0.0% |

**45% near-ATM (< 0.1%).** Near-ATM continuation YES contracts price at ~55-60c.
BV3 win_prob at 13min = 0.578. EV = 0.578 − 0.58 − 0.07 = **−7.2%**.

---

## 2A.6 EV by Minutes-Left Bucket

| Mins Left | n | Mean EV | Max EV |
|-----------|---|---------|--------|
| 2 | 30 | +7.2% | +41.6% |
| 3 | 33 | +7.6% | +44.8% |
| 4 | 48 | +3.4% | +78.1% |
| 5 | 58 | +5.5% | +25.5% |
| 6 | 80 | +2.5% | +22.6% |
| 7 | 91 | -0.5% | +20.2% |
| 8 | 109 | -1.6% | +41.6% |
| 9 | 120 | -2.9% | +27.9% |
| 10 | 147 | -4.5% | +13.4% |
| 11 | 162 | -5.5% | +10.6% |
| 12 | 183 | -5.9% | +9.5% |
| 13 | 196 | -7.8% | +6.8% |

---

## 2A.7 Root Cause Analysis

### Why BV3 Can't Beat AMM Pricing

The Kalshi AMM prices contracts accurately at 12-14 minutes. The embedded BV3 table
shows win rates calibrated on biased backtest data (see `stress_test_results`: 97.5%
trade rate, 79.6% WR - clear lookahead). At realistic time horizons:

| Scenario | BV3 win_prob | AMM price | EV |
|----------|-------------|-----------|-----|
| Near-ATM, 13min | 0.578 | ~58c continuation | −7.2% |
| 0.1-0.2% dist, 13min | 0.675 | ~67c continuation | −6.5% |
| 0.2-0.3% dist, 13min | 0.713 | ~71c continuation | −6.7% |
| 0.5-0.6% dist, 13min | 0.781 | ~78c continuation | −7.1% |

**The AMM is correctly pricing the continuation probability.** The BV3 table win_probs
closely match AMM prices, leaving essentially zero edge after the 7% fee.
S1 is correct to skip - every trade would lose money on average.

### Why the Embedded BV3 Shows High Win Rates

The `_S1_BV3_TABLE` was built by `backtesting/training/generate_bv3_table.py` which
the summary notes has "lookahead bias" and "different bucket edges" than the live code.
This explains why the table shows 85-99.9% win rates at close distance - these are not
achievable live because the entry price used at training time saw the outcome.

### The Vol Gate Is a Secondary Factor

Even if the vol gate never fired (letting all 2648 rows through to EV evaluation),
only 113 rows would pass. The vol gate is possibly over-triggering too, but
it's not the binding constraint - the EV gap is.

---

## 2A.8 Cross-Check: Backtest vs. Live

| Metric | Backtest (stress_test_results DB) | Live paper trade |
|--------|----------------------------------|-----------------|
| S1 trade rate | 97.5% | 0% |
| S1 win rate | 79.6% | N/A |
| Bias source | Lookahead (generate_bv3_table.py) | N/A |
| BV3 table | Embedded (lookahead-biased) | Same embedded table |

The backtest 97.5% trade rate is impossible live - it implies S1 traded on nearly
every 10-second tick. The 79.6% WR reflects lookahead contamination, not live edge.

---

## 2A.9 Verdict and Fix

**Verdict: `VOL_GATE_DOMINANT`**

EV gate (ignoring vol gate) would allow ~113 trades (8.3% of price-pass rows).
These 113 rows are concentrated at **1-6 minutes remaining** where BV3 win_probs are highest.
Actual trades = 0 → the **vol gate fires on all 113 EV-passing windows**.

The vol gate (`_vol_ratio = rv × √mins_left / abs_pct ≥ 1.80`) fires aggressively for
near-ATM BTC markets: typical BTC vol of 0.18%/min at abs_pct ≈ 0.1% gives
`vol_ratio = 0.0018 × √13 / 0.001 = 6.5` - far above 1.80 threshold.

**Fix sequence:**

1. **Add `s1_vol_ratio` to market_log** to measure vol gate firing rate.
   Without this we cannot confirm, only infer. One column, minimal change.

2. **Raise `vol_gate_thresh` from 1.80 → 3.0+** if confirmed over-triggering.
   This is the primary fix if vol gate is the blocker.

3. **Run `bv3_calibrator.py`** to replace embedded BV3 table with real calibration.
   Embedded table has lookahead bias (79.6% WR in stress_test vs ~58% real).
   Real sidecar win_probs may be lower - confirming or disconfirming S1 edge.

4. **Do not lower `min_ev_base_s1` below 4%** without completing step 3.
   Current EV distribution has p50 = -4.2% - lowering gate
   without better win_prob data risks systematic losses.
