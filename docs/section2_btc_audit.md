# Section 2 Audit: BTC Decision Flow in printer_brain()

**Source:** `bot.py`  
**Purpose:** Spec for BTCStrategy - replicate legacy BTC behavior on the new foundation.

---

## _brain_cal (line 164)

Module-level dict, updated every 5 completed trades by `calibrate_from_history()`.

| Field | Default | Meaning |
|---|---|---|
| `prob_scale` | `1.0` | Multiplies `(win_prob - 0.50)` to shrink/stretch edge |
| `min_edge_override` | `None` | Overrides min_ev if set |
| `confidence_bonus` | `0` | Added to confidence int (reward bonus) |
| `reward_tier` | `0` | 0=none 1=>50%WR 2=>75%WR 3=>85%WR |
| `overall_wr` | `0.0` | Overall win rate from recent trades |
| `bullish_wr` / `bearish_wr` | `0.5` | Per-direction WR; sub-0.35 penalizes EV by -4% |

---

## calculate_momentum() (line 1219)

- **Input:** price deque (defaults to global `btc_prices`; printer_brain passes `asset_manager._prices.get(asset, btc_prices)`)
- **Window:** 180 seconds from `time.time()`
- **Logic:** finds oldest price within cutoff, computes `(current - oldest) / oldest`
- **Threshold:** `> 0.005` → bullish, `< -0.005` → bearish, else neutral
- **Returns:** `(pct_change: float, label: str)`

---

## contract_velocity() (line 1301)

- **Input:** `ticker: str`, `side: str` (usually "yes")
- **Data source:** `_contract_price_history[ticker]` - a `deque(maxlen=30)` populated by `track_contract_price()` each loop
- **Logic:** compares oldest vs newest YES ask price in history; `change = (newest - oldest) / oldest`
- **For YES buys:** falling price (`change < -0.05`) = favorable; rising = unfavorable
- **For NO buys:** rising price = favorable (NO getting cheaper); falling = unfavorable
- **Returns:** `"favorable"`, `"neutral"`, or `"unfavorable"`

---

## printer_brain() - 9 steps (line 1983)

**Inputs:** btc_price, strike, yes_ask, no_ask, elapsed_seconds, secs_left, ticker, + config params  
**Output:** dict with keys: action, side, confidence, reasoning, key_signals, win_prob, mom_label, mom_pct, vel_signal, mins_left, abs_pct, above, _rv, _vol_ratio, price_filter_skip

### Step 0 - Realized vol gate
Uses `btc_realized_vol()` (std of 1-min returns over 10 min). If `vol * sqrt(mins_left) / abs_pct >= vol_gate_thresh` (default 1.80) → `_vol_skip = True`. Applied after EV gate order in final skip logic.

### Step 1 - BV3 raw win probability
`win_prob_raw = _win_prob_for_asset(asset, abs_pct, mins_left)`. BTC routes to `_empirical_win_prob()` which interpolates `_BV3_TABLE` by distance bucket and time, blending with live correction data (up to 40% weight at 50+ samples).

### Step 2 - Momentum adjustment (flat ±5%)
If bullish: `+0.05` when above strike, `-0.05` when below.  
If bearish: `+0.05` when below strike, `-0.05` when above.  
Neutral: 0. No strength multiplier.

### Step 3 - Contract velocity adjustment (±1%)
`+0.01` if favorable, `-0.01` if unfavorable, `0` if neutral.

### Step 4 - Calibration scale
`win_prob = 0.50 + (raw + mom_adj + vel_adj - 0.50) * _brain_cal["prob_scale"]`  
Clamped to `[0.10, 0.997]`.

### Step 5 - YES/NO split
`prob_yes = win_prob if above else (1 - win_prob)`.

### Step 5b - Market-implied anchor blend *(not replicated in Section 2)*
When model diverges >25pp from market implied price, blends toward market (0-50% weight). This is the main expected source of A/B disagreement.

### Step 6 - EV calculation
`yes_ev = prob_yes - yes_ask/100 - kalshi_fee`  
`no_ev = prob_no - no_ask/100 - kalshi_fee`  
`bullish_wr < 0.35` penalizes yes_ev by -4%; `bearish_wr < 0.35` penalizes no_ev.

### Step 7 - Continuation-side selection (always)
If above: side=yes, ev=yes_ev. If below: side=no, ev=no_ev. **No bidirectional choice.**

### Step 7b - Entry price hard filters
`max_entry_price_cents`, `min_reward_cents`, `max_risk_reward_ratio` - skip if violated.

### Step 8 - EV gate
`min_ev = min_ev_base/100 + _session_ev_adjustment()` (session adjustment is hardcoded 0.0).  
Skip if `best_ev < min_ev` or vol skip or price filter skip.

### Step 9 - Output
`confidence = int(true_p * 100) + _brain_cal["confidence_bonus"]`

---

## Deviations in BTCStrategy (Section 2)

The following legacy behaviors are **not replicated** in the Section 2 canary:

1. **Market-implied anchor blend (step 5b)** - not carried over; expected to cause A/B disagreement in high-divergence scenarios
2. **Entry price hard filters (step 7b)** - handled by SkipLayer/EV layer but with different thresholds
3. **Direction-based EV penalty** (bullish_wr/bearish_wr penalties) - not replicated

These are intentional omissions: the canary tests foundation plumbing, not exact numeric parity. Exact parity is the goal of later calibration sections.
