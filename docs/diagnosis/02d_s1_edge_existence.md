# Step 2D Diagnosis: S1 Edge Existence Check (OOS)

**Date:** 2026-05-07
**Verdict:** `EDGE_EXISTS_AT:BTC_r3_t10,BTC_r3_t11,BTC_r3_t13,BTC_r4_t10,ETH_r3_t13,ETH_r4_t9,ETH_r4_t10,ETH_r4_t11,ETH_r4_t12,SOL_r3_t13,XRP_r3_t12,XRP_r4_t9`

---

## TL;DR

S1 shows exploitable edge at: **BTC_r3_t10,BTC_r3_t11,BTC_r3_t13,BTC_r4_t10,ETH_r3_t13,ETH_r4_t9,ETH_r4_t10,ETH_r4_t11,ETH_r4_t12,SOL_r3_t13,XRP_r3_t12,XRP_r4_t9**
At least one (asset, distance, time) cell passes all 4 qualifying criteria.
Recommends narrowing S1 to those cells only.

---

## 2D.1 Data

| Item | Value |
|------|-------|
| Settlement period | 2026-02-25 to 2026-05-03 |
| OOS status | All data is OOS (BV3 trained on pre-2024 data) |
| Assets analyzed | BTC, ETH, SOL, XRP |
| Total (dist_row x time_col) cells | 519 |
| Cells with n >= 100 | 318 |
| Qualifying cells | 12 |

**Entry price proxy:** BV3 predicted win prob used as implied entry price.
Conservative: assumes Kalshi prices track BV3. Positive EV means
empirical win rate exceeded BV3 prediction by more than the 7% fee.

---

## 2D.2 EV Distribution (n >= 100 cells)

Top 20 cells by EV, among those with sufficient data:

| Asset | Dist bucket | T (min) | n | Empirical WR | BV3 pred | Gap | EV | Sharpe |
|-------|-------------|---------|---|-------------|---------|-----|-----|--------|
| BTC | 0.3-0.4% | 12 | 153 | 90.8% | 77.8% | -13.1% | +6.0% | 0.21 |
| BTC | 0.2-0.3% | 13 | 348 | 83.9% | 71.3% | -12.6% | +5.6% | 0.15 |
| BTC | 0.4-0.5% | 9 | 129 | 100.0% | 88.3% | -11.7% | +4.7% | 0.00 |
| ETH | 0.4-0.5% | 11 | 138 | 93.5% | 83.5% | -10.0% | +3.0% | 0.12 **Q** |
| ETH | 0.3-0.4% | 13 | 211 | 83.9% | 74.1% | -9.8% | +2.8% | 0.08 **Q** |
| ETH | 0.4-0.5% | 12 | 126 | 90.5% | 80.9% | -9.6% | +2.6% | 0.09 **Q** |
| BTC | 0.3-0.4% | 10 | 233 | 93.6% | 84.0% | -9.6% | +2.6% | 0.10 **Q** |
| XRP | 0.3-0.4% | 12 | 175 | 86.9% | 77.8% | -9.1% | +2.1% | 0.06 **Q** |
| ETH | 0.4-0.5% | 9 | 211 | 97.2% | 88.3% | -8.9% | +1.9% | 0.11 **Q** |
| BTC | 0.4-0.5% | 10 | 102 | 95.1% | 86.9% | -8.2% | +1.2% | 0.06 **Q** |
| ETH | 0.4-0.5% | 10 | 201 | 95.0% | 86.9% | -8.1% | +1.1% | 0.05 **Q** |
| SOL | 0.3-0.4% | 13 | 264 | 82.2% | 74.1% | -8.1% | +1.1% | 0.03 **Q** |
| BTC | 0.3-0.4% | 11 | 174 | 89.7% | 81.6% | -8.1% | +1.1% | 0.04 **Q** |
| BTC | 0.3-0.4% | 13 | 110 | 81.8% | 74.1% | -7.7% | +0.7% | 0.02 **Q** |
| XRP | 0.4-0.5% | 9 | 137 | 95.6% | 88.3% | -7.3% | +0.3% | 0.02 **Q** |
| BTC | 0.3-0.4% | 8 | 297 | 96.3% | 89.3% | -7.0% | -0.0% | -0.00 |
| BTC | 0.2-0.3% | 9 | 651 | 90.5% | 83.5% | -7.0% | -0.0% | -0.00 |
| BTC | 0.2-0.3% | 10 | 567 | 87.8% | 81.1% | -6.7% | -0.3% | -0.01 |
| XRP | 0.4-0.5% | 10 | 124 | 93.5% | 86.9% | -6.7% | -0.4% | -0.01 |
| SOL | 0.4-0.5% | 12 | 152 | 87.5% | 80.9% | -6.6% | -0.4% | -0.01 |

---

## 2D.3 Calibration Analysis

Largest calibration gaps (BV3 over-prediction), n >= 100:

| Asset | Dist bucket | T (min) | n | BV3 pred | Empirical WR | Gap |
|-------|-------------|---------|---|---------|-------------|-----|
| XRP | <0.1% | 1 | 1770 | 85.0% | 67.2% | +17.8% |
| XRP | <0.1% | 2 | 1796 | 79.6% | 67.6% | +12.0% |
| SOL | <0.1% | 1 | 2436 | 85.0% | 73.9% | +11.1% |
| XRP | <0.1% | 3 | 1867 | 75.8% | 65.6% | +10.2% |
| XRP | <0.1% | 5 | 1974 | 70.5% | 61.4% | +9.2% |
| ETH | <0.1% | 1 | 2471 | 85.0% | 76.0% | +9.0% |
| XRP | <0.1% | 4 | 1911 | 72.7% | 64.1% | +8.6% |
| XRP | <0.1% | 6 | 2070 | 68.6% | 60.4% | +8.2% |
| XRP | <0.1% | 7 | 2145 | 67.2% | 59.1% | +8.1% |
| XRP | 0.1-0.2% | 1 | 1103 | 98.0% | 90.2% | +7.8% |

---

## 2D.4 Qualifying Cells

Cells meeting all criteria (n>=100, |gap|<=10pp, EV>0, Sharpe>0):

| Asset | Dist bucket | T (min) | n | Empirical WR | BV3 pred | Gap | EV | Sharpe |
|-------|-------------|---------|---|-------------|---------|-----|-----|--------|
| BTC | 0.3-0.4% | 10 | 233 | 93.6% | 84.0% | -9.6% | +2.6% | 0.10 |
| BTC | 0.3-0.4% | 11 | 174 | 89.7% | 81.6% | -8.1% | +1.1% | 0.04 |
| BTC | 0.3-0.4% | 13 | 110 | 81.8% | 74.1% | -7.7% | +0.7% | 0.02 |
| BTC | 0.4-0.5% | 10 | 102 | 95.1% | 86.9% | -8.2% | +1.2% | 0.06 |
| ETH | 0.3-0.4% | 13 | 211 | 83.9% | 74.1% | -9.8% | +2.8% | 0.08 |
| ETH | 0.4-0.5% | 9 | 211 | 97.2% | 88.3% | -8.9% | +1.9% | 0.11 |
| ETH | 0.4-0.5% | 10 | 201 | 95.0% | 86.9% | -8.1% | +1.1% | 0.05 |
| ETH | 0.4-0.5% | 11 | 138 | 93.5% | 83.5% | -10.0% | +3.0% | 0.12 |
| ETH | 0.4-0.5% | 12 | 126 | 90.5% | 80.9% | -9.6% | +2.6% | 0.09 |
| SOL | 0.3-0.4% | 13 | 264 | 82.2% | 74.1% | -8.1% | +1.1% | 0.03 |
| XRP | 0.3-0.4% | 12 | 175 | 86.9% | 77.8% | -9.1% | +2.1% | 0.06 |
| XRP | 0.4-0.5% | 9 | 137 | 95.6% | 88.3% | -7.3% | +0.3% | 0.02 |

**Action:** Narrow S1 to operate ONLY in qualifying cells.
Set `min_ev_base_s1 = 0` and add cell whitelist filter.

---

## 2D.5 Summary Statistics

### Per-asset EV statistics (n >= 100 cells)

| Asset | Cells (n>=100) | Median EV | Max EV | Cells with EV>0 |
|-------|--------------|-----------|--------|-----------------|
| BTC | 69 | -3.3% | +6.0% | 7 |
| ETH | 86 | -5.8% | +3.0% | 5 |
| SOL | 90 | -7.1% | +1.1% | 1 |
| XRP | 73 | -7.7% | +2.1% | 2 |
