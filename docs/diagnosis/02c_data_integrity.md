# Step 2C Diagnosis: Trades Table Data Integrity

**Date:** 2026-05-07
**Verdict:** `SYNTHETIC_DATA`

---

## TL;DR

78 of 90 strategy2 trades in `kalshi_bot.db` are **synthetic test data**, not real bot
decisions. They are excluded from all future calibration analysis.

**Real S2 trade count: 12** (Phase 1, 2026-03-31 only).

---

## 2C.1 Finding: Source of the 78 Rows

### Identification Method

Market ID format reveals the source:

| Cohort | Market ID examples | Format |
|--------|-------------------|--------|
| Phase 1 (real) | `KXBTC15M-26MAR310245-45` | Real Kalshi market ticker |
| Phase 2 (synthetic) | `KXBTC-TEST-0` .. `KXBTC-TEST-77` | Sequential test IDs |

Real Kalshi 15-minute BTC market tickers follow the pattern `KXBTC15M-{date}{strike}-{minute}`.
The `KXBTC-TEST-{N}` format matches no real Kalshi market series.

### Supporting Evidence

| Field | Phase 1 (real) | Phase 2 (synthetic) |
|-------|---------------|---------------------|
| `market_id` | Real Kalshi ticker | `KXBTC-TEST-0` .. `KXBTC-TEST-77` |
| `market_title` | Populated | NULL |
| `order_id` | `paper_<ts>` or None | NULL |
| `confidence_score` | 20–82 | NULL |
| `seconds_left_at_entry` | 102–662 | NULL |
| `trade_amount_dollars` | $10–$20 | NULL |
| `entry_price_cents` | 2–46 (varies) | 72 (all identical) |
| `contracts` | 6–111 (varies) | 1 (all identical) |
| `ts` format | Real datetime | Exact hour boundary |

### Where the INSERT Came From

`db_write_trade` (the only write path to the `trades` table) is called only from:
- `bot_infra.py:316` — the main write path (called by `bot_loops.py` and `bot_risk.py`)

The Phase 2 rows were written via this path but with pre-populated `outcome`
and `exit_price_cents` fields, which the normal flow never sets on INSERT
(those are updated later via `db_update_trade`). This means the rows were
inserted by a script that called `db_write_trade` directly with pre-settled data.

The generator script is not in the current codebase. A search of all committed
Python files and the full git object history found no file containing `KXBTC-TEST`.
The script was either:
1. A deleted ad-hoc script run from an interactive shell (not committed), or
2. A one-off Python snippet executed via Claude Code during a prior session

The `KXBTC-TEST` pattern, sequential integer IDs, and exact hourly timestamps
are consistent with a synthetic data injector used to stress-test the dashboard
or calibration tooling before real paper-trade data accumulated.

---

## 2C.2 Verdict: SYNTHETIC_DATA

The 78 Phase 2 rows are synthetic test records. They:
- Have fake market IDs not matching any real Kalshi format
- Contain no real signal data (null confidence, seconds_left, trade_amount)
- Were all entered at 72c YES with 1 contract — obviously templated
- Have exact-hour timestamps matching a generated sequence, not real market activity

**These 78 rows MUST NOT be counted as bot performance.**

---

## 2C.3 Corrected S2 Trade Count

| Metric | Previous (incorrect) | Corrected |
|--------|---------------------|-----------|
| Total trades | 90 | 12 |
| Settled | 83 | 5 |
| Win rate | 56.6% | **0.0%** (0/5 settled) |
| Voided | 7 | 7 |
| Total PnL | +$105.93 | **-$30.43** |

S2 has **0 wins from 5 settled real trades** (all stop_loss exits on 2026-03-31).
7 additional trades were voided (unconfirmed fill).

---

## 2C.4 Logging Fix (Step 2C.5 — Not Applicable)

Since Phase 2 rows are synthetic (not real trades with logging bugs), no logging
fix is needed. The write path for real trades (`bot_loops.py:450`) correctly
populates all fields including `confidence_score`, `seconds_left_at_entry`, and
`trade_amount_dollars`. The null fields in Phase 2 are artifacts of the synthetic
generator, not a production logging bug.

**No code changes required.**

---

## 2C.5 Future Analysis

All subsequent calibration and EV analysis uses **Phase 1 only** (12 real trades):

| Date | Real trades | Wins | Losses | Voided |
|------|-------------|------|--------|--------|
| 2026-03-31 | 12 | 0 | 5 | 7 |

12 trades (5 settled) is insufficient for calibration. Meaningful S2 analysis
requires ≥ 100 resolved trades with `confidence_score` populated.
The immediate priority is accumulating real paper trade data, not calibrating on 5 trades.
