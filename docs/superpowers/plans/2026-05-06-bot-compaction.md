# bot.py Compaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split bot.py (4,439 lines) into 11 focused modules + thin entrypoint without changing any trading logic.

**Architecture:** Plain-module shared globals (`bot_state.py`) extracted first; all other modules import it. Functions extracted in dependency order; bot.py re-exports everything tests touch until the final cleanup task.

**Tech Stack:** Python 3.11+, asyncio, aiohttp, aiosqlite, cryptography. All existing stdlib imports.

---

## Pre-flight

Run before starting **every** task:
```
py -3 -m pytest tests/ -x -q
```
All 606 tests must pass. If they don't, stop and investigate before touching anything.

---

## Task 1: Create bot_state.py and strip all globals from bot.py

**Files:**
- Create: `bot_state.py`
- Modify: `bot.py` (remove global definitions, add `import bot_state`, rewrite every bare global reference)

### Step 1: Write bot_state.py

Create `bot_state.py` in the repo root with this exact content:

```python
"""
bot_state.py - Shared mutable globals and constants for the kalshi bot.

Every other module does `import bot_state` and reads/writes attributes here.
No classes, no dataclasses - plain module attributes for zero-overhead access.
"""
import os
from collections import deque

# constants (never change at runtime)
KALSHI_LIVE_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
KALSHI_BASE_URL      = KALSHI_LIVE_BASE_URL  # overwritten once in load_credentials()
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

# mutable runtime state
import asset_manager  # noqa: E402 - after os/path setup
btc_prices: deque = asset_manager._prices["BTC"]

_obi_monitor       = None   # OBIMonitor | None  - set in main()
_funding_monitor_btc = None  # FundingDispersionMonitor | None
_funding_monitor_eth = None  # FundingDispersionMonitor | None

private_key = None
api_key: str = ""

current_market: dict | None = None
current_phase: str = "DONE"
current_position: dict | None = None
_order_attempted_tickers: set = set()
_asset_states: dict = {}

_market_cache: dict | None = None
_market_cache_ts: float = 0.0
_all_markets_cache: list = []
_all_markets_cache_ts: float = 0.0

limit_triggered: bool = False
limit_reason: str = ""
pre_limit_mode: str | None = None
daily_reset_date = None

_price_val_count: int    = 0
_price_val_gap_n: int    = 0
_price_val_sim_sum: float  = 0.0
_price_val_real_sum: float = 0.0
_price_val_gap_sum: float  = 0.0

last_confidence_score: int      = 0
last_confidence_breakdown: dict = {}
last_action: str       = ""
last_skip_reason: str  = ""

_asset_eval: dict             = {}
_contract_price_history: dict = {}

_CAL_DEFAULTS: dict = {
    "last_count": 0, "prob_scale": 1.0, "min_edge_override": None,
    "confidence_bonus": 0, "reward_tier": 0, "overall_wr": 0.0,
    "condition_wr": {}, "bullish_wr": 0.5, "bearish_wr": 0.5,
}
_brain_cal_s1: dict = {**_CAL_DEFAULTS}
_brain_cal_s2: dict = {**_CAL_DEFAULTS}

_last_good_config: dict | None = None
_consecutive_losses: int       = 0
_consecutive_price_skips: int  = 0

_S2_SINGLETONS: dict  = {}
_config_mtime: float  = 0.0
_current_window: str  = ""

_S1_VERSION = "bv3-2026-05-06"
_S2_VERSION = "d3-hybrid-2026-05-06"

_S1_ASSET_VOL_RATIO: dict = {
    "BTC": 1.00, "ETH": 1.10, "SOL": 2.20, "XRP": 1.80, "DOGE": 2.60,
}
```

- [ ] **Step 1a:** Create the file as above.

### Step 2: Strip global definitions from bot.py and add bot_state import

- [ ] **Step 2a:** Open `bot.py`. Delete lines that define the same names now in `bot_state.py`.
  Remove from bot.py (approximate line ranges, verify before deleting):
  - Lines 59-65: `KALSHI_LIVE_BASE_URL`, `KALSHI_DEMO_BASE_URL`, `KALSHI_BASE_URL`, `KALSHI_PATH_PREFIX`, `API_TIMEOUT`, `MARKET_CACHE_TTL`, `WATCH_PHASE_SECONDS`, `KALSHI_FEE`
  - Lines 73-76: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
  - Lines 79-82: `_CONFIG_FILE`, `_DB_FILE`, `_STATE_FILE`, `_DATA_DIR`
  - Lines 86-174: all runtime globals from `btc_prices` through `_consecutive_price_skips`
  - Lines 1585-1588: `_S2_SINGLETONS`, `_config_mtime`, `_current_window` (mid-file globals)
  - Lines 1829-1830: `_S1_VERSION`, `_S2_VERSION`
  - Lines 1851-1857: `_S1_ASSET_VOL_RATIO`

- [ ] **Step 2b:** At the top of `bot.py`, directly after the existing `import asset_manager` line, add:
  ```python
  import bot_state
  ```

- [ ] **Step 2c:** Run the following helper script to rewrite every bare global reference in `bot.py`.
  Save as `_task1_rewrite.py` and run once (`py -3 _task1_rewrite.py`), then delete it.

```python
"""
_task1_rewrite.py - one-shot: rewrite bot.py global references to bot_state.*
Run from repo root: py -3 _task1_rewrite.py
"""
import re, sys

GLOBALS = [
    "KALSHI_LIVE_BASE_URL", "KALSHI_DEMO_BASE_URL", "KALSHI_BASE_URL",
    "KALSHI_PATH_PREFIX", "API_TIMEOUT", "MARKET_CACHE_TTL",
    "WATCH_PHASE_SECONDS", "KALSHI_FEE",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "_CONFIG_FILE", "_DB_FILE", "_STATE_FILE", "_DATA_DIR", "_PRICE_VAL_CSV",
    "btc_prices",
    "_obi_monitor", "_funding_monitor_btc", "_funding_monitor_eth",
    "private_key", "api_key",
    "current_market", "current_phase", "current_position",
    "_order_attempted_tickers", "_asset_states",
    "_market_cache", "_market_cache_ts",
    "_all_markets_cache", "_all_markets_cache_ts",
    "limit_triggered", "limit_reason", "pre_limit_mode", "daily_reset_date",
    "_price_val_count", "_price_val_gap_n", "_price_val_sim_sum",
    "_price_val_real_sum", "_price_val_gap_sum",
    "last_confidence_score", "last_confidence_breakdown",
    "last_action", "last_skip_reason",
    "_asset_eval", "_contract_price_history",
    "_CAL_DEFAULTS", "_brain_cal_s1", "_brain_cal_s2",
    "_last_good_config", "_consecutive_losses", "_consecutive_price_skips",
    "_S2_SINGLETONS", "_config_mtime", "_current_window",
    "_S1_VERSION", "_S2_VERSION", "_S1_ASSET_VOL_RATIO",
]

path = "bot.py"
src = open(path, encoding="utf-8").read()
lines = src.splitlines(keepends=True)
out = []

for line in lines:
    # Drop bare `global X` declarations for our globals
    stripped = line.strip()
    if stripped.startswith("global "):
        names = [n.strip() for n in stripped[7:].split(",")]
        remaining = [n for n in names if n not in GLOBALS]
        if not remaining:
            continue  # drop the whole line
        line = line.replace(stripped, "global " + ", ".join(remaining))

    # Rewrite bare identifiers -> bot_state.IDENTIFIER
    # Only when not already prefixed with "bot_state." and not in strings/comments
    # Use word-boundary replacement for each global name
    for g in GLOBALS:
        # Match the name when NOT preceded by a dot (avoid double-prefixing)
        line = re.sub(
            r'(?<!\.)(?<!\w)(' + re.escape(g) + r')(?!\w)',
            r'bot_state.\1',
            line,
        )

    out.append(line)

open(path, "w", encoding="utf-8").writelines(out)
print("Done. Review the diff before committing.")
```

> **Important:** After running the script, review `git diff bot.py` manually.
> Check for false positives - the script can incorrectly prefix local variables
> that happen to share a global name. Fix any false-positive replacements manually.
> Known safe patterns: function parameters named `config`, `mode`, `asset` are NOT globals.

- [ ] **Step 2d:** Run `git diff bot.py | head -200` to spot-check the changes.

### Step 3: Verify

- [ ] **Step 3a:** Run:
  ```
  py -3 -c "import bot"
  ```
  Expected: no output, no ImportError.

- [ ] **Step 3b:** Run:
  ```
  py -3 -m pytest tests/ -x -q
  ```
  Expected: 606 passed.

### Step 4: Commit

- [ ] **Step 4a:**
  ```
  git add bot_state.py bot.py
  git commit -m "refactor: extract all globals to bot_state.py"
  ```

---

## Task 2: Extract bot_config.py

**Files:**
- Create: `bot_config.py`
- Modify: `bot.py` (delete moved functions, add re-import)

### Step 1: Write bot_config.py

- [ ] **Step 1a:** Create `bot_config.py`:

```python
"""bot_config.py - Config read/write and atomic JSON helper."""
import json
import logging
import os
import tempfile

import bot_state

log = logging.getLogger("bot")


def atomic_write_json(data: dict, path: str) -> None:
    # (move verbatim from bot.py lines 179-206)
    ...


def read_config() -> dict:
    # (move verbatim from bot.py lines 306-329)
    ...


def write_config(data: dict) -> None:
    # (move verbatim from bot.py lines 331-334)
    ...


def get_asset_config(config: dict, asset: str, field: str, default=None):
    # (move verbatim from bot.py lines 336-342)
    ...


def _init_config() -> None:
    # (move verbatim from bot.py lines 344-403)
    ...
```

> Fill in each `...` with the exact function body from bot.py. Any bare reference
> to `_CONFIG_FILE`, `_DB_FILE`, `_DATA_DIR`, `_last_good_config` is already
> rewritten to `bot_state.*` by Task 1. No further changes needed inside the bodies.

- [ ] **Step 1b:** Verify `atomic_write_json` uses `bot_state._DATA_DIR` (indirectly via the path argument) and `bot_state._CONFIG_FILE` is accessed only by `read_config`/`write_config`/`_init_config`.

### Step 2: Update bot.py

- [ ] **Step 2a:** Delete functions `atomic_write_json`, `read_config`, `write_config`, `get_asset_config`, `_init_config` from bot.py (lines 179-403).

- [ ] **Step 2b:** After `import bot_state` in bot.py, add:
  ```python
  from bot_config import (
      atomic_write_json, read_config, write_config,
      get_asset_config, _init_config,
  )
  ```

### Step 3: Verify

- [ ] **Step 3a:** `py -3 -c "import bot"` - no error.
- [ ] **Step 3b:** `py -3 -m pytest tests/ -x -q` - 606 passed.

### Step 4: Commit

- [ ] **Step 4a:**
  ```
  git add bot_config.py bot.py
  git commit -m "refactor: extract bot_config.py"
  ```

---

## Task 3: Extract bot_db.py

**Files:**
- Create: `bot_db.py`
- Modify: `bot.py`

### Step 1: Write bot_db.py

- [ ] **Step 1a:** Create `bot_db.py`:

```python
"""bot_db.py - SQLite trade log: init, write, update, query."""
import logging
import sqlite3
import time

import aiosqlite

import bot_state

log = logging.getLogger("bot")


def init_db() -> None:
    # (move verbatim from bot.py lines 406-531)
    ...


def test_db_write() -> None:
    # (move verbatim from bot.py lines 534-562)
    ...


async def db_write_trade(trade: dict) -> int | None:
    # (move verbatim from bot.py lines 565-598)
    ...


async def db_update_trade(trade_id: int, fields: dict) -> None:
    # (move verbatim from bot.py lines 601-613)
    ...


async def db_write_market_log(entry: dict) -> None:
    # (move verbatim from bot.py lines 616-636)
    ...


async def db_get_today_pnl(mode: str) -> float:
    # (move verbatim from bot.py lines 639-658)
    ...
```

> All references to `bot_state._DB_FILE` and `bot_state._DATA_DIR` are already in
> place from Task 1. No further changes needed inside the bodies.

### Step 2: Update bot.py

- [ ] **Step 2a:** Delete the six functions from bot.py (lines 406-658).

- [ ] **Step 2b:** Add to bot.py imports:
  ```python
  from bot_db import (
      init_db, test_db_write, db_write_trade,
      db_update_trade, db_write_market_log, db_get_today_pnl,
  )
  ```

### Step 3: Verify

- [ ] **Step 3a:** `py -3 -c "import bot"` - no error.
- [ ] **Step 3b:** `py -3 -m pytest tests/ -x -q` - 606 passed.

### Step 4: Commit

- [ ] **Step 4a:**
  ```
  git add bot_db.py bot.py
  git commit -m "refactor: extract bot_db.py"
  ```

---

## Task 4: Extract bot_notify.py

**Files:**
- Create: `bot_notify.py`
- Modify: `bot.py`

> **Test note:** `tests/test_notify_ctx.py` dynamically loads `bot.py` and accesses
> `_notify_ctx` and `_phase_for_eth`. The re-import line added in Step 2b makes
> them visible as bot.py module attributes. The test keeps passing unchanged.

### Step 1: Write bot_notify.py

- [ ] **Step 1a:** Create `bot_notify.py`:

```python
"""bot_notify.py - Telegram notifications and phase/context helpers."""
import asyncio
import logging

import aiohttp

import bot_state

log = logging.getLogger("bot")


def _phase_for_eth(asset, elapsed_seconds):
    # (move verbatim from bot.py lines 661-675)
    ...


def _notify_ctx(asset, ticker, duration_min=15.0, phase=None):
    # (move verbatim from bot.py lines 678-681)
    ...


async def _maybe_fill_verification_notify(
    order_id, asset, ticker, side, contracts, entry_price_cents, ev
):
    # (move verbatim from bot.py lines 684-737)
    ...


async def send_telegram(text: str) -> None:
    # (move verbatim from bot.py lines 740-766)
    ...
```

> Bodies use `bot_state.TELEGRAM_BOT_TOKEN` and `bot_state.TELEGRAM_CHAT_ID` -
> already rewritten by Task 1.

### Step 2: Update bot.py

- [ ] **Step 2a:** Delete functions `_phase_for_eth`, `_notify_ctx`, `_maybe_fill_verification_notify`, `send_telegram` from bot.py (lines 661-766).

- [ ] **Step 2b:** Add to bot.py imports:
  ```python
  from bot_notify import (
      _phase_for_eth, _notify_ctx,
      _maybe_fill_verification_notify, send_telegram,
  )
  ```

### Step 3: Verify

- [ ] **Step 3a:** `py -3 -c "import bot"` - no error.
- [ ] **Step 3b:** `py -3 -m pytest tests/ -x -q` - 606 passed.
  (test_notify_ctx.py must pass - it tests `_notify_ctx` and `_phase_for_eth`
  via the re-exported names in bot.py's namespace.)

### Step 4: Commit

- [ ] **Step 4a:**
  ```
  git add bot_notify.py bot.py
  git commit -m "refactor: extract bot_notify.py"
  ```

---

## Task 5: Extract bot_kalshi.py

**Files:**
- Create: `bot_kalshi.py`
- Modify: `bot.py`

### Step 1: Write bot_kalshi.py

- [ ] **Step 1a:** Create `bot_kalshi.py`:

```python
"""bot_kalshi.py - RSA auth, Kalshi API calls, price helpers."""
import logging
import os
import re
import sys
import time
from base64 import b64encode

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import bot_state
from bot_config import read_config

log = logging.getLogger("bot")


def load_credentials(mode: str = "paper") -> None:
    # (move verbatim from bot.py lines 770-869)
    # References bot_state.private_key, bot_state.api_key,
    # bot_state.KALSHI_BASE_URL, bot_state.KALSHI_DEMO_BASE_URL - already rewritten.
    ...


def kalshi_headers(method: str, path: str) -> dict:
    # (move verbatim from bot.py lines 872-906)
    # References bot_state.private_key, bot_state.api_key,
    # bot_state.KALSHI_PATH_PREFIX - already rewritten.
    ...


def get_btc_price() -> float | None:
    # (move verbatim from bot.py lines 909-915)
    ...


async def fetch_current_market(
    session: aiohttp.ClientSession, return_all: bool = False
) -> dict | None | list:
    # (move verbatim from bot.py lines 918-1073)
    # References bot_state._market_cache, bot_state._market_cache_ts,
    # bot_state._all_markets_cache, bot_state._all_markets_cache_ts,
    # bot_state.KALSHI_BASE_URL, bot_state.API_TIMEOUT, bot_state.MARKET_CACHE_TTL
    ...


async def fetch_market_for_asset(
    session: aiohttp.ClientSession, asset: str
) -> dict | None:
    # (move verbatim from bot.py lines 1076-1137)
    ...


def parse_strike(market: dict) -> float | None:
    # (move verbatim from bot.py lines 1140-1178)
    ...


def seconds_remaining(market: dict) -> float:
    # (move verbatim from bot.py lines 1181-1190)
    ...


def seconds_elapsed(market: dict) -> float:
    # (move verbatim from bot.py lines 1193-1210)
    ...


async def fetch_orderbook(
    session: aiohttp.ClientSession,
    ticker: str,
    depth: int = 5,
) -> dict | None:
    # (move verbatim from bot.py lines 1213-1390)
    ...


def _simulated_amm_midpoint(btc_price: float, strike: float) -> tuple[float, float]:
    # (move verbatim from bot.py lines 209-229)
    ...


def _log_price_validation(
    market, btc_price, sim_yes_ask, real_yes_ask=None
) -> None:
    # (move verbatim from bot.py lines 232-303)
    # References bot_state._PRICE_VAL_CSV, bot_state._price_val_count,
    # bot_state._price_val_gap_n, bot_state._price_val_sim_sum,
    # bot_state._price_val_real_sum, bot_state._price_val_gap_sum
    ...
```

> `_simulated_amm_midpoint` and `_log_price_validation` are currently at lines 209
> and 232 in bot.py (before `read_config`). They belong in `bot_kalshi.py` per the
> module map. Move them at the same time.

### Step 2: Update bot.py

- [ ] **Step 2a:** Delete all twelve functions listed above from bot.py (lines 209-303 and 770-1390).

- [ ] **Step 2b:** Add to bot.py imports:
  ```python
  from bot_kalshi import (
      load_credentials, kalshi_headers, get_btc_price,
      fetch_current_market, fetch_market_for_asset, parse_strike,
      seconds_remaining, seconds_elapsed, fetch_orderbook,
      _simulated_amm_midpoint, _log_price_validation,
  )
  ```

### Step 3: Verify

- [ ] **Step 3a:** `py -3 -c "import bot"` - no error.
- [ ] **Step 3b:** `py -3 -m pytest tests/ -x -q` - 606 passed.

### Step 4: Commit

- [ ] **Step 4a:**
  ```
  git add bot_kalshi.py bot.py
  git commit -m "refactor: extract bot_kalshi.py"
  ```

---

## Task 6: Extract bot_orders.py

**Files:**
- Create: `bot_orders.py`
- Modify: `bot.py`

### Step 1: Write bot_orders.py

- [ ] **Step 1a:** Create `bot_orders.py`:

```python
"""bot_orders.py - Contract math, order placement, fill verification."""
import asyncio
import logging
import time

import aiohttp

import bot_state
from bot_kalshi import kalshi_headers, fetch_orderbook
from bot_notify import send_telegram, _maybe_fill_verification_notify
from bot_db import db_write_trade, db_update_trade

log = logging.getLogger("bot")


def calculate_contracts(
    config: dict, asset: str, side: str, price_cents: float
) -> int:
    # (move verbatim from bot.py lines 2090-2128)
    ...


def implied_prob(contract_price_cents: float) -> float:
    # (move verbatim from bot.py lines 2131-2138)
    ...


async def _portfolio_has_position(
    session: aiohttp.ClientSession, ticker: str
) -> bool:
    # (move verbatim from bot.py lines 2141-2170)
    ...


async def _verify_order_fill(
    session: aiohttp.ClientSession,
    order_id: str,
    expected_contracts: int,
) -> dict | None:
    # (move verbatim from bot.py lines 2173-2218)
    ...


async def place_order(
    session: aiohttp.ClientSession,
    market: dict,
    side: str,
    contracts: int,
    price_cents: int,
    asset: str,
    ev: float,
    config: dict,
) -> dict | None:
    # (move verbatim from bot.py lines 2221-2596)
    # References bot_state.KALSHI_BASE_URL, bot_state.api_key,
    # bot_state.KALSHI_FEE, bot_state.current_phase, bot_state._brain_cal_s1,
    # bot_state._brain_cal_s2, bot_state._order_attempted_tickers,
    # bot_state.last_action - already rewritten.
    ...
```

### Step 2: Update bot.py

- [ ] **Step 2a:** Delete functions `calculate_contracts`, `implied_prob`, `_portfolio_has_position`, `_verify_order_fill`, `place_order` from bot.py (lines 2090-2596).

- [ ] **Step 2b:** Add to bot.py imports:
  ```python
  from bot_orders import (
      calculate_contracts, implied_prob,
      _portfolio_has_position, _verify_order_fill, place_order,
  )
  ```

### Step 3: Verify

- [ ] **Step 3a:** `py -3 -c "import bot"` - no error.
- [ ] **Step 3b:** `py -3 -m pytest tests/ -x -q` - 606 passed.

### Step 4: Commit

- [ ] **Step 4a:**
  ```
  git add bot_orders.py bot.py
  git commit -m "refactor: extract bot_orders.py"
  ```

---

## Task 7: Extract bot_strategy.py (with dead-code removal)

**Files:**
- Create: `bot_strategy.py`
- Modify: `bot.py` (delete moved functions AND dead code)

> **Dead code removed here** (do NOT carry to new module):
> - `calibrate_from_history` (~lines 1405-1457)
> - `_calibrate_one_brain` (~lines 1460-1517)
> - `calibrate_brain` (~lines 1519-1527)
> - `recalibrate_asset_strategies` (~lines 1529-1578)
> - `_adaptive` dict was already removed in Task 1 (it was a global).

### Step 1: Write bot_strategy.py

- [ ] **Step 1a:** Create `bot_strategy.py`:

```python
"""bot_strategy.py - S1 (BV3 empirical) and S2 (D3 hybrid) strategy brains."""
import logging
import math
import time

import bot_state
from bot_config import read_config, get_asset_config
import asset_manager

log = logging.getLogger("bot")

# Separate logger for Brain v3 decision records
brain_log = logging.getLogger("brain")
brain_log.setLevel(logging.INFO)
brain_log.propagate = False
_brain_fh = logging.FileHandler("brain.log", encoding="utf-8")
_brain_fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
brain_log.addHandler(_brain_fh)


def track_contract_price(ticker: str, price: float) -> None:
    # (move verbatim from bot.py lines 1393-1402)
    # Uses bot_state._contract_price_history - already rewritten.
    ...


def _session_ev_adjustment() -> float:
    # (move verbatim from bot.py lines 1581-1583)
    ...


def _strategy_name_for(asset, duration_min=15.0):
    # (move verbatim from bot.py lines 1592-1594)
    ...


def _get_or_make_strategy_s2(asset: str, config, market_duration_min: float = 15.0):
    # (move verbatim from bot.py lines 1597-1673)
    # Uses bot_state._S2_SINGLETONS, bot_state._config_mtime,
    # bot_state._current_window - already rewritten.
    ...


def strategy_brain_s2(
    asset, market, above, yes_ask_cents, no_ask_cents,
    elapsed_seconds, session, config, obi_monitor=None,
):
    # (move verbatim from bot.py lines 1676-1826)
    ...


def _s1_empirical_win_prob(asset: str, abs_pct: float, mins_left: float) -> float:
    # (move verbatim from bot.py lines 1860-1872)
    # Uses bot_state._S1_ASSET_VOL_RATIO - already rewritten.
    ...


def _s1_calculate_momentum(prices, seconds: int = 180, threshold: float = 0.0005) -> tuple:
    # (move verbatim from bot.py lines 1875-1888)
    ...


def _s1_realized_vol(prices, window_minutes: int = 10) -> float:
    # (move verbatim from bot.py lines 1891-1904)
    ...


def _s1_contract_velocity(ticker: str) -> str:
    # (move verbatim from bot.py lines 1907-1920)
    # Uses bot_state._contract_price_history - already rewritten.
    ...


def strategy_brain_s1(
    asset, market, above, yes_ask_cents, no_ask_cents,
    elapsed_seconds, config, obi_monitor=None,
):
    # (move verbatim from bot.py lines 1923-2087)
    # Uses bot_state._brain_cal_s1, bot_state._obi_monitor,
    # bot_state._funding_monitor_btc, bot_state._funding_monitor_eth - already rewritten.
    ...
```

> `brain_log` and its `FileHandler` are **defined in bot_strategy.py**, not bot.py.
> Remove the `brain_log` setup block from bot.py (lines 54-60) when bot.py is
> cleaned up in Task 12. For now, leave it in bot.py - duplicate loggers with the
> same name share the handler registry; the FileHandler won't double-write.

### Step 2: Update bot.py (move + delete dead code)

- [ ] **Step 2a:** Delete from bot.py:
  - `track_contract_price` (lines 1393-1402)
  - `calibrate_from_history` (lines 1405-1457) - **dead code, do not port**
  - `_calibrate_one_brain` (lines 1460-1517) - **dead code, do not port**
  - `calibrate_brain` (lines 1519-1527) - **dead code, do not port**
  - `recalibrate_asset_strategies` (lines 1529-1578) - **dead code, do not port**
  - `_session_ev_adjustment` (lines 1581-1583)
  - `_strategy_name_for` (lines 1592-1594)
  - `_get_or_make_strategy_s2` (lines 1597-1673)
  - `strategy_brain_s2` (lines 1676-1826)
  - `_s1_empirical_win_prob` (lines 1860-1872)
  - `_s1_calculate_momentum` (lines 1875-1888)
  - `_s1_realized_vol` (lines 1891-1904)
  - `_s1_contract_velocity` (lines 1907-1920)
  - `strategy_brain_s1` (lines 1923-2087)

- [ ] **Step 2b:** Add to bot.py imports:
  ```python
  from bot_strategy import (
      track_contract_price, _session_ev_adjustment, _strategy_name_for,
      _get_or_make_strategy_s2, strategy_brain_s2,
      _s1_empirical_win_prob, _s1_calculate_momentum, _s1_realized_vol,
      _s1_contract_velocity, strategy_brain_s1,
  )
  ```

### Step 3: Verify

- [ ] **Step 3a:** `py -3 -c "import bot"` - no error.
- [ ] **Step 3b:** `py -3 -m pytest tests/ -x -q` - 606 passed.
  (Dead code removal should not affect any test.)

### Step 4: Commit

- [ ] **Step 4a:**
  ```
  git add bot_strategy.py bot.py
  git commit -m "refactor: extract bot_strategy.py; remove dead calibration code"
  ```

---

## Task 8: Extract bot_risk.py

**Files:**
- Create: `bot_risk.py`
- Modify: `bot.py`

> **Test note:** `tests/test_notify_ctx.py` accesses `_parse_strike_from_ticker` via
> the dynamically loaded bot.py module. The re-import in Step 2b preserves that.

### Step 1: Write bot_risk.py

- [ ] **Step 1a:** Create `bot_risk.py`:

```python
"""bot_risk.py - Daily limits, midnight reset, state file, strike parser."""
import logging
import re
import time
from datetime import datetime, timezone, timedelta

import bot_state
from bot_db import db_get_today_pnl
from bot_config import read_config, write_config
from bot_notify import send_telegram

log = logging.getLogger("bot")

# Compiled regexes for ticker strike parsing - live next to _parse_strike_from_ticker
_STRIKE_RE_T_SUFFIX      = re.compile(r"-T(\d+)$")
_STRIKE_RE_NUMERIC_SUFFIX = re.compile(r"-(\d{3,7})$")


async def check_daily_limits(config: dict) -> tuple[bool, str]:
    # (move verbatim from bot.py lines 2599-2654)
    # Uses bot_state.limit_triggered, bot_state.limit_reason,
    # bot_state.pre_limit_mode, bot_state._consecutive_losses - already rewritten.
    ...


def midnight_reset() -> None:
    # (move verbatim from bot.py lines 2657-2686)
    # Uses bot_state.limit_triggered, bot_state.limit_reason,
    # bot_state.pre_limit_mode, bot_state.daily_reset_date,
    # bot_state._consecutive_losses, bot_state._consecutive_price_skips - already rewritten.
    ...


def _parse_strike_from_ticker(ticker):
    # (move verbatim from bot.py lines 2689-2702)
    ...


async def write_state_file(config: dict) -> None:
    # (move verbatim from bot.py lines 2705-2843)
    # Uses many bot_state.* fields - already rewritten.
    ...


async def _log_entry(
    session, asset, ticker, market, above, ev, action, reason,
    contracts=0, price_cents=0, duration_min=15.0
) -> None:
    # (move verbatim from bot.py lines 2846-2872)
    ...
```

### Step 2: Update bot.py

- [ ] **Step 2a:** Delete functions `check_daily_limits`, `midnight_reset`, `_parse_strike_from_ticker`, `write_state_file`, `_log_entry` from bot.py (lines 2599-2872).

- [ ] **Step 2b:** Add to bot.py imports:
  ```python
  from bot_risk import (
      check_daily_limits, midnight_reset, _parse_strike_from_ticker,
      write_state_file, _log_entry,
  )
  ```

### Step 3: Verify

- [ ] **Step 3a:** `py -3 -c "import bot"` - no error.
- [ ] **Step 3b:** `py -3 -m pytest tests/ -x -q` - 606 passed.
  (test_notify_ctx.py's `_parse_strike_from_ticker` tests must pass.)

### Step 4: Commit

- [ ] **Step 4a:**
  ```
  git add bot_risk.py bot.py
  git commit -m "refactor: extract bot_risk.py"
  ```

---

## Task 9: Extract bot_trade.py

**Files:**
- Create: `bot_trade.py`
- Modify: `bot.py`

### Step 1: Write bot_trade.py

- [ ] **Step 1a:** Create `bot_trade.py`:

```python
"""bot_trade.py - S1 trade execution, settlement, orphan recovery."""
import asyncio
import logging
import time

import aiohttp

import bot_state
from bot_orders import place_order, calculate_contracts, implied_prob
from bot_strategy import strategy_brain_s1, track_contract_price, _session_ev_adjustment
from bot_db import db_write_trade, db_update_trade, db_get_today_pnl
from bot_notify import send_telegram, _notify_ctx
from bot_kalshi import (
    fetch_current_market, fetch_orderbook,
    seconds_remaining, seconds_elapsed, parse_strike,
)
from bot_risk import write_state_file, _log_entry, midnight_reset

log = logging.getLogger("bot")


async def _execute_s1_trade(
    session: aiohttp.ClientSession,
    market: dict,
    above: bool,
    asset: str,
    ev: float,
    yes_ask_cents: float,
    no_ask_cents: float,
    contracts: int,
    config: dict,
) -> None:
    # (move verbatim from bot.py lines 2875-2981)
    ...


async def _settle_s1_trade(
    session: aiohttp.ClientSession,
    market: dict,
    asset: str,
    config: dict,
) -> None:
    # (move verbatim from bot.py lines 2984-3037)
    ...


async def _try_settle_orphaned_s1(
    session: aiohttp.ClientSession,
    asset: str,
    config: dict,
) -> None:
    # (move verbatim from bot.py lines 3040-3066)
    ...
```

### Step 2: Update bot.py

- [ ] **Step 2a:** Delete `_execute_s1_trade`, `_settle_s1_trade`, `_try_settle_orphaned_s1` from bot.py (lines 2875-3066).

- [ ] **Step 2b:** Add to bot.py imports:
  ```python
  from bot_trade import _execute_s1_trade, _settle_s1_trade, _try_settle_orphaned_s1
  ```

### Step 3: Verify

- [ ] **Step 3a:** `py -3 -c "import bot"` - no error.
- [ ] **Step 3b:** `py -3 -m pytest tests/ -x -q` - 606 passed.

### Step 4: Commit

- [ ] **Step 4a:**
  ```
  git add bot_trade.py bot.py
  git commit -m "refactor: extract bot_trade.py"
  ```

---

## Task 10: Extract bot_preflight.py

**Files:**
- Create: `bot_preflight.py`
- Modify: `bot.py`

### Step 1: Write bot_preflight.py

- [ ] **Step 1a:** Create `bot_preflight.py`:

```python
"""bot_preflight.py - Startup credential check and preflight verification."""
import logging
import sys

import aiohttp

import bot_state
from bot_kalshi import kalshi_headers, fetch_current_market
from bot_db import init_db
from bot_notify import send_telegram

log = logging.getLogger("bot")


async def verify_kalshi_connection(session: aiohttp.ClientSession) -> None:
    # (move verbatim from bot.py lines 4139-4244)
    # Uses bot_state.KALSHI_BASE_URL, bot_state.API_TIMEOUT,
    # bot_state.api_key - already rewritten.
    ...


async def run_preflight_checks(config: dict) -> None:
    # (move verbatim from bot.py lines 4247-4337)
    ...
```

### Step 2: Update bot.py

- [ ] **Step 2a:** Delete `verify_kalshi_connection` and `run_preflight_checks` from bot.py (lines 4139-4337).

- [ ] **Step 2b:** Add to bot.py imports:
  ```python
  from bot_preflight import verify_kalshi_connection, run_preflight_checks
  ```

### Step 3: Verify

- [ ] **Step 3a:** `py -3 -c "import bot"` - no error.
- [ ] **Step 3b:** `py -3 -m pytest tests/ -x -q` - 606 passed.

### Step 4: Commit

- [ ] **Step 4a:**
  ```
  git add bot_preflight.py bot.py
  git commit -m "refactor: extract bot_preflight.py"
  ```

---

## Task 11: Extract bot_loops.py

**Files:**
- Create: `bot_loops.py`
- Modify: `bot.py`

> This is the largest extraction - ~1,100 lines covering all phase handlers and
> the main trading loop. `_no_data_eval` is a **nested closure** defined inside
> `_process_asset`; it moves with `_process_asset` and is not a top-level function.

### Step 1: Write bot_loops.py

- [ ] **Step 1a:** Create `bot_loops.py`:

```python
"""bot_loops.py - Phase handlers, asset loop, main trading loop."""
import asyncio
import logging
import time
from datetime import datetime, timezone

import aiohttp

import bot_state
import asset_manager
from asset_manager import get_price as _am_get_price, price_age_seconds as _am_price_age
from bot_config import read_config, get_asset_config
from bot_db import db_write_market_log, db_get_today_pnl
from bot_notify import send_telegram, _notify_ctx
from bot_kalshi import (
    fetch_current_market, fetch_market_for_asset, fetch_orderbook,
    seconds_remaining, seconds_elapsed, parse_strike, get_btc_price,
)
from bot_orders import calculate_contracts, implied_prob, place_order
from bot_strategy import (
    strategy_brain_s1, strategy_brain_s2,
    track_contract_price, _session_ev_adjustment, _strategy_name_for,
)
from bot_risk import (
    check_daily_limits, midnight_reset,
    write_state_file, _log_entry,
)
from bot_trade import _execute_s1_trade, _settle_s1_trade, _try_settle_orphaned_s1

log = logging.getLogger("bot")


async def handle_ready_phase(
    session: aiohttp.ClientSession,
    market: dict,
    asset: str,
    config: dict,
) -> None:
    # (move verbatim from bot.py lines 3069-3528)
    ...


async def handle_locked_phase(
    session: aiohttp.ClientSession,
    market: dict,
    asset: str,
    config: dict,
) -> None:
    # (move verbatim from bot.py lines 3531-3691)
    ...


def _init_asset_state(asset: str) -> dict:
    # (move verbatim from bot.py lines 3694-3702)
    ...


async def _process_asset(
    session: aiohttp.ClientSession,
    asset: str,
    config: dict,
) -> None:
    # (move verbatim from bot.py lines 3705-3834)
    # NOTE: _no_data_eval is a nested closure inside this function - it moves here too.
    ...


async def _non_btc_asset_loop(session: aiohttp.ClientSession) -> None:
    # (move verbatim from bot.py lines 3837-3869)
    ...


async def main_loop() -> None:
    # (move verbatim from bot.py lines 3872-4136)
    ...
```

### Step 2: Update bot.py

- [ ] **Step 2a:** Delete all six functions from bot.py (lines 3069-4136).

- [ ] **Step 2b:** Add to bot.py imports:
  ```python
  from bot_loops import (
      handle_ready_phase, handle_locked_phase,
      _init_asset_state, _process_asset,
      _non_btc_asset_loop, main_loop,
  )
  ```

### Step 3: Verify

- [ ] **Step 3a:** `py -3 -c "import bot"` - no error.
- [ ] **Step 3b:** `py -3 -m pytest tests/ -x -q` - 606 passed.

### Step 4: Commit

- [ ] **Step 4a:**
  ```
  git add bot_loops.py bot.py
  git commit -m "refactor: extract bot_loops.py"
  ```

---

## Task 12: bot.py final cleanup + update test_notify_ctx.py

**Files:**
- Modify: `bot.py` (rewrite to ~200-line entrypoint)
- Modify: `tests/test_notify_ctx.py` (load from new modules)

After Tasks 1-11, bot.py still has:
- A large imports section with redundant names (now provided by the new modules)
- The `brain_log` FileHandler setup
- Any lingering `global` declarations
- The `main()` function
- The `if __name__ == "__main__":` block

### Step 1: Rewrite bot.py

- [ ] **Step 1a:** Rewrite `bot.py` to the following (verify each import name is actually used in `main()`):

```python
"""
bot.py - Entrypoint for the Kalshi 15-minute trading bot.

Start via runner.py, not directly.
"""
import asyncio
import logging
import os
import sqlite3
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import aiohttp

import bot_state
import asset_manager
from asset_manager import (
    get_price           as _am_get_price,
    coinbase_price_task,
)
from obi_monitor import OBIMonitor

from bot_config import read_config, _init_config
from bot_db import init_db, test_db_write
from bot_notify import send_telegram
from bot_kalshi import load_credentials, get_btc_price, verify_kalshi_connection
from bot_preflight import run_preflight_checks
from bot_loops import main_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("bot")


async def main() -> None:
    """Bootstrap: load credentials, init DB, start feeds, run main loop."""
    _init_config()
    load_credentials(mode=read_config().get("mode", "paper"))
    init_db()
    test_db_write()

    try:
        conn = sqlite3.connect(bot_state._DB_FILE)
        cleaned = conn.execute(
            """UPDATE trades
               SET outcome         = 'expired_untracked',
                   exit_price_cents = 0,
                   pnl_dollars      = -(COALESCE(entry_price_cents,0) * COALESCE(contracts,1) / 100.0),
                   fill_confirmed   = 0
               WHERE outcome IN ('pending', '', NULL)
                 AND ts < datetime('now', '-30 minutes')"""
        ).rowcount
        conn.commit()
        conn.close()
        if cleaned:
            log.warning(f"Startup cleanup: marked {cleaned} zombie pending trade(s) as expired_untracked")
    except Exception as _e:
        log.warning(f"Startup zombie-trade cleanup failed (non-fatal): {_e}")

    if read_config().get("mode", "paper") != "paper":
        async with aiohttp.ClientSession() as verify_session:
            await verify_kalshi_connection(verify_session)

    _startup_config = read_config()
    _enabled = _startup_config.get("enabled_assets", ["ETH", "SOL", "XRP"])
    _feed_assets = list(dict.fromkeys(["BTC"] + _enabled))
    from asset_manager import seed_price_history
    await seed_price_history(_feed_assets)
    asyncio.create_task(coinbase_price_task(_feed_assets))

    bot_state._obi_monitor = OBIMonitor(["BTC", "ETH", "SOL", "XRP", "DOGE"])
    asyncio.create_task(bot_state._obi_monitor.run())
    log.info("OBI monitor started for BTC, ETH, SOL, XRP, DOGE")

    _src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
    if _src_path not in sys.path:
        sys.path.insert(0, _src_path)
    from strategies.original.signals.funding_dispersion import FundingDispersionMonitor as _FDM
    bot_state._funding_monitor_btc = _FDM("BTC")
    bot_state._funding_monitor_eth = _FDM("ETH")

    async def _funding_refresh_loop() -> None:
        while True:
            try:
                await bot_state._funding_monitor_btc.refresh()
                await bot_state._funding_monitor_eth.refresh()
            except Exception as _fe:
                log.warning(f"funding refresh (BTC/ETH) error: {_fe}")
            await asyncio.sleep(60)

    asyncio.create_task(_funding_refresh_loop())
    log.info("BTC/ETH funding monitors started")

    _first_asset = _enabled[0] if _enabled else "ETH"
    log.info(f"Waiting for price feeds ({_enabled})...")
    waited = 0
    while _am_get_price(_first_asset) is None and waited < 120:
        await asyncio.sleep(1)
        waited += 1
        if waited % 30 == 0:
            log.warning(f"Still waiting for {_first_asset} price feed ({waited}s elapsed)...")
    _first_price = _am_get_price(_first_asset)
    if _first_price is None:
        log.warning("Price feed not available after 120s - continuing anyway.")
    else:
        log.info(f"Price feed ready after {waited}s. {_first_asset}: ${_first_price:,.2f}")

    _startup_cfg = read_config()
    _btc_display = (
        f"${get_btc_price():,.2f}" if get_btc_price() is not None
        else f"{_first_asset}: ${_first_price:,.2f}" if _first_price
        else "price N/A"
    )
    await send_telegram(
        f"<b>Printer bot started</b>\n{_btc_display}\n"
        f"Mode: {_startup_cfg.get('mode','?').upper()}  |  "
        f"Bot enabled: {_startup_cfg.get('bot_enabled', False)}"
    )

    await run_preflight_checks(_startup_cfg)
    await main_loop()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 1b:** Confirm `verify_kalshi_connection` is imported from `bot_preflight` (not `bot_kalshi`) - check the import block you wrote matches the function's home after Task 10.

  > **Note:** `verify_kalshi_connection` moved to `bot_preflight.py` in Task 10.
  > The import above should be `from bot_preflight import run_preflight_checks, verify_kalshi_connection`.

### Step 2: Update test_notify_ctx.py

The test currently loads bot.py dynamically and accesses `_notify_ctx`, `_phase_for_eth`, and `_parse_strike_from_ticker`. Since bot.py no longer defines or re-exports those, update the test to load from the correct modules.

- [ ] **Step 2a:** Rewrite `tests/test_notify_ctx.py`:

```python
"""Tests for ticker parsing and notification-context helpers."""
from bot_risk import _parse_strike_from_ticker
from bot_notify import _notify_ctx, _phase_for_eth


def test_parse_strike_btc_ticker():
    assert _parse_strike_from_ticker("KXBTCD-26APR23-17:00-T94000") == 94000


def test_parse_strike_eth_ticker():
    assert _parse_strike_from_ticker("KXETHD-26APR23-17:00-T3500") == 3500


def test_parse_strike_15m_ticker():
    assert _parse_strike_from_ticker("KXBTC15M-26APR231715-95000") == 95000


def test_parse_strike_no_match_returns_none():
    assert _parse_strike_from_ticker("GIBBERISH-TICKER") is None
    assert _parse_strike_from_ticker("") is None
    assert _parse_strike_from_ticker(None) is None


def test_notify_ctx_15m_no_phase():
    got = _notify_ctx("ETH", "KXETH15M-26APR231715-3500", duration_min=15.0)
    assert got == "[ETH | 15m | KXETH15M-26APR231715-3500]"


def test_notify_ctx_15m_ignores_phase():
    got = _notify_ctx("ETH", "KXETH15M-26APR231715-3500", duration_min=15.0, phase="Dwell")
    assert got == "[ETH | 15m | KXETH15M-26APR231715-3500]"


def test_phase_for_eth_mid():
    assert _phase_for_eth("ETH", elapsed_seconds=600.0) == "Mid"


def test_phase_for_eth_dwell():
    assert _phase_for_eth("ETH", elapsed_seconds=35 * 60) == "Dwell"


def test_phase_for_eth_late():
    assert _phase_for_eth("ETH", elapsed_seconds=50 * 60) == "Late"


def test_phase_for_eth_between():
    assert _phase_for_eth("ETH", elapsed_seconds=20 * 60) is None


def test_phase_for_eth_non_eth_returns_none():
    assert _phase_for_eth("BTC", elapsed_seconds=35 * 60) is None
```

### Step 3: Final verification

- [ ] **Step 3a:** Count lines in the new bot.py:
  ```
  py -3 -c "print(sum(1 for _ in open('bot.py')))"
  ```
  Expected: ~200 (acceptable range 150-250).

- [ ] **Step 3b:** Confirm no `global` declarations remain in any extracted module:
  ```
  py -3 -m grep -rn "^    global " bot_state.py bot_config.py bot_db.py bot_notify.py bot_kalshi.py bot_orders.py bot_strategy.py bot_risk.py bot_trade.py bot_preflight.py bot_loops.py
  ```
  Expected: no output.

- [ ] **Step 3c:** `py -3 -c "import bot"` - no error.

- [ ] **Step 3d:** `py -3 -m pytest tests/ -x -q` - 606 passed.

- [ ] **Step 3e:** Check the module count:
  ```
  py -3 -c "import bot_state, bot_config, bot_db, bot_notify, bot_kalshi, bot_orders, bot_strategy, bot_risk, bot_trade, bot_preflight, bot_loops, bot; print('all imports OK')"
  ```
  Expected: `all imports OK`.

### Step 4: Commit

- [ ] **Step 4a:**
  ```
  git add bot.py tests/test_notify_ctx.py
  git commit -m "refactor: bot.py final cleanup to ~200-line entrypoint"
  ```

---

## Final Acceptance Checklist

- [ ] All 606 tests pass: `py -3 -m pytest tests/ -x -q`
- [ ] `py -3 -c "import bot"` succeeds
- [ ] `git diff --stat HEAD~12 HEAD` shows bot.py at ~200 lines
- [ ] No `global` declarations in extracted modules
- [ ] Dead calibration functions not present in any file:
  ```
  py -3 -m grep -rn "def calibrate_from_history\|def calibrate_brain\|def _calibrate_one_brain\|def recalibrate_asset_strategies" .
  ```
  Expected: no output.
- [ ] 11 new `bot_*.py` files exist in repo root
