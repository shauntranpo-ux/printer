# bot.py Compaction Design

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Split bot.py (4,439 lines) into 11 focused modules without changing any trading logic, signal math, or decision paths. bot.py becomes a ~200-line entrypoint.

**Constraint:** Zero changes to trading logic. All 606 existing tests must pass before and after every task. `python -c "import bot"` must succeed after every task.

---

## Context

`bot.py` is a single-file trading bot that has grown to 4,439 lines. It is a standalone asyncio process — `server.py` and `runner.py` do not import from it, so all splits are safe. The file has clear natural seams already visible in its section comments.

**Key discovery:** Three calibration functions are defined but never called anywhere in the trading loop (`calibrate_from_history`, `calibrate_brain`, `_calibrate_one_brain`). The `_adaptive` dict they write to is also never read in any decision path. These are dead code and will be removed.

---

## Module Map

| File | Responsibility | Est. Lines |
|------|---------------|------------|
| `bot_state.py` | All mutable globals and constants (plain module — every other file does `import bot_state`) | 120 |
| `bot_config.py` | `read_config`, `write_config`, `get_asset_config`, `atomic_write_json`, `_init_config` | 200 |
| `bot_db.py` | `init_db`, `test_db_write`, `db_write_trade`, `db_update_trade`, `db_write_market_log`, `db_get_today_pnl` | 260 |
| `bot_notify.py` | `send_telegram`, `_maybe_fill_verification_notify`, `_notify_ctx`, `_phase_for_eth` | 130 |
| `bot_kalshi.py` | RSA auth/signing, `load_credentials`, `kalshi_headers`, `get_btc_price`, `fetch_current_market`, `fetch_market_for_asset`, `fetch_orderbook`, `parse_strike`, `seconds_remaining`, `seconds_elapsed`, `_simulated_amm_midpoint`, `_log_price_validation` | 750 |
| `bot_orders.py` | `place_order`, `_verify_order_fill`, `_portfolio_has_position`, `calculate_contracts`, `implied_prob` | 520 |
| `bot_strategy.py` | S1 brain (BV3 table, vol ratio, momentum, velocity), S2 brain (FifteenMinStrategy dispatch), calibration stubs, `track_contract_price`, `_session_ev_adjustment` | 800 |
| `bot_risk.py` | `check_daily_limits`, `midnight_reset`, `write_state_file`, `_log_entry`, `_parse_strike_from_ticker` | 320 |
| `bot_trade.py` | `_execute_s1_trade`, `_settle_s1_trade`, `_try_settle_orphaned_s1` | 200 |
| `bot_loops.py` | `handle_ready_phase`, `handle_locked_phase`, `_init_asset_state`, `_process_asset`, `_non_btc_asset_loop`, `main_loop` | 1,100 |
| `bot_preflight.py` | `verify_kalshi_connection`, `run_preflight_checks` | 220 |
| `bot.py` | Globals init, OBI/funding monitor startup, `main()` | ~200 |

---

## bot_state.py — Globals Container

Plain Python module. All other modules do `import bot_state` and read/write attributes directly (e.g., `bot_state.current_phase = "READY"`). No classes, no dataclasses — exact same semantics as the current module-level globals, just in a dedicated file.

### Constants (never change at runtime)
```python
KALSHI_LIVE_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
KALSHI_PATH_PREFIX   = "/trade-api/v2"
API_TIMEOUT          = 10
MARKET_CACHE_TTL     = 30
WATCH_PHASE_SECONDS  = 30
KALSHI_FEE           = 0.07

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "")

_CONFIG_FILE = os.environ.get("BOT_CONFIG_FILE", "config.json")
_DB_FILE     = os.environ.get("BOT_DB_FILE",     "kalshi_bot.db")
_STATE_FILE  = os.environ.get("BOT_STATE_FILE",  "bot_state.json")
_DATA_DIR    = os.path.dirname(os.path.abspath(_DB_FILE))
_PRICE_VAL_CSV = os.path.join(_DATA_DIR, "price_validation_log.csv")
```

### Mutable runtime state
```python
# Price feed (alias to asset_manager's BTC deque)
import asset_manager
from collections import deque
btc_prices: deque = asset_manager._prices["BTC"]

# Auth
private_key = None
api_key: str = ""
KALSHI_BASE_URL = KALSHI_LIVE_BASE_URL  # overwritten in load_credentials()

# Market / phase
current_market: dict | None = None
current_phase: str = "DONE"
current_position: dict | None = None
_order_attempted_tickers: set = set()
_asset_states: dict = {}

# Market cache
_market_cache: dict | None = None
_market_cache_ts: float = 0.0
_all_markets_cache: list = []
_all_markets_cache_ts: float = 0.0

# Daily limits
limit_triggered: bool = False
limit_reason: str = ""
pre_limit_mode: str | None = None

# Price validation counters
_price_val_count: int = 0
_price_val_sim_sum: float = 0.0
_price_val_real_sum: float = 0.0
_price_val_gap_sum: float = 0.0

# State-file snapshot fields
last_confidence_score: int = 0
last_confidence_breakdown: dict = {}
last_action: str = ""
last_skip_reason: str = ""

# Per-asset eval cache and contract history
_asset_eval: dict = {}
_contract_price_history: dict = {}

# Brain calibration (S1 and S2 — updated by calibrate_brain when wired)
_CAL_DEFAULTS: dict = {
    "last_count": 0, "prob_scale": 1.0, "min_edge_override": None,
    "confidence_bonus": 0, "reward_tier": 0, "overall_wr": 0.0,
    "condition_wr": {}, "bullish_wr": 0.5, "bearish_wr": 0.5,
}
_brain_cal_s1: dict = {**_CAL_DEFAULTS}
_brain_cal_s2: dict = {**_CAL_DEFAULTS}

# S2 strategy singletons
_S2_SINGLETONS: dict = {}
_config_mtime: float = 0.0
_current_window: str = ""

# OBI and funding monitors (started in main())
_obi_monitor = None
_funding_monitor_btc = None
_funding_monitor_eth = None

# Consecutive tracking
_consecutive_losses: int = 0
_consecutive_price_skips: int = 0
_last_good_config: dict | None = None

# Strategy version tags
_S1_VERSION = "bv3-2026-05-06"
_S2_VERSION = "d3-hybrid-2026-05-06"
```

---

## Dependency Graph

No circular imports. Arrow = "imports from".

```
asset_manager  (external, unchanged)
      ↑
bot_state      ← leaf, imported by everything else

bot_config     → bot_state
bot_db         → bot_state
bot_notify     → bot_state
bot_kalshi     → bot_state, bot_config
bot_orders     → bot_state, bot_kalshi, bot_notify, bot_db
bot_strategy   → bot_state, bot_config, asset_manager (for price deques)
bot_risk       → bot_state, bot_db, bot_config, bot_notify
bot_trade      → bot_state, bot_orders, bot_strategy, bot_db, bot_notify, bot_kalshi, bot_risk
bot_loops      → bot_state, bot_strategy, bot_orders, bot_trade, bot_risk,
                 bot_kalshi, bot_db, bot_notify, bot_config, asset_manager
bot_preflight  → bot_state, bot_kalshi, bot_db, bot_notify
bot.py         → bot_loops, bot_preflight, bot_db, bot_config, bot_state,
                 obi_monitor, asset_manager
```

`bot_state` and the leaf modules (`bot_config`, `bot_db`, `bot_notify`) have no dependencies on each other.

---

## Dead Code Removal

Remove these from bot.py and do not carry them into any new module:

| Symbol | Lines | Reason |
|--------|-------|--------|
| `_adaptive` dict | ~144–150 | Only written by `calibrate_from_history`; never read in any decision path |
| `calibrate_from_history()` | ~1405–1458 | Never called anywhere in the trading loop |
| `_calibrate_one_brain()` | ~1460–1517 | Only called from dead `calibrate_brain` |
| `calibrate_brain()` | ~1519–1527 | Never called; wraps `_calibrate_one_brain` |
| `recalibrate_asset_strategies()` | ~1529–1580 | Never called |

Total dead lines removed: ~180.

> Note: `_brain_cal_s1` and `_brain_cal_s2` are **kept** — they are read by the strategy brains (prob_scale, bullish_wr, bearish_wr, etc.). They just never get updated because calibrate_brain was never wired. They live in `bot_state.py` at their default values.

---

## Global Mutation Pattern

Wherever bot.py currently does:
```python
global current_phase
current_phase = "READY"
```

The new code does:
```python
import bot_state
bot_state.current_phase = "READY"
```

No `global` declarations needed — Python module attributes are mutable by assignment. Every function that currently declares a global will instead import bot_state at the top of its module and reference `bot_state.<name>`.

`KALSHI_BASE_URL` is special — it's a "constant" that gets overwritten once in `load_credentials()`. Pattern:
```python
# In bot_kalshi.py:
import bot_state
def load_credentials(mode):
    ...
    bot_state.KALSHI_BASE_URL = bot_state.KALSHI_DEMO_BASE_URL
```

`brain_log` and its FileHandler are set up at module level in `bot_strategy.py` (the only module that uses them for strategy decision logging).

---

## Special Notes

### `_S2_SINGLETONS`, `_config_mtime`, `_current_window`
These are currently defined inside `_get_or_make_strategy_s2` as module-level variables placed mid-file (lines 1585–1588). In the refactor they move to `bot_state.py` like all other globals. `_get_or_make_strategy_s2` moves to `bot_strategy.py` and reads them via `bot_state`.

### `_STRIKE_RE_T_SUFFIX`, `_STRIKE_RE_NUMERIC_SUFFIX`
These compiled regexes live next to `_parse_strike_from_ticker`. They move with it to `bot_risk.py` as module-level constants.

### Logging setup
`log = logging.getLogger("bot")` stays in `bot.py`. Each module that needs logging creates its own `log = logging.getLogger(__name__)` or `logging.getLogger("bot")` — Python's logging registry is global so they share the same configured handler.

### `test_db_write`
Called once from `main()` as a startup sanity check. Lives in `bot_db.py`; `bot.py` imports and calls it.

---

## Testing Strategy

After every task (every file extracted):
1. `py -3 -m pytest tests/ -x -q` — all 606 must pass
2. `py -3 -c "import bot"` — must not raise ImportError

Final acceptance after all tasks complete:
1. All 606 tests pass
2. `py -3 -c "import bot"` succeeds
3. `git diff --stat` shows bot.py at ~200 lines
4. No `global` declarations remain in any extracted module (they all use `bot_state.<attr>`)

---

## Implementation Order

Extract in dependency order — leaves first, orchestrator last:

1. `bot_state.py` — globals container (no deps on other bot_* modules)
2. `bot_config.py` — reads bot_state for file paths
3. `bot_db.py` — reads bot_state for _DB_FILE
4. `bot_notify.py` — reads bot_state for Telegram tokens
5. `bot_kalshi.py` — reads bot_state + bot_config
6. `bot_orders.py` — reads bot_state + bot_kalshi + bot_notify + bot_db
7. `bot_strategy.py` — reads bot_state + bot_config + asset_manager; dead code removed here
8. `bot_risk.py` — reads bot_state + bot_db + bot_config + bot_notify
9. `bot_trade.py` — reads bot_state + bot_orders + bot_strategy + bot_db + bot_notify + bot_kalshi + bot_risk
10. `bot_preflight.py` — reads bot_state + bot_kalshi + bot_db + bot_notify
11. `bot_loops.py` — reads everything
12. `bot.py` cleanup — strip extracted code, keep globals init + main()
