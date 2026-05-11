# Architecture: Money Printer Kalshi Bot

**If anything in README.md or pyproject.toml conflicts with this file, THIS file wins.**

Sources of truth: running code and `grep`. Every claim below is verified with a file:line citation. If you find a discrepancy, fix the doc—not the code.

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
| `bot_strategy.py` | S1 (EMA momentum) and S2 (contract velocity + OBI) strategy brains |
| `bot_infra.py` | `config.json` read/write, sqlite3 `init_db()`, Telegram async helper |
| `bot_state.py` | Shared in-memory globals: price deques, API keys, trade state |
| `bot_stats.py` | Win rate calibration helpers used by S1/S2 brains |
| `asset_manager.py` | Coinbase WebSocket price feed; populates price history deques |
| `price_validator.py` | Paper-mode: continuous price accuracy monitor vs Kalshi mid |
| `validate_and_report.py` | Live-mode: blocking GO/NO-GO preflight gate before bots start |
| `weekly_report.py` | Generates weekly PnL summary via Telegram |
| `collect_kalshi_ladder_history.py` | Downloads Kalshi order book ladder history for offline analysis |
| `backtest.py` | Offline backtest runner. **NOT loaded at runtime.** |
| `obs.py` | Structured JSON logging formatter and in-memory error ring buffer |
| `notify.py` | Telegram + `alerts.log` dispatcher; used by `runner.py` and `server.py` |

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
- `runner.py` is **not** the gunicorn worker — gunicorn serves `server.py` independently.
- Both processes share the same Railway volume via `BOT_DB_FILE` env var.

---

## Live Database

Raw sqlite3. **No ORM. No Alembic migrations.**

- Schema defined in: `bot_infra.py:158` (`init_db()`)
- Tables: `trades`, `market_log`, `daily_summary`, `stress_test_results`
- Connection in `bot_infra.py` uses `sqlite3.connect(bot_state._DB_FILE)` and `aiosqlite` for async reads.
- Dashboard queries use `sqlite3.connect()` directly in `server.py:159` (`get_db()`).
- The `alembic/` directory at repo root belongs to the abandoned rewrite and **does not run**.

---

## Live Config

### config.json

Written to disk at startup if missing (`bot_infra.py:~131`). Edited via dashboard `/api/config` POST (`server.py:299`). Not committed to git (`.gitignore`).

Key fields: `bot_enabled`, `mode` (paper|demo|live), `trade_amount_dollars`, `daily_loss_limit_dollars`, `enabled_assets`.

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

- **Default `enabled_assets`:** `["ETH", "SOL", "XRP"]` — `bot_infra.py:73`, `bot.py:81`
- **BTC:** always subscribed for price data (correlation signals), but not traded by default — `bot.py:82-87`
- **DOGE:** supported by strategy, not enabled by default
- **HYPE, BNB:** not present anywhere in the codebase

---

## Quarantined / Experimental

These files describe an **abandoned rewrite** ("kalshi-botv3"). They are **not loaded by any production code**. Do not edit unless reviving the rewrite. Do not "fix" inconsistencies between these and the live bot — they are intentional.

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
| `RAILWAY_SETUP.md` | **PARTIALLY STALE** | "bot.py runs as the worker process" is wrong — `runner.py` does. Volume path says `/app` but `BOT_DB_FILE` points inside `/app/data`. `SOLANA_RPC_URL` documented but not read in any `.py` file. |
