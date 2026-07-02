# Step 2E Diagnosis: Time-Sliced Holdout Verification

**Date:** 2026-05-07
**Input:** 12 qualifying cells from Step 2D
**Surviving cells:** 8

---

## Data Constraint Note

The originally specified 4-slice window (2024-01-01 to 2026-04-09) does not
exist in settlement files. All available data covers 2026-02-25 to 2026-05-03
(67 days). The 4 slices below divide this window equally (~17 days each).
This limits regime-independence testing: all slices are in the same bull/bear
period. Treat ROBUST/LIKELY_ROBUST as 'internally consistent' not 'multi-regime'.

| Slice | Date range | Days |
|-------|-----------|------|
| Slice1 (Feb25-Mar13) | 2026-02-25 to 2026-03-14 | 17 |
| Slice2 (Mar14-Mar31) | 2026-03-14 to 2026-03-31 | 17 |
| Slice3 (Mar31-Apr17) | 2026-03-31 to 2026-04-17 | 17 |
| Slice4 (Apr17-May03) | 2026-04-17 to 2026-05-04 | 17 |

---

## Per-Cell Robustness Table

EV = empirical_win_rate - BV3_pred - 0.07 (using BV3 as entry price proxy)

| Cell | n_total | Slice1 EV | Slice2 EV | Slice3 EV | Slice4 EV | Pos slices | Robustness | Decision |
|------|---------|-----------|-----------|-----------|-----------|------------|------------|---------|
| BTC_r3_t10 | 233 | -0.5% (n=84) | +4.4% (n=65) | +4.8% (n=48) | +3.4% (n=36) | 3/4 | LIKELY_ROBUST | **KEEP** |
| BTC_r3_t11 | 174 | +0.3% (n=81) | +1.6% (n=41) | +1.7% (n=31) | +1.9% (n=21) | 4/4 | ROBUST | **KEEP** |
| BTC_r3_t13 | 110 | -0.7% (n=51) | -0.3% (n=26) | +5.3% (n=22) | +0.7% (n=11) | 2/4 | QUESTIONABLE | **DROP** |
| BTC_r4_t10 | 102 | +0.2% (n=51) | -1.6% (n=26) | +6.1% (n=16) | +6.1% (n=9) | 3/4 | LIKELY_ROBUST | **KEEP** |
| ETH_r3_t13 | 211 | +6.7% (n=82) | +1.1% (n=45) | +1.7% (n=58) | -4.2% (n=26) | 3/4 | LIKELY_ROBUST | **KEEP** |
| ETH_r4_t9 | 211 | +2.4% (n=87) | +1.0% (n=54) | +2.5% (n=46) | +0.5% (n=24) | 4/4 | ROBUST | **KEEP** |
| ETH_r4_t10 | 201 | +0.2% (n=85) | +0.1% (n=50) | +3.8% (n=43) | +1.8% (n=23) | 4/4 | ROBUST | **KEEP** |
| ETH_r4_t11 | 138 | -1.2% (n=56) | +4.2% (n=38) | +6.2% (n=30) | +9.5% (n=14) | 3/4 | LIKELY_ROBUST | **KEEP** |
| ETH_r4_t12 | 126 | +8.5% (n=55) | -6.1% (n=33) | +8.1% (n=25) | -11.0% (n=13) | 2/4 | QUESTIONABLE | **DROP** |
| SOL_r3_t13 | 264 | +2.0% (n=118) | -4.5% (n=64) | -3.3% (n=54) | +18.9% (n=28) | 2/4 | QUESTIONABLE | **DROP** |
| XRP_r3_t12 | 175 | +3.0% (n=82) | +0.2% (n=60) | +3.1% (n=33) | n/a (n=0) | 3/4 | LIKELY_ROBUST | **KEEP** |
| XRP_r4_t9 | 137 | +4.7% (n=56) | -6.2% (n=55) | +4.7% (n=26) | n/a (n=0) | 2/4 | QUESTIONABLE | **DROP** |

---

## Surviving Cells

8 cells pass holdout filter (ROBUST or LIKELY_ROBUST):

| Cell | Asset | Dist bucket | T (min) | Robustness |
|------|-------|-------------|---------|------------|
| BTC_r3_t10 | BTC | 0.3-0.4% | 10 | LIKELY_ROBUST |
| BTC_r3_t11 | BTC | 0.3-0.4% | 11 | ROBUST |
| BTC_r4_t10 | BTC | 0.4-0.5% | 10 | LIKELY_ROBUST |
| ETH_r3_t13 | ETH | 0.3-0.4% | 13 | LIKELY_ROBUST |
| ETH_r4_t9 | ETH | 0.4-0.5% | 9 | ROBUST |
| ETH_r4_t10 | ETH | 0.4-0.5% | 10 | ROBUST |
| ETH_r4_t11 | ETH | 0.4-0.5% | 11 | LIKELY_ROBUST |
| XRP_r3_t12 | XRP | 0.3-0.4% | 12 | LIKELY_ROBUST |

---

## Dropped Cells

- `BTC_r3_t13` - QUESTIONABLE (2/4 slices positive EV)
- `ETH_r4_t12` - QUESTIONABLE (2/4 slices positive EV)
- `SOL_r3_t13` - QUESTIONABLE (2/4 slices positive EV)
- `XRP_r4_t9` - QUESTIONABLE (2/4 slices positive EV)
