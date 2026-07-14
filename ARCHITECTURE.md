# Architecture: Money Printer Kalshi Bot

**If anything in README.md or pyproject.toml conflicts with this file, THIS file wins.**

Sources of truth: running code and `grep`. Every claim below is verified with a file:line citation. If you find a discrepancy, fix the doc-not the code.

---

## Live Bot (production)

Deployed on Railway. Trades real money via Kalshi's REST API. All production logic lives in **top-level flat `.py` files**. There is no package structure in the live bot.

| File | Purpose |
|---|---|
| `bot.py` | Async entry point. Bootstraps config, DB, price feeds, then delegates to `main_loop()` |
| `runner.py` | Process manager. Spawns `bot.py` worker instances, monitors crash rate, restarts sidecars |
| `server.py` | Flask dashboard (`GET/POST /api/*`). Reads DB and `bot_state.json`; never writes to DB |
| `bot_loops.py` | Main async market loop. Phase handler: watching -> ready -> trading -> cooldown |
| `bot_market.py` | Kalshi REST API client. Order placement, OBI calculation, fill polling |
| `bot_risk.py` | Preflight checks, trade execution, PnL tracking, S1 orphan settlement |
| `bot_strategy.py` | All strategy brains: S1 Momentum (main), S2 Favorite-Bias, and the lab slots S3 Structural-Arb / S4 Mean-Reversion / S5 Maker-Capture / S6 Window-Fade / S7 Vol-Spike / S8 Calm-Favorite |
| `bot_strategies.py` | Registry for the lab slots (S3+): brain callable, labels, enabled keys. Adding S7/S8 = one brain + one entry here |
| `bot_infra.py` | `config.json` read/write, sqlite3 `init_db()`, Telegram async helper |
| `bot_state.py` | Shared in-memory globals: price deques, API keys, trade state |
| `sessions.py` | Pure ET time-of-day session + weekday/weekend taxonomy; used by the session gate, `/api/edge`, and `edge_report` |
| `bot_stats.py` | Win rate calibration helpers used by S1/S2 brains |
| `asset_manager.py` | Coinbase price feed: websocket ticker channel (1s decimation) with REST-polling fallback + supervisor; populates price history deques |
| `price_validator.py` | Paper-mode: continuous price accuracy monitor vs Kalshi mid |
| `validate_and_report.py` | Live-mode: blocking GO/NO-GO preflight gate before bots start |
| `weekly_report.py` | Generates weekly PnL summary via Telegram |
| `collect_kalshi_ladder_history.py` | Downloads Kalshi order book ladder history for offline analysis |
| `backtest.py` | Offline backtest runner. **NOT loaded at runtime.** |
| `obs.py` | Structured JSON logging formatter and in-memory error ring buffer |
| `notify.py` | Telegram + `alerts.log` dispatcher; used by `runner.py` and `server.py` |

### Strategy brains (`bot_strategy.py`)

S1 and S2 are deliberately **opposite bets** so their per-strategy net-$ head-to-head is
meaningful (the "which profits more" comparison the dashboard Edge tab and the Telegram daily
"Day winner" surface). Both still price a **Bachelier fair value** for P(YES) (`_bachelier_p_above`)
off the shared vol engine, but they trade on opposite theses. In paper the **duel** runs both on
every market (`strategy_duel_mode`, default true) - opposing positions on the same ticker are
allowed since there is no capital conflict; setting it false restores the one-way "S1 blocks S2"
dedup in `handle_ready_phase`.

**Vol engine (`_sigma_eff`).** Primary path: a per-asset **market-implied sigma EWMA**
(`_implied_sigma_from_quote` / `update_implied_sigma`, fed opportunistically from orderbook
fetches the loop already makes) blended with live realized vol (`_live_sigma_15m`, quadratic
variation on a 15s resampled grid - the raw 1s feed cadence must be gridded before differencing
or every pair fails the dt filter) and clamped around the implied anchor. Cold start falls back
to the static `_ASSET_VOL_15M` table (re-fit 2026-07 to realistic values; the old 2.5-4x-inflated
table lives on as the frozen `_LEGACY_VOL_15M` for off-path legacy helpers). A per-asset
`sigma_scale` fitted from settled decisions multiplies the result. Anchoring sigma to the
market's own vol means fair value can disagree with the market only through spot freshness,
never through a vol opinion.

- **S1 - MOMENTUM / CONTINUATION (SOL / XRP / DOGE by default).** `_momentum_signal` measures
  the spot return over `s1_momentum_lookback_secs` (75s); a move counts only when it is real
  (`|r| >= s1_momentum_min_sigma` window-sigmas) AND still underway (a shorter sub-window agrees
  in sign - not reversing). Direction is the move's sign (up->YES, down->NO). It prices a
  continuation fair value on the spot **projected forward** by `s1_momentum_drift_lambda * r`,
  then trades the moving side against its own ask when `ev >= s1_min_edge` after fee - it does
  NOT shrink to the market mid. Entry band 30-75c (room to run). BTC-lead (`_asset_beta`) is a
  logged confirming input, hard-gated only when `s1_require_btc_confirm`. BTC/ETH off by default
  (`s1_btc_enabled` / `s1_eth_enabled`). Keeps the S1 caps / rate-limit / cooldown / window guard.
- **S2 - FAVORITE-BIAS HARVEST (BTC / SOL / XRP / DOGE).** Fires LATE (`s2_fav_time_min/max`,
  2.5-6 min) when the spot sits decisively past the strike (`|z| >= s2_fav_min_z`) with the last
  prints confirming it, and the favorite side's de-vigged mid is in the premium band
  (`s2_fav_mid_lo`..`s2_fav_mid_hi`, 0.70-0.88). It BUYS that favorite. There is **no**
  fair-value-disagreement gate - the edge is realized win-rate > price, not a model edge; a
  `s2_fav_max_model_shortfall` guard only vetoes favorites the Bachelier model strongly rejects.
  Round-trip spread cap and ETH-off (`s2_eth_enabled`) as before. This is the mirror risk
  profile to S1: high hit-rate, low payout.
- **s_fav (shadow, zero capital).** `shadow_fav_candidate` still logs a would-buy-the-favorite
  decision_log row (strategy `s_fav`, `would_trade=0`); now that S2 trades this thesis live the
  shadow measures a slightly wider untraded extension (`shadow_fav_enabled`).

### The strategy lab (S3-S8, paper-only)

Registry-driven test slots (`bot_strategies.STRATEGY_REGISTRY`) racing alongside S1/S2 on
every market, executed through the generic slot engine in `bot_risk`
(`_execute_slot_trade` / `_settle_slot_trades` / `_settle_slot_orphans` /
`_settle_slot_rollover`) with per-slot state in `bot_state._slot_state`. Slot trades are
**hard-forced `mode="paper"` inside the executor** - no code path lets a lab slot place a
live order. Dispatch happens in `handle_ready_phase` and both LOCKED-phase blocks;
settlement piggybacks on S2's expiry detection plus a generalized orphan sweep
(startup/300s/rollover).

- **S3 Structural Arb**: `check_dual_side_arb` fires when YES+NO asks < 93c -> buy BOTH
  sides (two trade rows, one economic trade sized so the pair outlay respects the clip).
  Guaranteed profit per pair; measures how often books dislocate that far.
- **S4 Mean-Reversion**: fade a >= `s4_min_sigma` (2.0) run over `s4_lookback_secs` once
  the last third shows it stalling (`s4_still_running` guards S1's setup). The mirror
  bet to S1 - together they answer trend-vs-revert.
- **S5 Maker Capture**: on a 0.60-0.90 favorite, record a passive quote 1c inside the
  ask (no order). Settlement scans the held-book path (`bot_state._maker_track`, now
  also appended during READY) - crossed -> filled at maker price + maker fee (~25% of
  taker); never crossed -> `outcome="unfilled"`, $0. Profit source is execution, not
  prediction.
- **S6 Window-Fade**: first `s6_window_secs` (120s) of a window only, FADE the PREVIOUS
  window's resolved direction (from `bot_state._prev_window_outcome`, written at every
  settlement AND estimated at every rollover, carrying a same-direction `streak`
  counter) at 40-60c entries. Thesis validated AND tuned on 25k real Kalshi
  settlements (`scripts/backtest_carry.py` + `scripts/tune_fade.py`): the fade rate is
  monotonic in previous-move size and streak length; the shipped gate (move >= 15bp,
  streak >= 2) fades 56.4% (Wilson-LB 0.552 vs ~0.517 breakeven, +4.6c/ct).
- **S7 Vol-Spike / S8 Calm-Favorite**: the volatility-regime mirror pair (`_vol_regime`:
  live realized sigma vs the implied-EWMA/static anchor). S7 buys the fresh move's
  direction only when live vol >= 1.6x anchor (spiked tape, book lags); S8 buys the
  favorite only when live vol <= 0.6x anchor (dead tape, favorite holds). Both fair-
  value with the LIVE sigma; regimes are mutually exclusive by construction.

Scoreboard: `/api/edge` returns a `leaderboard` (per-strategy net-$/contract, Wilson-LB,
verdict, and projected weekly $ at $25/$100/$250 clips - projections gated on positive
net and labeled as unproven below 200 picks; nothing in code ever sizes a strategy up).
The dashboard Strategy Lab card renders it; `scripts/edge_report.py` prints the same
ranking; the Telegram daily summary iterates all eight labels.

**Sizing** is quarter-Kelly scaled DOWN from the `trade_amount_dollars` clip (`_kelly_stake`;
never above the clip, floored at `min_stake_dollars`).

The legacy momentum/velocity/CA-lead helpers (`_s1_multitf_momentum`, `_s2_contract_direction`,
`_s1_certainty_win_prob`, `_s1_dislocation_check`, the `_S1_ASSET_CONFIG` / `_S2_ASSET_CONFIG`
dicts and the win-rate tables) are retained for tests/offline scripts but are **not on the live
decision path**. Both brains keep the prior return-dict shape (incl. `signals.model_raw_p_yes`)
so the loop, website, and the `decision_log` harness are unchanged.

---

## Live Dependencies

File: `requirements.txt`

Key runtime deps: `flask`, `gunicorn`, `aiohttp` (REST + the websocket price feed), `aiosqlite`. Standard library `sqlite3` for schema creation and dashboard queries.

**Not used at runtime:** `pyproject.toml` lists `fastapi`, `sqlalchemy`, `uvicorn`, `alembic`, `apscheduler`. Those are dependencies of the abandoned rewrite. See [Quarantined / Experimental](#quarantined--experimental).

---

## Live Entry Point

```
Procfile          web: bash start.sh            (start.sh:last line)
  └── start.sh
        ├── python runner.py   (background loop, auto-restarts)  (start.sh:3-6)
        └── gunicorn server:app --bind 0.0.0.0:${PORT:-5000}    (start.sh:8)

runner.py
  └── subprocess  python bot.py   (one instance per enabled strategy)  (runner.py:~103)
        └── bot.py -> bot_loops.main_loop()
```

- `runner.py` also spawns sidecars: `price_validator.py`, `collect_kalshi_ladder_history.py`, `weekly_report.py`.
- `runner.py` is **not** the gunicorn worker - gunicorn serves `server.py` independently.
- Both processes share the same Railway volume via `BOT_DB_FILE` env var.

---

## Live Database

Raw sqlite3. **No ORM. No Alembic migrations.**

- Schema defined in: `bot_infra.py:158` (`init_db()`)
- Tables: `trades`, `market_log`, `daily_summary`, `stress_test_results`, `wr_calibration`,
  `decision_log`, `maker_log`, `settlement_basis`
  - **`decision_log`** - the edge-measurement harness. One row per brain evaluation that
    reached the model/EV stage (`would_trade` = whether the gate would trade), with
    `model_p_yes`, `market_mid_p_yes`, `market_edge`, plus the sigma-observability columns
    `spot`, `strike`, `sigma_eff`, `z` (nullable, added via the try/except ALTER pattern);
    settlement `outcome` backfilled at expiry. Logs ALL evaluated decisions (not just taken
    trades) so signal edge is measured free of survivorship bias; a second dedup slot in
    `bot_loops._log_decision` guarantees the eventual `would_trade=1` row is recorded even
    when a full-signal skip logged first. Written by `bot_loops._log_decision`; scored by
    `scripts/edge_report.py` and `GET /api/edge`.
  - **`maker_log`** - the maker-vs-taker counterfactual. Also feeds the paper maker
    execution model when `maker_execution_enabled` is on.
    Per settled trade: whether a passive maker order would have filled and its P&L vs the
    realized taker fill. Written by `bot_loops._record_maker_counterfactual`; summarized by
    `scripts/maker_report.py` and `GET /api/edge`.
  - **`settlement_basis`** - per official settle: our Coinbase spot-implied side vs Kalshi's
    result and the signed distance from strike. Written by `bot_loops._record_settlement_basis`;
    feeds the per-asset basis-offset fit in the recalibration job.
  - All harness writes are gated by the `measurement_enabled` config flag (default true).
- Connection in `bot_infra.py` uses `sqlite3.connect(bot_state._DB_FILE)` and `aiosqlite` for async reads.
- Dashboard queries use `sqlite3.connect()` directly in `server.py:159` (`get_db()`).
- The `alembic/` directory at repo root belongs to the abandoned rewrite and **does not run**.

---

## Live Config

### config.json

Written to disk at startup if missing (`bot_infra.py:~131`). Edited via dashboard `/api/config` POST (`server.py:299`). Not committed to git (`.gitignore`).

Key fields: `bot_enabled`, `mode` (paper|demo|live), `trade_amount_dollars`, `daily_loss_limit_dollars`, `enabled_assets`.

Config normalization in `_init_config()` enforces (current behavior):
- **`trade_amount_dollars`** is hard-clamped to **<= $25** (per-trade clip cap).
- **`daily_loss_limit_dollars`** defaults to **0 = no daily loss cap** (the bot does not halt
  itself for the day). A positive value re-enables the cap (clamped <=150); `check_daily_limits`
  and the `bot_loops` kill-switch both treat a non-positive limit as disabled.
- **`measurement_enabled`** (default **true**) gates all edge-measurement instrumentation
  (`decision_log` / `maker_log` writes, held-book tracking, periodic settlement backfill).
- **Self-calibration** (`calibration_enabled`, default **true**): every 30 min the bot fits,
  in order, a per-asset **sigma scale** from settled `decision_log` z/outcome pairs
  (`fit_sigma_scale` - vol space is where the 2026-07 failure lived), a probability scale per
  strategy (`fit_prob_scale`), a per-asset settlement level offset from `settlement_basis`
  (`fit_basis_offset`), and per-asset **lead betas** from the live price deques
  (`fit_lead_beta` - the contemporaneous `fit_rolling_beta` slope is kept for reporting only
  and no longer writes `_live_betas`). All fits are n-gated and clamped, applied in the brains
  (`_sigma_eff`, `_calibrated_p`, `_basis_adjusted_spot`, `_asset_beta`), and persisted to
  `data/calibration.json` together with the implied-sigma EWMA (restored at startup while
  fresh, so a redeploy does not cold-start vol). `settlement_avg_seconds` (default 60) prices
  the digital to the effective settlement time `secs_left - avg/2` since Kalshi settles on a
  window average.
- **Auto-gate** (`auto_gate_enabled`, default **true**): the recalibration job blocks any ET
  session or (strategy, asset) bucket whose Wilson-LB net-$/contract is not positive at 150+
  settled picks (GATE-1 per bucket). The gate fires after the EV gate so blocked decisions
  still reach `decision_log`; blocked buckets appear in `data/calibration.json` / `/api/edge`.
- **Best-strike ladder** (`ladder_max_strikes`, default 3; 1 = off): in READY, non-BTC assets
  evaluate up to N candidate strikes/windows every 30s (`bot_loops._pick_best_strike`) and
  enter the one with the SMALLEST model-vs-market gap among firing candidates (tie-break:
  tighter spread). Win rate falls monotonically with the gap in settled data, so a max-EV pick
  would walk the ladder out to the worst strike. (BTC already gets this via the multi-window
  picker.)
- **Paper maker execution** (`maker_execution_enabled`, default **false**; S2 + paper mode
  only): settles each trade as the resting maker order the counterfactual tracked - filled
  trades get maker pricing + the ~25% maker fee, unfilled trades are voided at $0
  (`exit_reason='maker_unfilled'`, excluded from loss streaks). Turn on only after the Edge
  tab's maker-vs-taker delta is clearly positive.
- **Session filter** (`bot_strategy._session_allowed`, both brains, right after quiet-hours):
  `blocked_sessions` (list of ET session labels from `sessions.py` - `us_open`, `us_midday`,
  `us_close`, `us_evening`, `overnight`; default `[]`) and `block_weekends` (bool, default
  `false`) let the operator skip time windows the Edge panel shows losing. Default = no behavior
  change; a blocked window yields an `s{1,2}_session_gate:<label>` skip. The data-driven variant
  is the auto-gate above (`auto_gate_enabled`).
- DORMANT v3-era keys: `max_model_edge`, `min_market_edge`, `min_ev_anchored`,
  `max_model_market_gap`, the fv entry band (`fv_min/max_entry_price_cents`), the tail-ban
  floors (`s1_min_side_price_cents`, `s2_min_side_price_cents`), `s1_min_btc_ret` and the
  `staleness_*` family still sit in `_init_config` defaults but are NOT read by the current
  momentum/favorite-bias brains, which gate on their own `s1_min_edge`/`s7_min_edge`-style
  keys instead. They are config-file inertia from the replaced v3 brains - editing them
  changes nothing. Same for `min_ev_base` and `vol_gate_thresh` (the dashboard Risk card
  displays them, no live gate reads them) and the `s1_config`/`s2_config[asset]` per-asset
  override layer, whose mirrored keys are always clobbered by the flat `s1_*`/`s2_fav_*`
  values `_init_config` materializes.

### Dashboard API (additions)

`GET /api/edge` (`server.py`) surfaces the harness for the dashboard **Edge** tab: per-strategy
calibration + net-of-fee edge with Wilson lower bounds and a GATE-1 verdict (from `decision_log`),
plus the maker-vs-taker counterfactual (from `maker_log`). Reuses the math in
`scripts/edge_report.py` and `scripts/maker_report.py`. The `decisions` block also carries
`by_session` and `by_daytype` breakdowns (net-$/contract + Wilson LB per ET session and
weekday/weekend, each flagged `insufficient` below `edge_report.MIN_BUCKET_N`), derived by parsing
each row's `ts` through `sessions.py` - the "which market times pay" view. `scripts/edge_report.py`
prints the same two breakdowns. The payload also carries a `calibration` block (the fitted
prob_scale / basis offsets from `data/calibration.json`) and a `basis` block (per-asset
agreement stats from `settlement_basis`).

### Offline analysis scripts (additions)

- `scripts/replay_gates.py <export.csv>` - replays an exported trade CSV through the v3 gate
  set (pure arithmetic over the logged `entry_signals`; no network) and prints a per-gate block
  matrix plus the surviving-subset P&L. `tests/test_replay_gates.py` pins the directional result
  against the vendored 2026-06-30..07-04 export in `tests/fixtures/`.
- `scripts/plot_report.py --csv <export.csv>|--db <bot.db> [--out DIR]` - renders equity-curve,
  calibration, entry-price-bucket, and sigma-check PNGs. matplotlib is a script-only dependency
  (deliberately not in `requirements.txt`).
- `scripts/backtest_carry.py` / `scripts/tune_fade.py` - the S6 settlement backtests: carry
  killed, fade proven, then gated to its strongest sub-population (move >= 15bp, streak >= 2).
- `scripts/xasset_check.py` - cross-asset conditioning of the S6 fade: checked and REJECTED
  (BTC agreement adds no Wilson-LB lift; no 15-min BTC->alt lead-lag). Kept so the idea isn't
  re-derived from scratch.
- `scripts/fetch_candles.py` + `scripts/backtest_signals.py` - 1-min Coinbase candles for the
  settlement span (fetch must run from an unrestricted network - hosted containers block
  exchange hosts) and the intra-window signal tests they enable: S1 momentum-vs-model excess,
  S4 stall-fade, and the favorite-calibration behind S2/S8. pandas/pyarrow/requests are
  script-only dependencies. Results (see `handoff/NEXT_STEPS.md` "Historical validation
  status" for the full numbers): S1 is real but marginal (positive in all cells, clears the
  n>=1000/Wilson-LB bar in only 3 of 9, concentrated at 7min/z>=0.7) - too small and too
  loosely mapped onto the live brain's actual signal to retune gates from alone. S4 is
  data-starved (every bucket n<1000) - inconclusive. The favorite-calibration finding is the
  strongest of the three (+8-10 model-probability points at n=1900-4200 exactly where S2/S8
  trade) but is measured against a drift-free theoretical model, not real historical Kalshi
  prices, and likely partly reflects the settlement-averaging effect `effective_secs` already
  corrects for live - directionally supportive, not proof. None of the three cleared the bar
  S6 did (a full 3.5-point margin over breakeven, vs. sub-1.5-point margins here), so no gate
  changes shipped from this round; the live paper lab remains the authoritative test for all
  three. `load_closes()`'s epoch conversion must stay unit-agnostic (pandas.Timestamp
  subtraction, not raw `.astype("int64")`) - pandas >=3.0 preserves the parquet file's native
  datetime unit instead of always upcasting to nanoseconds, and a naive int64 view silently
  produced a 1000x-wrong epoch that zeroed out every decision row with no error.

### Environment Variables (runtime-verified)

Every variable below is confirmed by `grep -rn 'os.environ' *.py`.

| Variable | Where read | Purpose |
|---|---|---|
| `KALSHI_API_KEY` | `bot_market.py:63,102`; `collect_kalshi_ladder_history.py:73`; `price_validator.py:63` | Live API key |
| `KALSHI_PRIVATE_KEY` | `bot_market.py:64,103`; `collect_kalshi_ladder_history.py:74`; `price_validator.py:64` | Live RSA private key (PEM or path) |
| `KALSHI_DEMO_API_KEY` | `bot_market.py:77` | Demo API key |
| `KALSHI_DEMO_PRIVATE_KEY` | `bot_market.py:78` | Demo RSA private key |
| `TELEGRAM_BOT_TOKEN` | `bot_state.py:42`; `notify.py:29` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | `bot_state.py:43`; `notify.py:30` | Telegram destination chat |
| `BOT_CONFIG_FILE` | `bot_state.py:45` | Path to `config.json`; default `config.json` |
| `BOT_DB_FILE` | `bot_state.py:46`; `server.py:45,161,357,706,1025,1026`; `notify.py:52` | Path to sqlite3 DB; default `kalshi_bot.db` |
| `BOT_STATE_FILE` | `bot_state.py:47`; `server.py:1025,1075` | Path to `bot_state.json`; default `bot_state.json` |
| `BOT_MODE` | `bot_infra.py:142` | Override `mode` in `config.json` (paper\|demo\|live) |
| `BOT_ENABLED` | `bot_infra.py:144` | Override `bot_enabled` in `config.json` (true\|false) |
| `LOG_FORMAT` | `obs.py:90` | `text` = human-readable; default `json` (structured) |
| `PORT` | `start.sh:8` | Railway-assigned port for gunicorn (bash, not Python) |

**Not read by any `.py` file:** `LOG_LEVEL`, `SOLANA_RPC_URL`, `DATABASE_URL`, `HEALTH_PORT`, `CONFIG_PATH`. Those appeared in old docs and are dead.

---

## Asset Support

**5 assets, not 7.** Confirmed by `bot_strategy.py:51`.

```python
{"BTC": "B3", "ETH": "E1", "SOL": "SL1", "XRP": "X3", "DOGE": "D3"}
```

- **Default `enabled_assets`:** `["ETH", "SOL", "XRP"]` - `bot_infra.py:73`, `bot.py:81`
- **BTC:** always subscribed for price data (correlation signals), but not traded by default - `bot.py:82-87`
- **DOGE:** supported by strategy, not enabled by default
- **HYPE, BNB:** not present anywhere in the codebase

---

## Quarantined / Experimental

These files describe an **abandoned rewrite** ("kalshi-botv3"). They are **not loaded by any production code**. Do not edit unless reviving the rewrite. Do not "fix" inconsistencies between these and the live bot - they are intentional.

| Path | What it is |
|---|---|
| `src/kalshi_bot/` | Empty FastAPI/SQLAlchemy scaffold. No business logic. Never imported. |
| `alembic/` | Alembic migration for the rewrite's SQLAlchemy schema. Does not run. |
| `pyproject.toml` | Describes "kalshi-botv3" with FastAPI, SQLAlchemy, etc. Live bot uses `requirements.txt`. |
| `uv.lock` | Lock file for the rewrite's deps. Live bot uses no lock file. |

---

## Documentation Reliability

| File | Status | What it gets wrong |
|---|---|---|
| `README.md` | **STALE** (now updated) | Previously described the dead rewrite: uv quickstart, 7 assets, FastAPI, scaffolding-only status |
| `.env.example` | **STALE** (now updated) | Previously listed `DATABASE_URL` (aiosqlite), `HEALTH_PORT`, `CONFIG_PATH`, `MODE` (wrong name), `LOG_LEVEL` (not read) |
| `RAILWAY_SETUP.md` | **PARTIALLY STALE** | "bot.py runs as the worker process" is wrong - `runner.py` does. Volume path says `/app` but `BOT_DB_FILE` points inside `/app/data`. `SOLANA_RPC_URL` documented but not read in any `.py` file. |
