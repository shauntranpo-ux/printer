# BV3 Data Leakage Warning

## What is BV3?

Brain v3 (`_BV3_TABLE` in `bot.py`) is an empirical win-probability lookup table
built from 4.5 million BTC 1-minute rows spanning **2017–2026**.

It maps `(abs_pct_distance_from_strike, minutes_remaining)` → `win_probability`.

## The Problem

The OOS holdout period (2023-01-01 onward, per `data/split_config.json`) is
**included in the dataset used to build the BV3 table**.

This means the BV3 table has already "seen" the OOS period:

- Win probabilities in `_BV3_TABLE` partially reflect price behavior from the
  OOS holdout window.
- When `run_oos_eval()` or walk-forward validation evaluates the OOS period, it
  calls `brain_decide()`, which uses `_BV3_TABLE` to look up win probabilities.
- This is **data leakage**: the model was trained on data it is being tested on.

## Impact

- **OOS win rate and Sharpe are overstated.** The strategy looks better on OOS
  data than it will in live trading because the BV3 table was fitted to that data.
- The magnitude of the bias is unknown without regenerating BV3 from pre-OOS
  data only and re-running the OOS evaluation.
- Monte Carlo and walk-forward results on the **train partition** are unaffected
  by this specific leakage (the train split is also included in BV3 construction,
  so the in-sample period is similarly contaminated — the comparison is consistent
  but not predictive of live performance).

## What "no leakage" would require

1. Build `_BV3_TABLE` **only** from data before the OOS start date
   (e.g., 2017-01-01 to 2022-12-31).
2. Re-run `run_oos_eval()` with the clean table.
3. The resulting OOS metrics are the only unbiased estimate of live performance.

Until this is done, **treat all reported OOS metrics as optimistic upper bounds**,
not reliable performance forecasts.

## Current status

BV3 regeneration from pre-OOS data has **not yet been performed**.
The derivation code is not currently in this repository.

All OOS metrics in `results/oos_report.json` and `results/wfv_report.json`
should be read with this caveat in mind.
