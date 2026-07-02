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
| `bot_loops.py` | Main async market loop. Phase handler: watching → ready → trading → cooldown |
| `bot_market.py` | Kalshi REST API client. Order placement, OBI calculation, fill polling |
| `bot_risk.py` | Preflight checks, trade execution, PnL tracking, S1 orphan settlement |
| `bot_strategy.py` | S1 (CA-LEAD-SLOW: BTC-lead cross-asset dislocation) and S2 (spot_fv_disloc: spot-anchored Bachelier fair-value dislocation) strategy brains |
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

Both brains compute a **Bachelier fair value** for P(YES) (`_bachelier_p_above`) and trade only
when the de-vigged market mid is stale-cheap relative to it, gated by `_anchored_ev` (shrink the
model toward the mid, cap the deviation at `max_model_edge`, require `ev ≥ min_ev_anchored` **and**
`market_edge ≥ min_market_edge`). Direction comes from the model, not from momentum. Vol for the
digital is `_sigma_eff` - a blend of live realized vol (`_live_sigma_15m`, quadratic-variation) and
the static per-asset vol, so a co-moving jump does not over-inflate σ right when we act.

- **S1 - CA-LEAD-SLOW (SOL / XRP / DOGE).** BTC leads the alts intraday. Over a ~60s lookback it
  computes `residual = beta·btc_ret − alt_ret` (beta from `data/betas.json` via the mtime-cached
  `_load_betas`), predicts the alt's catch-up spot `alt_now·exp(residual)`, and prices the digital
  on that. BTC/ETH are disabled by default (`s1_ca_btc_enabled` / `s1_ca_eth_enabled`). Keeps the
  existing S1 caps / rate-limit / cooldown / cross-asset window guard.
- **S2 - spot_fv_disloc (BTC / SOL / XRP / DOGE).** Prices the digital on the current spot vs strike
  and trades the stale-cheap side. Gates: `|z| ≥ min_z`, spot-sign confirmation over the last N
  prints (no flicker across the strike), and a round-trip spread cap. ETH disabled by default
  (`s2_eth_enabled`).

The legacy momentum/velocity/OBI helpers (`_s1_multitf_momentum`, `_s2_contract_direction`,
`_s2_obi_gate`, `_s1_certainty_win_prob`, the `_S1_ASSET_CONFIG` / `_S2_ASSET_CONFIG` dicts and the
win-rate tables) are retained for other callers/tests but are **no longer on the live decision
path**. Both brains keep the prior return-dict shape (incl. `signals.model_raw_p_yes`) so the loop,
website, and the `decision_log` harness are unchanged.

---

## Live Dependencies

File: `requirements.txt`

Key runtime deps: `flask`, `gunicorn`, `aiohttp`, `websockets`, `aiosqlite`. Standard library `sqlite3` for schema creation and dashboard queries.

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
        └── bot.py → bot_loops.main_loop()
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
    `model_p_yes`, `market_mid_p_yes`, `market_edge`, settlement `outcome` backfilled at
    expiry. Logs ALL evaluated decisions (not just taken trades) so signal edge is measured
    free of survivorship bias. Written by `bot_loops._log_decision`; scored by
    `scripts/edge_report.py` and `GET /api/edge`.
  - **`maker_log`** - the maker-vs-taker counterfactual (measurement only, no execution).
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
- **`trade_amount_dollars`** is hard-clamped to **≤ $25** (per-trade clip cap).
- **`daily_loss_limit_dollars`** defaults to **0 = no daily loss cap** (the bot does not halt
  itself for the day). A positive value re-enables the cap (clamped ≤150); `check_daily_limits`
  and the `bot_loops` kill-switch both treat a non-positive limit as disabled.
- **`measurement_enabled`** (default **true**) gates all edge-measurement instrumentation
  (`decision_log` / `maker_log` writes, held-book tracking, periodic settlement backfill).
- **Self-calibration** (`calibration_enabled`, default **true**): every 30 min the bot fits a
  probability scale per strategy from its own settled `decision_log` rows and a per-asset
  settlement level offset from `settlement_basis` (`scripts/calibration.py`; n-gated, clamped),
  applies them in the brains (`_calibrated_p`, `_basis_adjusted_spot`), and persists to
  `data/calibration.json`. `settlement_avg_seconds` (default 60) prices the digital to the
  effective settlement time `secs_left - avg/2` since Kalshi settles on a window average.
  The same job also fits **rolling BTC-lead betas** from the live price deques
  (`fit_rolling_beta`; preferred by `_asset_beta` over `data/betas.json`).
- **Auto-gate** (`auto_gate_enabled`, default **true**): the recalibration job blocks any ET
  session or (strategy, asset) bucket whose Wilson-LB net-$/contract is not positive at 150+
  settled picks (GATE-1 per bucket). The gate fires after the EV gate so blocked decisions
  still reach `decision_log`; blocked buckets appear in `data/calibration.json` / `/api/edge`.
- **Best-strike ladder** (`ladder_max_strikes`, default 3; 1 = off): in READY, non-BTC assets
  evaluate up to N candidate strikes/windows every 30s (`bot_loops._pick_best_strike`) and
  enter the highest-EV one. (BTC already gets this via the multi-window picker.)
- **Paper maker execution** (`maker_execution_enabled`, default **false**; S2 + paper mode
  only): settles each trade as the resting maker order the counterfactual tracked - filled
  trades get maker pricing + the ~25% maker fee, unfilled trades are voided at $0
  (`exit_reason='maker_unfilled'`, excluded from loss streaks). Turn on only after the Edge
  tab's maker-vs-taker delta is clearly positive.
- **Session filter** (`bot_strategy._session_allowed`, both brains, right after quiet-hours):
  `blocked_sessions` (list of ET session labels from `sessions.py` - `us_open`, `us_midday`,
  `us_close`, `us_evening`, `overnight`; default `[]`) and `block_weekends` (bool, default
  `false`) let the operator skip time windows the Edge panel shows losing. Default = no behavior
  change; a blocked window yields an `s{1,2}_session_gate:<label>` skip. `session_filter_enabled`
  (default `false`) is reserved for a future data-driven auto-gate (not yet wired).
- EV-gate tuning: `max_model_edge`, `min_market_edge`, `min_ev_anchored` (the market-anchored
  gate, `bot_strategy._anchored_ev`) are read per-asset from `s1_config`/`s2_config[asset]` and
  fall back to a top-level config value, then to conservative defaults (0.08 / 0.035 / 0.025).

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
