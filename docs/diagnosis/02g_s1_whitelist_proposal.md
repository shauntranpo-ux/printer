# Step 2G Diagnosis: S1 Cell Whitelist Proposal (NO IMPLEMENTATION)

**Date:** 2026-05-07
**Status:** PROPOSAL ONLY - awaiting explicit user approval before any code change

---

## Surviving Cells (after Steps 2D + 2E)

12 cells qualified in Step 2D. 8 survived the Step 2E holdout filter:

| Cell | Asset | Dist bucket | T (min) | EV | Robustness | Vol gate? |
|------|-------|-------------|---------|-----|------------|-----------|
| BTC_r3_t10 | BTC | 0.3-0.4% | 10 | +2.6% | LIKELY_ROBUST | FAIL (1.63) |
| BTC_r3_t11 | BTC | 0.3-0.4% | 11 | +1.1% | ROBUST | FAIL (1.71) |
| BTC_r4_t10 | BTC | 0.4-0.5% | 10 | +1.2% | LIKELY_ROBUST | FAIL (1.26) |
| ETH_r3_t13 | ETH | 0.3-0.4% | 13 | +2.8% | LIKELY_ROBUST | PASS (1.86) |
| ETH_r4_t9  | ETH | 0.4-0.5% | 9  | +1.9% | ROBUST | FAIL (1.33) |
| ETH_r4_t10 | ETH | 0.4-0.5% | 10 | +1.1% | ROBUST | FAIL (1.41) |
| ETH_r4_t11 | ETH | 0.4-0.5% | 11 | +3.0% | LIKELY_ROBUST | FAIL (1.47) |
| XRP_r3_t12 | XRP | 0.3-0.4% | 12 | +2.1% | LIKELY_ROBUST | PASS (1.75) |

**Dropped from Step 2D:** BTC_r3_t13 (QUESTIONABLE), ETH_r4_t12 (QUESTIONABLE),
SOL_r3_t13 (QUESTIONABLE), XRP_r4_t9 (QUESTIONABLE).

---

## Critical Pre-Condition: Vol Gate Conflict

**The whitelist cannot be implemented as a simple "additional filter" on top of the existing vol gate.** Step 2F confirmed:

- Vol gate formula: `vol_ratio = rv x sqrtt_min / abs_pct >= 1.80`
- For dist_row 3 (abs_pct ~ 0.0035) and dist_row 4 (abs_pct ~ 0.0045):
  - BTC rv ~ 0.0018/min, ETH rv ~ 0.0020/min
  - Max vol_ratio at t=13: BTC_r3 = 1.86 (barely passes), ETH_r4 = 1.65 (fails)
  - At t=10: BTC_r3 = 1.63 (fails), ETH_r4 = 1.41 (fails)

6 of 8 surviving cells fail the vol gate. Adding a whitelist check before the vol gate
(as described in the spec) with vol gate still at 1.80 = zero new live trades.

**Three design options - user must choose:**

### Option A: Whitelist Bypasses Vol Gate (RECOMMENDED)

If a cell is whitelisted, skip the vol gate entirely and proceed to EV gate.
If a cell is not whitelisted, the existing code path (vol gate -> EV gate) runs unchanged.

```
strategy_brain_s1():
    compute dist_row, t_min
    if (asset, dist_row, t_min) in whitelist:
        skip vol gate
        -> go to EV gate (with EV threshold = 0 for whitelisted cells)
    else:
        -> vol gate -> EV gate (existing behavior)
```

**Trade-off:** Whitelist cells bypass a risk filter. Risk is bounded by the EV gate (which can
be set to 0%). The OOS data (n=102-233 per cell) provides statistical justification for bypass.

### Option B: Per-Cell Vol Gate Override

Store a per-cell `vol_gate_override` in the whitelist config. For cells where the vol gate is
too strict, override with a lower threshold (e.g., 1.20 for dist_row 4).

**Trade-off:** More granular control but more config parameters to maintain.

### Option C: Lower Vol Gate for Dist Rows 3-4 Only

Add a per-dist-row vol gate threshold map. For dist_rows 3-4, use 1.20 instead of 1.80.

**Trade-off:** Simpler than Option B but less targeted. Would allow other non-whitelisted cells
in those dist rows through.

---

## 2G.1 Proposed Config Schema

Add to `config.json` (or `asset_overrides` section):

```json
{
  "s1_whitelist_enabled": true,
  "s1_whitelist_bypass_vol_gate": true,
  "s1_cell_whitelist": [
    {"asset": "BTC", "dist_row": 3, "t_min_range": [10, 11]},
    {"asset": "BTC", "dist_row": 4, "t_min_range": [10, 10]},
    {"asset": "ETH", "dist_row": 3, "t_min_range": [13, 13]},
    {"asset": "ETH", "dist_row": 4, "t_min_range": [9, 11]},
    {"asset": "XRP", "dist_row": 3, "t_min_range": [12, 12]}
  ]
}
```

`t_min_range: [lo, hi]` means t_min ∈ [lo, hi] inclusive (e.g., [10, 11] covers t=10 and t=11).
`s1_whitelist_bypass_vol_gate: true` implements Option A above.

---

## 2G.2 Required bot_strategy.py Change

**Function:** `strategy_brain_s1()` at `bot_strategy.py:413`

The change introduces a whitelist gate BEFORE the vol gate, with bypass behavior:

```python
def strategy_brain_s1(asset, btc_price, strike, mins_left, yes_bid, yes_ask,
                      no_bid, no_ask, rv, config, ...):
    # ... existing distance and win prob calculations ...

    abs_pct = abs(btc_price - strike) / strike
    dist_row = _dist_row(abs_pct)
    t_min = int(round(mins_left))

    # NEW: whitelist gate
    whitelist = getattr(config, "s1_cell_whitelist", None)
    whitelist_enabled = getattr(config, "s1_whitelist_enabled", False)
    bypass_vol = getattr(config, "s1_whitelist_bypass_vol_gate", False)
    in_whitelist = False

    if whitelist_enabled and whitelist:
        for entry in whitelist:
            if (entry["asset"] == asset and
                    entry["dist_row"] == dist_row and
                    entry["t_min_range"][0] <= t_min <= entry["t_min_range"][1]):
                in_whitelist = True
                break

    if whitelist_enabled and not in_whitelist:
        return {"action": "skip", "reason": "not_in_whitelist"}

    # Vol gate - skip if whitelisted and bypass_vol_gate is set
    if not (bypass_vol and in_whitelist):
        vol_ratio = rv * math.sqrt(mins_left) / abs_pct if abs_pct > 0 else 0
        if vol_ratio < config.vol_gate_thresh_s1:
            return {"action": "skip", "reason": f"vol_gate|vol_ratio={vol_ratio:.2f}"}

    # ... EV gate and rest of existing logic unchanged ...
```

**Files touched:** `bot_strategy.py` only (1 function, ~20 lines added before vol gate check).
**No changes to:** `bot_loops.py`, `bot_risk.py`, `bot_infra.py`, `config.json` (until approved).

---

## 2G.3 Estimated Live Trade Frequency

From OOS data (67 days, 2026-02-25 to 2026-05-03):

| Asset | Cells | Total n in OOS | n/day |
|-------|-------|----------------|-------|
| BTC   | 3 (r3t10, r3t11, r4t10) | 537 | 8.0 |
| ETH   | 4 (r3t13, r4t9, r4t10, r4t11) | 761 | 11.4 |
| XRP   | 1 (r3t12) | 175 | 2.6 |
| **Total** | **8** | **1473** | **22.0** |

22 signal opportunities per day ~ 2-3 per hour in a 15-minute market cycle.
Not all will pass the EV gate even with vol gate bypassed. Expect 10-15 actual
trades per day (assuming ~60% of opportunities have EV > min_ev_base_s1 after
accounting for real Kalshi prices deviating from BV3 estimates).

**Risk check vs current limits:**
- `daily_loss_limit_dollars`: likely 500 (from daily_analysis risk_warnings)
- At $10-$20 per trade x 10-15 trades/day -> $100-$300 daily exposure if all lose
- 10-15 trades/day is within `max_consecutive_losses=5` guard assuming mixed results
- **No risk limit changes needed.** Trade frequency fits within existing guards.

---

## 2G.4 What Is NOT Proposed

- No change to BV3 table values
- No change to vol_gate_thresh (stays at 1.80 for non-whitelisted cells)
- No change to min_ev_base_s1 thresholds
- No change to S2 (FifteenMinStrategy) logic or config
- No backfilling of historical trade records
- No changes to bot_loops.py dispatch order

---

## Summary: Decision Required

| Question | Options |
|----------|---------|
| Vol gate handling for whitelisted cells | A (bypass - recommended), B (per-cell override), C (dist-row override) |
| Whitelist scope | 8 cells as proposed, or narrower (ETH-only, ROBUST-only) |
| EV threshold for whitelisted cells | 0% (any positive EV trades), or keep existing min_ev_base_s1 |
| Deploy timing | Next session once code reviewed, or needs more data first |

**No code has been written. Awaiting explicit approval.**
