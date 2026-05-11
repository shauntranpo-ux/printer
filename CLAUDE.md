# gstack

Use the `/browse` skill from gstack for all web browsing. Never use `mcp__claude-in-chrome__*` tools.

Available gstack skills:
/office-hours, /plan-ceo-review, /plan-eng-review, /plan-design-review, /design-consultation, /design-shotgun, /design-html, /review, /ship, /land-and-deploy, /canary, /benchmark, /browse, /connect-chrome, /qa, /qa-only, /design-review, /setup-browser-cookies, /setup-deploy, /retro, /investigate, /document-release, /codex, /cso, /autoplan, /plan-devex-review, /devex-review, /careful, /freeze, /guard, /unfreeze, /gstack-upgrade, /learn

---

## Repo Layout Guardrails

**Read `ARCHITECTURE.md` before touching any code in this repo.**

### What is live vs dead

- **Live bot = top-level flat `.py` files** (bot.py, runner.py, server.py, bot_loops.py, bot_market.py, bot_risk.py, bot_strategy.py, bot_infra.py, bot_state.py, bot_stats.py, asset_manager.py, obs.py, notify.py, and the sidecar scripts). These run on Railway and trade real money.
- **`src/kalshi_bot/` is quarantined and dead.** Empty scaffold for an abandoned rewrite. Not imported by anything. Do not edit it as if it were live.
- **Live deps = `requirements.txt`.** `pyproject.toml` is quarantined — it describes the dead rewrite ("kalshi-botv3") and lists FastAPI, SQLAlchemy, etc. that are not used at runtime.
- **Live DB = raw sqlite3 via `bot_infra.init_db()` (bot_infra.py:158).** `alembic/` does not run. Do not run `alembic upgrade`. Do not add SQLAlchemy models.

### What drives trading decisions

- **Live decision path:** `strategy_brain_s1()` and `strategy_brain_s2()` in `bot_strategy.py`, called from `bot_loops.py`.
- **`backtest.py` and `_BV3_TABLE`** are offline analysis tools. They are **not loaded at runtime** and must not be referenced as if they affect live behavior.
- **5 assets, not 7:** BTC, ETH, SOL, XRP, DOGE. Default enabled: ETH, SOL, XRP (bot_infra.py:73). HYPE and BNB do not exist in the codebase.

### When docs and code disagree

- **Code wins. Update the doc, not the code.**
- `README.md`, `.env.example`, and `RAILWAY_SETUP.md` were stale and have been updated (commit 70948e5 and prior). If you find new discrepancies, fix the doc.
- `ARCHITECTURE.md` is the authoritative layout document. If it conflicts with any other file, `ARCHITECTURE.md` wins.
