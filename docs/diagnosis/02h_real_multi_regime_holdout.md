# Step 2H Diagnosis: Real Multi-Regime Holdout

**Date:** 2026-05-07
**Input:** 8 cells surviving Step 2E within-window stability check
**Surviving cells:** 0

---

## 2H.1 Root Cause of Data Gap

`backtesting/scripts/fetch_kalshi_settlements.py` fetches settled markets
from the Kalshi REST API (`/markets?series_ticker=KXBTC15M&status=settled`).
Kalshi's API returns at most ~90 days of settlement history. The parquet
files only contain records from ~2026-02-25 onwards.

**Step 2E used 4 slices within a single 10-week window - not a holdout test.**
It was a within-period stability check only.

## 2H.2 Synthetic Settlement Generation

Synthetic settlements generated from `{ASSET}_1m_extended.parquet` price bars.
Methodology matches `bv3_calibrator.py` entry logic:

```
strike      = open of first 1m bar AT window_open
close_price = close of last 1m bar BEFORE close_time (window_open + 15min)
result      = 1 if close_price > strike else 0
```

This is internally consistent with how `_S1_BV3_TABLE` was originally built.
Synthetic records cover 2024-01-01 to the start of real Kalshi data;
real API-fetched records used for the 2026-02-25 to 2026-04-09 overlap.

---

## 2H.3 True 4-Slice Holdout Results

| Slice | Date range | Key events |
|-------|-----------|------------|
| Slice1_2024H1 (Jan24-Jul24) | 2024-01-01 to 2024-07-15 | BTC halving April 2024 |
| Slice2_2024H2 (Jul24-Jan25) | 2024-07-15 to 2025-01-26 | BTC ATH Nov 2024 (~$99k) |
| Slice3_2025H1 (Jan25-Aug25) | 2025-01-26 to 2025-08-09 | Post-ATH correction, BTC ~$80-95k |
| Slice4_2025H2 (Aug25-Apr26) | 2025-08-09 to 2026-04-10 | Includes real Kalshi data period |

EV = empirical_win_rate - BV3_pred - 0.07 (using BV3 as entry price proxy)
Min 20 observations required per slice for a valid EV reading.

| Cell | n_total | Slice1 EV | Slice2 EV | Slice3 EV | Slice4 EV | Valid | Pos | Robustness | Decision |
|------|---------|-----------|-----------|-----------|-----------|-------|-----|------------|---------|
| BTC_r3_t10 | 2704 | -3.1% (n=743) | -4.3% (n=715) | -1.4% (n=528) | -0.5% (n=718) | 4/4 | 0/4 | REGIME_LUCK | **DROP** |
| BTC_r3_t11 | 2244 | -4.0% (n=630) | -4.1% (n=593) | -2.1% (n=406) | -0.5% (n=615) | 4/4 | 0/4 | REGIME_LUCK | **DROP** |
| BTC_r4_t10 | 1235 | -3.8% (n=332) | -3.9% (n=319) | -4.5% (n=208) | -0.3% (n=376) | 4/4 | 0/4 | REGIME_LUCK | **DROP** |
| ETH_r3_t13 | 3079 | +5.5% (n=455) | -0.6% (n=590) | +2.2% (n=828) | -2.1% (n=1206) | 4/4 | 2/4 | QUESTIONABLE | **DROP** |
| ETH_r4_t9 | 2945 | -1.6% (n=506) | -2.6% (n=577) | -3.0% (n=832) | -3.5% (n=1030) | 4/4 | 0/4 | REGIME_LUCK | **DROP** |
| ETH_r4_t10 | 2571 | -2.2% (n=409) | -2.3% (n=467) | -2.1% (n=748) | -3.7% (n=947) | 4/4 | 0/4 | REGIME_LUCK | **DROP** |
| ETH_r4_t11 | 2155 | -0.9% (n=356) | +0.1% (n=374) | -0.2% (n=601) | -3.5% (n=824) | 4/4 | 1/4 | REGIME_LUCK | **DROP** |
| XRP_r3_t12 | 4314 | +2.7% (n=763) | -6.2% (n=1227) | -2.8% (n=1181) | +1.6% (n=1143) | 4/4 | 2/4 | QUESTIONABLE | **DROP** |

---

## 2H.4 Surviving Cells

**Zero cells survive multi-regime holdout.**

All 8 cells from Step 2E failed ROBUST/LIKELY_ROBUST criteria when tested
across genuine market regime changes (2024 halving, 2024 ATH, 2025 correction).

**Implication:** S1 edge in Step 2D/2E was regime-specific, not generalizable.
Recommend: KILL S1 (remove from bot_loops.py dispatch).

---

## 2H.5 Dropped Cells

| Cell | Robustness | Positive slices | Notes |
|------|------------|----------------|-------|
| BTC_r3_t10 | REGIME_LUCK | 0/4 | |
| BTC_r3_t11 | REGIME_LUCK | 0/4 | |
| BTC_r4_t10 | REGIME_LUCK | 0/4 | |
| ETH_r3_t13 | QUESTIONABLE | 2/4 | |
| ETH_r4_t9 | REGIME_LUCK | 0/4 | |
| ETH_r4_t10 | REGIME_LUCK | 0/4 | |
| ETH_r4_t11 | REGIME_LUCK | 1/4 | |
| XRP_r3_t12 | QUESTIONABLE | 2/4 | |
