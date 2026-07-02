# Step 2B Diagnosis: S2 (FifteenMinStrategy) Calibration Audit

**Date:** 2026-05-07
**Total S2 trades:** 90 (paper mode, 2026-03-31 to 2026-04-16)

---

## TL;DR

S2 data splits into two fundamentally different cohorts with different data quality:

| Cohort | Dates | Trades | Win Rate | Data Quality |
|--------|-------|--------|----------|-------------|
| Phase 1 (real market) | 2026-03-31 | 12 | 0.0% | confidence_score present |
| Phase 2 (settlement records) | 2026-04-01+ | 78 | 60.3% | confidence_score NULL |

**Phase 1 verdict: CATASTROPHIC - 0% win rate on real market**
**Phase 2 verdict: BELOW_BREAKEVEN** (need 77.4% WR to break even at avg 72c entry)

Meaningful calibration is not possible with 78/90 null confidence_score rows.

---

## 2B.1 Phase 1 - Real Market Paper Trades (2026-03-31)

These 12 trades have real time-series data: entry price, seconds remaining, stop-losses.

| Timestamp | Side | N | Entry | Conf | SecsLeft | Outcome | EV_model | Exit |
|-----------|------|---|-------|------|----------|---------|---------|------|
| 2026-03-31T06:33:57 | no | 16 | 43c | 59 | 662 | loss | +0.090 | stop_loss |
| 2026-03-31T06:43:18 | no | 64 | 31c | 80 | 102 | loss | +0.420 | stop_loss |
| 2026-03-31T06:51:38 | no | 111 | 18c | 36 | 501 | loss | +0.110 | stop_loss |
| 2026-03-31T06:52:19 | no | 6 | 17c | 34 | 460 | loss | +0.100 | stop_loss |
| 2026-03-31T06:58:07 | no | 100 | 2c | 20 | 112 | loss | +0.110 | stop_loss |
| 2026-03-31T07:58:09 | yes | 5 | 46c | 71 | 112 | voided | +0.180 | unconfirmed_fill |
| 2026-03-31T09:42:30 | no | 33 | 30c | 69 | 151 | voided | +0.320 | unconfirmed_fill |
| 2026-03-31T10:26:30 | no | 39 | 23c | 66 | 211 | voided | +0.360 | unconfirmed_fill |
| 2026-03-31T11:11:20 | yes | 100 | 10c | 82 | 220 | voided | +0.650 | unconfirmed_fill |
| 2026-03-31T11:12:13 | yes | 25 | 39c | 67 | 168 | voided | +0.210 | unconfirmed_fill |
| 2026-03-31T15:56:45 | no | 41 | 24c | 67 | 196 | voided | +0.360 | unconfirmed_fill |
| 2026-03-31T16:50:44 | yes | 7 | 29c | 60 | 556 | voided | +0.240 | unconfirmed_fill |

### Phase 1 Summary

| Metric | Value |
|--------|-------|
| Total | 12 |
| Settled | 5 (0 win, 5 loss) |
| Voided | 7 |
| Win rate | 0.0% |
| Phase 1 PnL | $-30.43 |
| Mean predicted (conf/100) | 0.458 |
| Actual WR | 0.000 |
| Calibration gap | +0.458 (model +overconfident) |

Phase 1 outcome: **0 wins, 5 losses, 7 voided.**
All 5 settled trades were losses (stop_loss triggered). 7 markets voided.
This represents S2's real-time performance on 2026-03-31 only.

### Phase 1 Stop-Loss Analysis

All 5 Phase 1 losses were triggered by `stop_loss` exit. The stop-loss
pattern suggests S2 entered at cheap (< 50c) NO contracts and the market moved against
quickly. Entry prices range from 2c to 43c, indicating deep-contrarian entries where
S2 was betting against a strong directional trend.

---

## 2B.2 Phase 2 - Auto-Settlement Records (2026-04-01+)

These 78 records appear to be auto-generated settlement entries:
- All have entry_price_cents = 72 and contracts = 1
- Timestamps are exact hour boundaries (10:00:00, 11:00:00, etc.)
- confidence_score = 0 or NULL (not from live model evaluation)
- seconds_left_at_entry = 0 or NULL

These are NOT real-time paper trades. They represent held positions that settled -
likely the S2 bot was running and placing orders that got filled, held to expiry,
and settled automatically. The entry_price=72c with 1 contract each is suspicious
and may indicate a paper trading simulation artifact.

### Phase 2 Statistics

| Metric | Value |
|--------|-------|
| Total | 78 |
| Settled | 78 (47 win, 31 loss) |
| Win rate | 60.3% |
| Avg entry price | 72c |
| Breakeven WR at 72c | 77.4% |
| Phase 2 PnL | $136.36 |

**Win rate 60.3% vs breakeven 77.4% -> Phase 2 is unprofitable on paper.**

### Phase 2 Side Distribution

| Side | Trades | Wins | Losses | Win Rate |
|------|--------|------|--------|---------|
| yes | 78 | 47 | 31 | 60.3% |

---

## 2B.3 Combined Calibration Assessment

| Metric | All 90 trades | Phase 1 only (n=5) |
|--------|---------------|------------------------------|
| Win rate | 56.6% | 0.0% |
| Total PnL | $105.93 | $-30.43 |
| Model calibration | NOT MEASURABLE (78 null conf) | Gap = +0.458 |

**Full calibration is impossible.** 78/90 trades have null `confidence_score`
(the model's predicted probability). Without this field, we cannot compute:
- Reliability curves (predicted vs actual WR by probability bin)
- Brier score or log loss
- Per-asset calibration breakdown
- Confidence-conditional EV estimates

---

## 2B.4 Data Quality Assessment

| Field | Non-null | Null | Coverage |
|-------|---------|------|---------|
| confidence_score | 12 | 78 | 13% |
| seconds_left_at_entry | 12 | 78 | 13% |
| trade_amount_dollars | 12 | 78 | 13% |
| entry_price_cents | 90 | 0 | 100% |
| outcome | 90 | 0 | 100% |
| pnl_dollars | 83 | 7 | 92% (7 voided=0) |

**Root cause of null fields:** Phase 2 records are not real-time paper trades.
They appear to be settlement records written at trade resolution time (exact-hour
timestamps), missing all pre-trade metadata. The schema supports the field but the
write path for settlement-settled positions doesn't populate it.

---

## 2B.5 Verdict and Recommendations

**Verdict: INSUFFICIENT_DATA for calibration. Phase 1 shows 0% live WR.**

**Critical fixes required before meaningful Step 2B analysis:**

1. **Fix the market_log write path** to always populate:
   - `confidence_score` (model's predicted win_prob x 100)
   - `seconds_left_at_entry` (time remaining at fill)
   - `trade_amount_dollars` (actual dollars committed)

2. **Fix exit_reason logging** for settlement-settled positions.
   Currently "order not filled" or NULL for Phase 2 positions held to expiry.

3. **Accumulate more Phase 1 trades** (real-time paper trades with full data).
   12 trades in one day is insufficient for calibration. Need >= 100 per asset.

4. **Phase 1's 0% WR on 5 settled trades** is a red flag:
   - All entries were contrarian (2-43c for NO/YES against trend)
   - Stop-losses triggered immediately in all cases
   - S2 may be entering too early (6-11 min before expiry) in trending markets
   - The FifteenMinStrategy contrarian approach needs separate evaluation

Once data logging is fixed and >= 100 trades per asset accumulate, run this script
again to compute the full reliability diagram and calibration gap.
