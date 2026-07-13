# Running the Strategy Lab

Operator guide for the eight-strategy paper lab. The old contents of this file (the
dashboard wiring punchlist) are obsolete - every endpoint has been wired since.

Eight strategies race on the same tape, all paper, all at the $25 clip:

| Slot | Thesis | Trades when |
|------|--------|-------------|
| S1 · Momentum (main) | A fresh, confirmed move continues | 3-10 min left, 30-75c entries |
| S2 · Favorite-Bias | Late favorites are underpriced | 2.5-6 min left, 70-88c favorites |
| S3 · Structural Arb | YES+NO asks < 93c = free money | any time a book dislocates (rare) |
| S4 · Mean-Reversion | Overextended moves snap back | a 2-sigma run that stalled |
| S5 · Maker Capture | Earn the spread, don't pay it | passive quote on 60-90c favorites |
| S6 · Window-Fade | Windows anti-persist: fade a decisive 2+ streak (56.4% on 5.3k historical pairs) | first 2 min, ~50c entries |
| S7 · Vol-Spike | Breakouts are underpriced when realized vol spikes | live vol >= 1.6x its anchor |
| S8 · Calm Favorite | Favorites are underpriced when vol collapses | live vol <= 0.6x its anchor |

## Daily 30-second check (Overview tab)

1. **Lab strip** (under the market strip): every slot should show a growing trade
   count and a P&L number. Drift in either direction is fine - it is data.
2. **A quiet slot** is not automatically broken. Open the Edge tab and read
   **Lab activity** in the Strategy Lab card:
   - many evals, zero trades -> healthy but selective. The top skip reasons say why
     (e.g. S3 `s3_no_arb` = no dislocated books appeared, expected most days;
     S6 `s6_no_prev_window` = window memory missing, should fade after the first hour).
   - **zero evals -> the dispatch is broken.** Check Railway logs.
3. Telegram daily summary lists all eight with per-strategy totals and the day winner.

## Weekly read (Edge tab leaderboard)

- **Net $/ct** - realized P&L per contract after fees on executed paper trades
  (current strategy versions only; voided maker quotes excluded). This is the ranking.
- **Wilson LB** - conservative lower bound; > 0 at 200+ trades = statistically real.
- **Verdict**: `collecting (n/200)` -> keep waiting. `proven: Wilson LB > 0` -> the
  promotion conversation. `positive but unproven` -> promising, keep collecting.
  `no edge` after ~200 trades -> retire it (see below).
- **Wk @$25/$100/$250** - what the measured edge would earn weekly at that clip IF it
  survives proof. Projections only appear for positive strategies.

## The honest $1,000/week math

Profit = edge per dollar x dollars traded. Nothing changes that.

| Measured edge (net $/ct) | @$25 clip | @$100 clip | @$250 clip |
|--------------------------|-----------|------------|------------|
| 2c, 10 trades/day        | ~$70/wk   | ~$280/wk   | ~$700/wk   |
| 4c, 10 trades/day        | ~$140/wk  | ~$560/wk   | ~$1,400/wk |
| 4c, 20 trades/day        | ~$280/wk  | ~$1,120/wk | ~$2,800/wk |

(Assumes ~50c mean entries; the dashboard computes the exact figure per strategy.)
The path: prove the edge at $25 -> raise the clip for the PROVEN strategy only.
Nothing in code ever sizes up on its own - that is an explicit config change you make.

## When a slot proves out

1. Confirm: 200+ trades, `proven: Wilson LB > 0`, and the projection row at your
   target clip clears your goal.
2. Raise `trade_amount_dollars` deliberately and in steps (e.g. 25 -> 100 -> target),
   watching that the edge survives the larger size (slippage grows with size).
3. Consider `maker_execution_enabled: true` first if the Edge tab's maker-vs-taker
   delta is positive - cheaper fees compound with any edge.

## When a slot flames out

1. `no edge` at ~200 trades -> set its enable key to `false` in config
   (`s3_arb_enabled` / `s4_revert_enabled` / `s5_maker_enabled` / `s6_carry_enabled` / `s7_volspike_enabled` / `s8_calm_enabled`).
2. Slot in a replacement idea: one brain function in `bot_strategy.py` + one entry in
   `bot_strategies.STRATEGY_REGISTRY` (S7/S8 slots are free). Labels, stats, API,
   dashboard, and Telegram pick it up automatically.

## Tuning knobs (config.json, all take effect without a deploy)

- S3: `s3_arb_max_combined_cents` (93) - how much guaranteed edge before firing.
- S4: `s4_min_sigma` (2.0), `s4_lookback_secs` (120), `s4_min_edge` (0.03),
  entry band `s4_min/max_entry_cents` (25/70).
- S5: `s5_mid_lo/hi` (0.60/0.90) favorite band, `s5_improve_cents` (1) quote depth.
- S6: `s6_window_secs` (120) entry window, `s6_min_prev_move` (0.0015) + `s6_min_streak`
  (2) the tuned decisive-move/streak gates, `s6_fade_premium` (0.064) the measured edge.
- S1: `s1_momentum_min_sigma` (1.0) - lower = more trades, noisier signal.
- S2: `s2_fav_mid_lo/hi` (0.70/0.88), `s2_fav_min_z` (0.8).
- S7: `s7_spike_ratio` (1.6) how abnormal vol must be, `s7_min_edge` (0.03).
- S8: `s8_calm_ratio` (0.6), `s8_mid_lo/hi` (0.55/0.85), `s8_min_z` (0.4).
- Duel: `strategy_duel_mode` (true) - S1+S2 may hold the same market on paper only.

Loosening gates buys more trades per day (faster to 200) at the cost of signal
quality; tighten again once a slot has its sample.

## Known quirks (verified in the pre-merge audit, deliberately deferred)

- Per-asset "today P&L" on the market tiles buckets by UTC day; the KPI Today tiles
  use the ET trading day. After 8pm ET the two disagree until midnight UTC.
- The equity curves / streak chip are built from the newest 500 trades across ALL
  modes - with the lab running they lean paper. The leaderboard is the honest view.
- When Kalshi's official result is late (>40s), decision_log outcomes are stamped
  from the spot estimate without an "estimated" marker (the trades row does carry
  `exit_reason=expiry_estimated`).
- Paper S2 rows have no orphan sweep (S1 and the lab slots do): a restart with a
  lost state file mid-window can leave one S2 row pending in the DB.

## Historical validation status (what the Feb-May settlement data already said)

- **S6 Window-Fade: PROVEN prior.** Fading a decisive (>=15bp) 2+ streak won 56.4%
  of 5,275 historical pairs, Wilson-LB 0.552 vs the 0.517 breakeven at 50c
  (`scripts/tune_fade.py`). The shipped gates encode exactly this.
- **Window-carry (S6's original direction): KILLED** - windows anti-persist
  (`scripts/backtest_carry.py`).
- **BTC-confirmation gate for S6: checked and REJECTED** - requiring BTC's
  same-window move to agree adds nothing (Wilson-LB 0.536 with vs 0.536 without),
  and BTC's window result does not lead the alts' next window
  (`scripts/xasset_check.py`).
- **S1 / S4 / S2-S8 favorite calibration: UNTESTED** - needs price data the
  settlement parquets don't carry. The two-step below fixes that.

## Validate S1/S4/S2 against history (one-time, run on your PC)

The hosted container cannot reach exchange APIs, so run these two commands from the
repo on your Windows machine (open internet, no API keys needed):

```
pip install pandas pyarrow requests
python scripts/fetch_candles.py     # ~5 min: 1-min candles for the settlement span
python scripts/backtest_signals.py  # momentum / stall-fade / calibration report
```

Then commit the new `data/historical/*_candles_1m.parquet` files and push - with the
candles in the repo, the analysis can be extended and re-run from any session. Read
the report the same way as the leaderboard: a thesis is real only where LB-excess
stays positive with n >= 1000.
