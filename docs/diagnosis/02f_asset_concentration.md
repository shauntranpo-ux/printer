# Step 2F Diagnosis: Per-Asset Edge Concentration

**Date:** 2026-05-07

---

## 2F.1 Per-Asset Breakdown (12 qualifying cells)

| Asset | Qualifying cells | Total n | Avg EV | Cells (dist, t_min) |
|-------|----------------|---------|--------|---------------------|
| BTC | 4 | 619 | +1.40% | r3t10, r3t11, r3t13, r4t10 |
| ETH | 5 | 887 | +2.28% | r3t13, r4t9, r4t10, r4t11, r4t12 |
| SOL | 1 | 264 | +1.10% | r3t13 |
| XRP | 2 | 312 | +1.20% | r3t12, r4t9 |

---

## 2F.2 Edge Distribution

Edge is **generic across assets** — 4/4 assets have ≥1 qualifying cell.

Asset breakdown:

- **BTC**: 4 qualifying cells, dist bucket 0.3-0.5%
- **ETH**: 5 qualifying cells, dist bucket 0.3-0.5%
- **SOL**: 1 qualifying cells, dist bucket 0.3-0.5%
- **XRP**: 2 qualifying cells, dist bucket 0.3-0.5%

Concentration pattern: qualifying cells cluster at **dist_row 3–4 (0.3–0.5%)**
and **t_min 9–13** for all assets. No asset has cells at row 0-2 or t_min < 9.
Edge is regime-generic (all assets) but cell-concentrated (specific dist+time bucket).

---

## 2F.3 BTC Gap-Failed Cells vs Qualifying Avg EV

Two BTC cells passed EV and n criteria but were dropped in Step 2D for |gap| > 10pp:

| Cell | n | Empirical WR | BV3 pred | Gap | EV | Status |
|------|---|-------------|---------|-----|----|--------|
| BTC_r3_t12 | 153 | 90.8% | 77.8% | -13.1% | +6.0% | DROPPED (gap > 10pp) |
| BTC_r2_t13 | 348 | 83.9% | 71.3% | -12.6% | +5.6% | DROPPED (gap > 10pp) |

BTC avg EV (4 qualifying cells only): **+1.40%**
BTC avg EV (including 2 gap-failed cells, n=6): **+2.87%**

Gap-failed cells inflate BTC avg EV from +1.4% to +2.9%. These cells are
excluded because the -12 to -13pp gap signals BV3 is badly miscalibrated there
(empirical win rate far exceeds model prediction). Whether that's real edge or
statistical noise in 153-348 samples is unknown without more data.

---

## 2F.4 Cross-reference: OOS Edge vs S2 Live Paper (2026-04-15)

**Note:** `daily_analysis/2026-04-15.json` grades reflect **S2** (FifteenMinStrategy)
performance, not S1. S1 has never fired a live trade. Comparison is indirect.

| Asset | S1 OOS avg EV | S2 live grade | S2 live PnL | Agreement |
|-------|-------------|--------------|------------|-----------|
| BTC | +1.40% | D | $-18.5 | DISAGREE (OOS edge, but S2 live negative) — likely S1 vs S2 difference |
| ETH | +2.28% | B | $+12.3 | AGREE (both positive) |
| SOL | +1.10% | C | $-3.2 | DISAGREE (OOS edge, but S2 live negative) — likely S1 vs S2 difference |
| XRP | +1.20% | B | $+8.9 | AGREE (both positive) |

**Interpretations:**

- ETH: S1 has 5 qualifying OOS cells (highest count), S2 also grades B. Agrees.
- XRP: S1 has 2 qualifying cells, S2 grades B. Broadly agrees.
- BTC: S1 has 4 qualifying cells (avg EV +1.4%), S2 grades D. DISAGREES.
  → Likely explanation: S1 and S2 use opposite direction logic on BTC.
  → S1 is continuation-only; S2 is often contrarian. One can be profitable
    while the other loses on the same underlying markets.
- SOL: S1 has 1 qualifying cell (SOL dropped in 2E holdout). S2 grades C.
  → Borderline agreement: neither strategy shows strong edge on SOL.

---

## 2F.5 Critical Finding: Vol Gate Conflict

**All qualifying cells will be blocked by the vol gate even with the whitelist.**

The vol gate threshold is 1.80: `vol_ratio = rv × √t_min / abs_pct ≥ 1.80`

Expected vol_ratio for qualifying cells (using typical realized vol per asset):

| Cell | Est. vol_ratio | Passes vol gate? |
|------|----------------|-----------------|
| BTC_r3_t10 | 1.63 | NO (vol_ratio=1.63) |
| BTC_r3_t11 | 1.71 | NO (vol_ratio=1.71) |
| BTC_r4_t10 | 1.26 | NO (vol_ratio=1.26) |
| ETH_r3_t13 | 2.06 | YES |
| ETH_r4_t9 | 1.33 | NO (vol_ratio=1.33) |
| ETH_r4_t10 | 1.41 | NO (vol_ratio=1.41) |
| ETH_r4_t11 | 1.47 | NO (vol_ratio=1.47) |
| XRP_r3_t12 | 2.18 | YES |

**Result:** All 8 surviving cells fall BELOW the 1.80 vol gate threshold.
Adding a whitelist before the vol gate (as an additional filter) would have
no practical effect — vol gate would still block all whitelisted cells.

The 2G proposal must address this. Options:
1. Whitelist bypasses the vol gate (cells in whitelist skip vol gate check)
2. Per-cell vol_gate_thresh override stored in whitelist config
3. Lower vol_gate_thresh globally to ~1.20 for whitelisted dist_rows only

This is a design decision requiring explicit user approval in Step 2G.
