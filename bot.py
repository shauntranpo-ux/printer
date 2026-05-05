"""
bot.py — Core trading logic for the Kalshi 15-minute prediction market bot.

Connects to Coinbase for live crypto prices, polls Kalshi for the
soonest-expiring 15-minute markets (ETH, SOL, XRP), evaluates
evidence-based strategy signals, places paper or live orders, and enforces
daily loss / profit limits. Writes bot_state.json every cycle for server.py.

Start via runner.py, not directly.
"""

import asyncio
import json
import math
import sqlite3
import logging
import os
import re
import sys
import tempfile
import time
from base64 import b64encode
from collections import deque
from datetime import datetime, timezone, timedelta

import aiosqlite
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import asset_manager
from asset_manager import (
    ASSET_CONFIG,
    get_price           as _am_get_price,
    price_age_seconds   as _am_price_age,
    coinbase_price_task,
)

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ logging â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("bot")

# Separate logger for Brain v3 decisions — writes to brain.log only
brain_log = logging.getLogger("brain")
brain_log.setLevel(logging.INFO)
brain_log.propagate = False   # don't bleed into the main bot log
_brain_fh = logging.FileHandler("brain.log", encoding="utf-8")
_brain_fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
brain_log.addHandler(_brain_fh)

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ constants â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
KALSHI_LIVE_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_DEMO_BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
KALSHI_BASE_URL      = KALSHI_LIVE_BASE_URL  # overwritten at startup based on mode
KALSHI_PATH_PREFIX = "/trade-api/v2"  # included in signature but not in the path arg
API_TIMEOUT = 10          # seconds for every Kalshi HTTP call
MARKET_CACHE_TTL = 30     # seconds to cache the active market
WATCH_PHASE_SECONDS = 30   # wait 30s into each 15-min session before evaluating

# Kalshi platform fee: ~7c per $1 contract, subtracted from gross EV.
# Without this, every EV calculation was 7% optimistic — trades that looked
# +5% edge were actually -2% after fees.
KALSHI_FEE = 0.07


# â”€â”€ Telegram notifications (optional — set env vars to enable) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "")

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ file paths (env-overridable for multi-strategy) â”€â”€
_CONFIG_FILE = os.environ.get("BOT_CONFIG_FILE", "config.json")
_DB_FILE     = os.environ.get("BOT_DB_FILE",     "kalshi_bot.db")
_STATE_FILE  = os.environ.get("BOT_STATE_FILE",  "bot_state.json")

# Derive data directory from DB path so all persistent files land together.
# On Railway: BOT_DB_FILE=/app/data/kalshi_bot.db  → _DATA_DIR=/app/data (volume).
# Local dev:  BOT_DB_FILE not set, DB at cwd/kalshi_bot.db → _DATA_DIR=cwd.
_DATA_DIR = os.path.dirname(os.path.abspath(_DB_FILE))

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ global state â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# btc_prices is an alias to asset_manager's BTC price deque.
# All existing code that reads btc_prices (momentum, vol, feed) works unchanged.
btc_prices: deque = asset_manager._prices["BTC"]

private_key = None    # loaded on startup
api_key: str = ""

# Market / phase
current_market: dict | None = None
current_phase: str = "DONE"   # WATCH | READY | LOCKED | DONE
current_position: dict | None = None
_order_attempted_tickers: set = set()  # tickers where an order was attempted this session
_asset_states: dict[str, dict] = {}    # per-asset state dicts for non-BTC assets

# Market cache
_market_cache: dict | None = None
_market_cache_ts: float = 0.0
_all_markets_cache: list = []
_all_markets_cache_ts: float = 0.0

# Daily-limit tracking
limit_triggered: bool = False
limit_reason: str = ""
pre_limit_mode: str | None = None
daily_reset_date = None

# Price-validator CSV — counts rows collected and running totals for summary
_PRICE_VAL_CSV = os.path.join(_DATA_DIR, "price_validation_log.csv")
_price_val_count: int = 0
_price_val_gap_n: int = 0      # rows where real_yes is not None (valid gap count)
_price_val_sim_sum: float = 0.0
_price_val_real_sum: float = 0.0
_price_val_gap_sum: float = 0.0


# Last evaluation info (written to state file)
last_confidence_score: int = 0
last_confidence_breakdown: dict = {}
last_action: str = ""
last_skip_reason: str = ""
# Per-asset eval snapshots for dashboard market cards (keyed by asset ticker)
_asset_eval: dict = {}

# Contract price history — tracks YES ask over time per ticker for velocity signal
_contract_price_history: dict = {}   # ticker → deque[(ts, price)]

# Adaptive weights — updated every 20 completed trades from DB analysis
_adaptive: dict = {
    "last_calibrated_count": 0,
    "low_price_wins":    False,
    "near_strike_wins":  False,
    "threshold_adjust":  0,
}

# Brain self-calibration — updated every 5 completed trades
_brain_cal: dict = {
    "last_count":        0,
    "prob_scale":        1.0,   # multiplies our true_prob estimate (learned correction)
    "min_edge_override": None,  # if set, overrides the 20% default
    "confidence_bonus":  0,     # added to confidence score as a reward
    "reward_tier":       0,     # 0=none 1=good(>50%) 2=great(>75%) 3=max(>85%)
    "overall_wr":        0.0,   # tracked for dashboard display
    # condition win rates: key = "dist_time_mom" → [wins, total]
    "condition_wr":      {},
    # momentum direction performance
    "bullish_wr":        0.5,
    "bearish_wr":        0.5,
}

# Config cache — fallback if config.json is corrupt mid-write
_last_good_config: dict | None = None

_consecutive_losses: int = 0

# Consecutive price-filter skip counter (triggers Telegram warning at 20)
_consecutive_price_skips: int = 0


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Atomic JSON write utility
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def atomic_write_json(data: dict, path: str) -> None:
    """
    Write JSON atomically: serialize to a sibling temp file, fsync, then
    os.replace() which is atomic on POSIX and near-atomic on Windows (NTFS
    guarantees no partial-read exposure because replace is a rename).
    Cleans up the temp file on any failure so no orphaned .tmp files linger.
    """
    dir_ = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Price-validator CSV logger
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

import csv as _csv_module  # import here to keep top-level imports clean


def _simulated_amm_midpoint(btc_price: float, strike: float) -> tuple[float, float]:
    """
    Deterministic midpoint of simulate_amm_prices() — same distance bands as
    backtest.py but without random noise, so each call is reproducible.
    Used to record what the backtest *expects* vs what Kalshi actually quotes.
    """
    pct   = (btc_price - strike) / strike
    ap    = abs(pct) * 100
    above = pct > 0
    spread = 4.5  # midpoint of backtest's 3.0—6.0 spread

    if ap < 0.10:
        yes_ask = 51.5 if above else 48.5
    elif ap < 0.30:
        yes_ask = 68.5 if above else 31.5
    else:
        yes_ask = 84.5 if above else 15.5

    yes_ask = max(3.0, min(97.0, yes_ask))
    no_ask  = max(3.0, min(97.0, 100.0 + spread - yes_ask))
    return yes_ask, no_ask


def _log_price_validation(
    ts: str,
    ticker: str,
    btc_price: float,
    strike: float,
    sim_yes: float,
    sim_no: float,
    real_yes: float | None,
    real_no: float | None,
    mins_remaining: float = 0.0,
) -> None:
    """
    Append one row to price_validation_log.csv and print a running summary
    every 50 entries.  Logs null for real prices if the API call failed.

    Columns: ts, ticker, btc_price, strike, abs_pct, mins_remaining,
             sim_yes_ask, sim_no_ask, real_yes_ask, real_no_ask, price_gap_cents
    """
    global _price_val_count, _price_val_gap_n, _price_val_sim_sum, _price_val_real_sum, _price_val_gap_sum

    gap     = (real_yes - sim_yes) if (real_yes is not None) else None
    abs_pct = round(abs((btc_price - strike) / strike) * 100, 4) if strike else 0.0

    file_exists = os.path.isfile(_PRICE_VAL_CSV)
    try:
        with open(_PRICE_VAL_CSV, "a", newline="", encoding="utf-8") as fh:
            writer = _csv_module.writer(fh)
            if not file_exists:
                writer.writerow([
                    "ts", "ticker", "btc_price", "strike",
                    "abs_pct", "mins_remaining",
                    "sim_yes_ask", "sim_no_ask",
                    "real_yes_ask", "real_no_ask",
                    "price_gap_cents",
                ])
            writer.writerow([
                ts, ticker, round(btc_price, 2), round(strike, 2),
                abs_pct, round(mins_remaining, 2),
                round(sim_yes, 1), round(sim_no, 1),
                round(real_yes, 1) if real_yes is not None else "null",
                round(real_no,  1) if real_no  is not None else "null",
                round(gap, 1) if gap is not None else "null",
            ])
    except Exception as exc:
        log.warning(f"Price validation CSV write error: {exc}")
        return

    _price_val_count += 1
    _price_val_sim_sum += sim_yes
    if real_yes is not None:
        _price_val_real_sum += real_yes
        _price_val_gap_sum  += gap
        _price_val_gap_n    += 1

    if _price_val_count % 50 == 0:
        n        = _price_val_count
        avg_sim  = _price_val_sim_sum / n
        avg_real = _price_val_real_sum / _price_val_gap_n if _price_val_gap_n > 0 else 0.0
        avg_gap  = _price_val_gap_sum  / _price_val_gap_n if _price_val_gap_n > 0 else 0.0
        verdict  = ("✓ within 3c" if abs(avg_gap) < 3
                    else "⚠ 3-7c gap — edge marginal" if abs(avg_gap) < 7
                    else "✗ >7c gap — strategy likely unprofitable")
        log.info(
            f"Price validation: {n} samples collected. "
            f"Avg price gap: {avg_gap:+.1f}c. "
            f"Simulated avg: {avg_sim:.1f}c. Real avg: {avg_real:.1f}c. "
            f"{verdict}"
        )


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Config helpers
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def read_config() -> dict:
    """Read and return the contents of the config file.
    Falls back to _last_good_config if the file is transiently corrupt
    (e.g. a partial write in progress). Raises only on startup failure
    when no cached config is available yet.
    """
    global _last_good_config
    try:
        with open(_CONFIG_FILE, "r") as fh:
            cfg = json.load(fh)
        cfg.setdefault("enabled_assets", ["ETH", "SOL", "XRP"])
        _last_good_config = cfg
        return cfg
    except json.JSONDecodeError as exc:
        log.warning(f"Config JSON decode error: {exc}. Using last known good config.")
        if _last_good_config is not None:
            return _last_good_config
        raise
    except Exception as exc:
        log.warning(f"Config read error: {exc}. Using last known good config.")
        if _last_good_config is not None:
            return _last_good_config
        raise


def write_config(data: dict) -> None:
    """Write a dict to the config file atomically (temp+replace)."""
    atomic_write_json(data, _CONFIG_FILE)


def get_asset_config(config: dict, asset: str, field: str, default=None):
    """Get config value — asset override if present, else global value, else default."""
    overrides = config.get("asset_overrides", {}).get(asset, {})
    if field in overrides:
        return overrides[field]
    return config.get(field, default)


def _init_config() -> None:
    """
    Create config.json on startup if missing, and apply Railway env var overrides.

    Set BOT_MODE=live and BOT_ENABLED=true in Railway environment variables
    so live mode survives every redeploy without manual editing.
    Daily loss limits still work — they set limit_triggered in memory which
    is checked independently of the mode flag.
    """
    # Build defaults
    defaults = {
        "bot_enabled": False,
        "trade_amount_dollars": 25,
        "mode": "paper",
        "confidence_threshold": 0,   # Supertrend win_prob is fixed 0.70; real gate is EV
        "daily_loss_limit_dollars": 50,          # 2× trade size — real guard, not $5M decoration
        "daily_profit_target_dollars": 200,
        "max_consecutive_losses": 5,             # pause 15 min after this many losses in a row
        "enable_reversal_signal": False,         # disabled by default — no backtested evidence yet
        "min_ev_base": 8,                        # EV gate; fee formula fix may allow lower — tune via backtest
        "kalshi_fee_per_contract_cents": 7,      # Kalshi platform fee; update if pricing changes
        "preflight_override": False,             # set true ONLY to bypass pre-flight hard stop — not recommended
    }

    # Load existing config or start from defaults
    if os.path.exists(_CONFIG_FILE):
        try:
            with open(_CONFIG_FILE) as fh:
                cfg = json.load(fh)
        except Exception:
            cfg = defaults.copy()
    else:
        cfg = defaults.copy()

    # Persistent bot_enabled from Railway volume — survives redeploys
    # (written by server.py whenever the dashboard toggle changes)
    _data_dir = os.path.dirname(os.path.abspath(_DB_FILE))
    _be_state = os.path.join(_data_dir, "bot_enabled.state")
    if os.path.exists(_be_state):
        try:
            cfg["bot_enabled"] = open(_be_state).read().strip() == "1"
        except Exception:
            pass

    # Railway env var overrides — highest priority, set once, persist forever
    if "BOT_MODE" in os.environ:
        cfg["mode"] = os.environ["BOT_MODE"].strip().lower()
    if "BOT_ENABLED" in os.environ:
        cfg["bot_enabled"] = os.environ["BOT_ENABLED"].strip().lower() in ("1", "true", "yes")

    # Fill in any missing keys with defaults
    for k, v in defaults.items():
        cfg.setdefault(k, v)

    write_config(cfg)
    log.info(f"Config ready: mode={cfg['mode']} enabled={cfg['bot_enabled']}")


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Database
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def init_db() -> None:
    """Create the database and all required tables if they do not exist."""
    try:
        conn = sqlite3.connect(_DB_FILE)
        conn.execute("PRAGMA journal_mode=WAL")
        c = conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                    TEXT,
                market_id             TEXT,
                market_title          TEXT,
                mode                  TEXT,
                side                  TEXT,
                contracts             INTEGER,
                entry_price_cents     INTEGER,
                trade_amount_dollars  REAL,
                confidence_score      INTEGER,
                model_prob            REAL,
                implied_prob          REAL,
                btc_price_at_entry    REAL,
                strike                REAL,
                seconds_left_at_entry INTEGER,
                fill_confirmed        INTEGER,
                exit_price_cents      INTEGER,
                exit_reason           TEXT,
                outcome               TEXT DEFAULT 'pending',
                pnl_dollars           REAL,
                profit_percent        REAL,
                order_id              TEXT,
                asset                 TEXT DEFAULT 'BTC',
                raw_p_yes             REAL,
                entry_signals         TEXT,
                strategy_variant      TEXT DEFAULT 'strategy2'
            )
        """)

        conn.commit()

        c.execute("""
            CREATE TABLE IF NOT EXISTS market_log (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                   TEXT,
                market_id            TEXT,
                market_title         TEXT,
                phase                TEXT,
                seconds_left         INTEGER,
                btc_price            REAL,
                strike               REAL,
                contract_price_cents INTEGER,
                confidence_score     INTEGER,
                action               TEXT,
                skip_reason          TEXT,
                mode                 TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_summary (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                date               TEXT,
                mode               TEXT,
                markets_seen       INTEGER,
                markets_traded     INTEGER,
                markets_skipped    INTEGER,
                wins               INTEGER,
                losses             INTEGER,
                total_pnl_dollars  REAL,
                avg_confidence     REAL,
                avg_profit_percent REAL,
                opening_balance    REAL,
                closing_balance    REAL
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS stress_test_results (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                run_ts                TEXT,
                start_date            TEXT,
                end_date              TEXT,
                total_markets         INTEGER,
                total_trades          INTEGER,
                win_rate              REAL,
                total_pnl_dollars     REAL,
                max_drawdown_percent  REAL,
                avg_confidence        REAL,
                avg_profit_percent    REAL,
                sharpe_ratio          REAL,
                max_consecutive_losses INTEGER
            )
        """)

        # Migrate existing DB — add new columns if not present
        for col, typedef in (
            ("order_id",          "TEXT"),
            ("asset",             "TEXT DEFAULT 'BTC'"),  # multi-asset support
            ("raw_p_yes",         "REAL"),                # pre-calibration P(YES wins)
            ("entry_signals",    "TEXT"),                # JSON snapshot of entry signals
            ("calibrated_p_yes",  "REAL"),               # post-calibration p_yes used in EV gate
            ("signal_name",       "TEXT"),               # which signal fired (d3_hybrid / supertrend)
            ("strategy_variant",  "TEXT DEFAULT 'strategy2'"),  # strategy1=original, strategy2=D3 hybrid
        ):
            try:
                c.execute(f"ALTER TABLE trades ADD COLUMN {col} {typedef}")
            except Exception:
                pass  # column already exists

        # Drop dead columns (SQLite 3.35+)
        existing_cols = {row[1] for row in c.execute("PRAGMA table_info(trades)")}
        for dead_col in ("claude_confidence", "stop_loss_price_cents", "claude_signals"):
            if dead_col in existing_cols:
                try:
                    c.execute(f"ALTER TABLE trades DROP COLUMN {dead_col}")
                    log.info("DB: dropped dead column %s from trades", dead_col)
                except Exception as exc:
                    log.warning("DB: could not drop column %s: %s", dead_col, exc)

        conn.commit()
        conn.close()
        log.info("Database initialized.")
    except Exception as exc:
        log.error(f"DB init error: {exc}")
        raise


def test_db_write() -> None:
    """
    Smoke-test the DB pipeline at startup: write a sentinel row, read it back,
    delete it.  Halts the bot (exit code 2) on any failure so runner.py won't
    silently loop on a broken DB.
    """
    try:
        conn = sqlite3.connect(_DB_FILE)
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO trades (ts, market_id, mode, outcome) VALUES (?, ?, ?, ?)",
            ("_selftest_", "_selftest_", "_selftest_", "_selftest_"),
        )
        conn.commit()
        test_id = cur.lastrowid
        cur.execute("SELECT id FROM trades WHERE id = ?", (test_id,))
        row = cur.fetchone()
        if not row or row[0] != test_id:
            raise RuntimeError(f"read-back mismatch: expected {test_id}, got {row}")
        cur.execute("DELETE FROM trades WHERE id = ?", (test_id,))
        conn.commit()
        conn.close()
        log.info(f"DB self-test PASSED  path={os.path.abspath(_DB_FILE)}")
    except Exception as exc:
        log.error(f"DB self-test FAILED: {exc}")
        log.error(f"DB path: {os.path.abspath(_DB_FILE)}")
        log.error("Cannot write trades — halting to prevent silent data loss.")
        sys.exit(2)


async def db_write_trade(trade: dict) -> int | None:
    """Insert a trade record. Returns the new row id."""
    try:
        async with aiosqlite.connect(_DB_FILE) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            cur = await db.execute("""
                INSERT INTO trades (
                    ts, market_id, market_title, mode, side, contracts,
                    entry_price_cents, trade_amount_dollars, confidence_score,
                    model_prob, implied_prob, btc_price_at_entry, strike,
                    seconds_left_at_entry, fill_confirmed,
                    exit_price_cents, exit_reason, outcome, pnl_dollars, profit_percent,
                    order_id, asset, raw_p_yes, entry_signals, strategy_variant
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                trade.get("ts"), trade.get("market_id"), trade.get("market_title"),
                trade.get("mode"), trade.get("side"), trade.get("contracts"),
                trade.get("entry_price_cents"), trade.get("trade_amount_dollars"),
                trade.get("confidence_score"), trade.get("model_prob"),
                trade.get("implied_prob"), trade.get("btc_price_at_entry"),
                trade.get("strike"), trade.get("seconds_left_at_entry"),
                trade.get("fill_confirmed"),
                trade.get("exit_price_cents"), trade.get("exit_reason"),
                trade.get("outcome", "pending"), trade.get("pnl_dollars"),
                trade.get("profit_percent"),
                trade.get("order_id"), trade.get("asset", "BTC"),
                trade.get("raw_p_yes"), trade.get("entry_signals"),
                trade.get("strategy_variant", "strategy2"),
            ))
            await db.commit()
            return cur.lastrowid
    except Exception as exc:
        log.error(f"DB write_trade error: {exc}")
        return None


async def db_update_trade(trade_id: int, fields: dict) -> None:
    """Update named columns on an existing trade row."""
    try:
        async with aiosqlite.connect(_DB_FILE) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            set_clause = ", ".join(f"{k} = ?" for k in fields)
            await db.execute(
                f"UPDATE trades SET {set_clause} WHERE id = ?",
                list(fields.values()) + [trade_id],
            )
            await db.commit()
    except Exception as exc:
        log.error(f"DB update_trade error: {exc}")


async def db_write_market_log(entry: dict) -> None:
    """Append one row to market_log."""
    try:
        async with aiosqlite.connect(_DB_FILE) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("""
                INSERT INTO market_log (
                    ts, market_id, market_title, phase, seconds_left, btc_price,
                    strike, contract_price_cents, confidence_score, action,
                    skip_reason, mode
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                entry.get("ts"), entry.get("market_id"), entry.get("market_title"),
                entry.get("phase"), entry.get("seconds_left"), entry.get("btc_price"),
                entry.get("strike"), entry.get("contract_price_cents"),
                entry.get("confidence_score"), entry.get("action"),
                entry.get("skip_reason"), entry.get("mode"),
            ))
            await db.commit()
    except Exception as exc:
        log.error(f"DB write_market_log error: {exc}")


async def db_get_today_pnl(mode: str) -> float:
    """Sum pnl_dollars for completed trades in the given mode today (UTC)."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        async with aiosqlite.connect(_DB_FILE) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            async with db.execute(
                "SELECT COALESCE(SUM(pnl_dollars), 0) FROM trades "
                "WHERE mode = ? AND ts LIKE ? AND outcome != 'pending'",
                (mode, f"{today}%"),
            ) as cur:
                row = await cur.fetchone()
        return float(row[0]) if row else 0.0
    except Exception as exc:
        log.error(f"DB get_today_pnl error: {exc}")
        return 0.0


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Kalshi auth
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def _phase_for_eth(asset, elapsed_seconds):
    """Return ETH hourly window-phase label ('Mid'/'Dwell'/'Late') or None.

    BTC and all 15m markets return None.
    """
    if asset != "ETH":
        return None
    m = elapsed_seconds / 60.0
    if 9 <= m <= 11:
        return "Mid"
    if 30 <= m <= 42:
        return "Dwell"
    if m >= 45:
        return "Late"
    return None


def _notify_ctx(asset, ticker, duration_min=15.0, phase=None):
    """Format a context prefix for Telegram notifications."""
    parts = [asset, "15m", ticker]
    return f"[{' | '.join(parts)}]"


async def _maybe_fill_verification_notify(
    asset: str,
    ticker: str,
    side: str,
    market: dict | None,
    secs_left: float,
    entry_price_cents: int | None,
    price_this_attempt: int | None,
    market_ask_at_post_c: int | None,
    fill_yes_price: int | None,
) -> None:
    """Send a fill-verification Telegram message to spot price-selection bugs in flight.

    Compares:
      - Target:     the strategy-chosen entry price (may be None for BTC).
      - Market ask: the ask observed just before POST.
      - Posted:     the price actually sent to Kalshi (may differ via retry drift).
      - Filled:     the price Kalshi returned on fill.

    Warns with ⚠️ when abs(filled - target) > 3¢. Silently skips when fill_yes_price is None.
    """
    if fill_yes_price is None:
        return
    try:
        _elapsed_sec = seconds_elapsed(market) if market else 0.0
    except Exception:
        _elapsed_sec = 0.0
    _target = entry_price_cents  # may be None for BTC (strategy doesn't emit it)
    _ask = market_ask_at_post_c
    _posted = price_this_attempt
    _filled = fill_yes_price
    _target_str = f"{int(round(_target))}¢" if _target is not None else "—"
    _ask_str    = f"{int(round(_ask))}¢"    if _ask    is not None else "—"
    _posted_str = f"{int(round(_posted))}¢" if _posted is not None else "—"
    _filled_str = f"{int(round(_filled))}¢"
    if _target is not None:
        _slip_target = int(round(_filled - _target))
        _slip_target_str = f"{_slip_target:+d}¢ vs target"
        _warn = "⚠️ " if abs(_slip_target) > 3 else "🎯 "
    else:
        _slip_target_str = "n/a vs target"
        _warn = "🎯 "
    _slip_market_str = (
        f"{int(round(_filled - _ask)):+d}¢ vs market" if _ask is not None else "n/a vs market"
    )
    _ctx = _notify_ctx(asset, ticker)
    await send_telegram(
        f"{_warn}<b>{_ctx} FILL VERIFICATION</b>\n"
        f"Target:     <b>{_target_str}</b>\n"
        f"Market ask: {_ask_str}\n"
        f"Posted:     {_posted_str}\n"
        f"Filled:     <b>{_filled_str}</b>\n"
        f"Slippage:   {_slip_target_str}  |  {_slip_market_str}"
    )


async def send_telegram(text: str) -> None:
    """Send a Telegram notification with up to 3 retries on failure."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return  # silently skip — Telegram is optional
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for attempt in range(1, 4):
        try:
            log.info(f"Telegram: sending (attempt {attempt}/3)…")
            async with aiohttp.ClientSession() as tg:
                async with tg.post(
                    url,
                    json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    body = await resp.text()
                    if resp.status == 200:
                        log.info("Telegram: sent OK")
                        return
                    elif resp.status == 429:
                        log.warning(f"Telegram: rate-limited (429) — attempt {attempt}/3, retrying…")
                    else:
                        log.warning(f"Telegram: HTTP {resp.status} — {body}")
                        return  # non-retryable HTTP error
        except Exception as exc:
            log.warning(f"Telegram: error on attempt {attempt}/3 — {exc}")
        if attempt < 3:
            await asyncio.sleep(2)
    log.error("Telegram: failed after 3 attempts — notification dropped")


def load_credentials(mode: str = "paper") -> None:
    """
    Load Kalshi API credentials from environment variables based on active mode.

    paper — skips credential loading (no API calls needed)
    live  — loads KALSHI_API_KEY + KALSHI_PRIVATE_KEY, routes to live endpoint
    demo  — loads KALSHI_DEMO_API_KEY + KALSHI_DEMO_PRIVATE_KEY, routes to demo endpoint

    Credential values may be a PEM string or a path to a PEM file.
    Exits with a clear error message if required variables are missing.
    """
    global api_key, private_key, KALSHI_BASE_URL

    if mode == "paper":
        # Paper mode simulates fills but still reads real Kalshi market data.
        # Load live credentials if present so market fetch/orderbook calls work.
        # Non-fatal if missing — bot runs fully simulated without them.
        _key = os.environ.get("KALSHI_API_KEY", "").strip()
        _pem = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()
        if _key and _pem:
            api_key = _key
            try:
                pem_bytes = open(_pem, "rb").read() if os.path.exists(_pem) else _pem.encode()
                private_key = serialization.load_pem_private_key(pem_bytes, password=None)
                log.info("Paper mode: Kalshi credentials loaded for market data access.")
            except Exception as exc:
                log.warning(f"Paper mode: credential load failed ({exc}) — market data unavailable.")
        return

    if mode == "demo":
        KALSHI_BASE_URL = KALSHI_DEMO_BASE_URL
        key_id_var = "KALSHI_DEMO_API_KEY"
        pem_var    = "KALSHI_DEMO_PRIVATE_KEY"
        label      = "DEMO"
    else:  # live
        KALSHI_BASE_URL = KALSHI_LIVE_BASE_URL
        key_id_var = "KALSHI_API_KEY"
        pem_var    = "KALSHI_PRIVATE_KEY"
        label      = "LIVE"

    api_key = os.environ.get(key_id_var, "").strip()
    pem_val = os.environ.get(pem_var, "").strip()

    if not api_key or not pem_val:
        missing = key_id_var if not api_key else pem_var
        if mode == "demo":
            # Demo creds not set in environment — degrade gracefully to paper mode
            # so the bot doesn't crash-loop. Set KALSHI_DEMO_API_KEY and
            # KALSHI_DEMO_PRIVATE_KEY in Railway to enable demo trading.
            log.warning(
                f"{missing} not set — DEMO mode requires demo credentials. "
                f"Falling back to paper mode. Add the env vars in Railway to enable demo."
            )
            try:
                cfg = read_config()
                cfg["mode"] = "paper"
                with open(_CONFIG_FILE, "w", encoding="utf-8") as fh:
                    import json as _json
                    _json.dump(cfg, fh, indent=2)
            except Exception as _ce:
                log.warning(f"Could not write paper fallback to config: {_ce}")
            # Load live creds if available so market data still works
            _key = os.environ.get("KALSHI_API_KEY", "").strip()
            _pem = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()
            if _key and _pem:
                api_key = _key
                try:
                    pem_bytes = open(_pem, "rb").read() if os.path.exists(_pem) else _pem.encode()
                    private_key = serialization.load_pem_private_key(pem_bytes, password=None)
                    log.info("Paper fallback: loaded live credentials for market data access.")
                except Exception as exc:
                    log.warning(f"Paper fallback: live credential load failed ({exc}).")
            KALSHI_BASE_URL = KALSHI_LIVE_BASE_URL
            return
        else:
            print(f"ERROR: {missing} is not set (required for {label} mode).")
            sys.exit(1)

    # Safety assertions — fail loudly rather than silently routing to the wrong endpoint
    if mode == "demo" and KALSHI_BASE_URL != KALSHI_DEMO_BASE_URL:
        print(f"SAFETY ERROR: demo mode must use demo URL; got {KALSHI_BASE_URL}")
        sys.exit(1)
    if mode == "live" and KALSHI_BASE_URL != KALSHI_LIVE_BASE_URL:
        print(f"SAFETY ERROR: live mode must use live URL; got {KALSHI_BASE_URL}")
        sys.exit(1)

    if os.path.exists(pem_val):
        with open(pem_val, "rb") as fh:
            pem_bytes = fh.read()
        log.info(f"Loaded {label} private key from file: {pem_val}")
    else:
        pem_bytes = pem_val.encode()
        log.info(f"Loaded {label} private key from environment variable string.")

    private_key = serialization.load_pem_private_key(pem_bytes, password=None)

    masked = api_key[:6] + "..." if len(api_key) > 6 else "***"
    print(f"[{label} MODE] Base URL : {KALSHI_BASE_URL}")
    print(f"[{label} MODE] API key  : {masked}")
    log.info(f"{label} credentials loaded successfully.")


def kalshi_headers(method: str, path: str) -> dict:
    """
    Generate the three Kalshi authentication headers for one request.

    Args:
        method: HTTP method in uppercase (e.g. 'GET', 'POST').
        path:   URL path without the base URL (e.g. '/markets').

    Returns:
        Dict of header name → value.
    """
    ts = str(int(time.time() * 1000))
    # Kalshi signs the full URL path including the /trade-api/v2 prefix
    full_path = KALSHI_PATH_PREFIX + path
    msg = (ts + method.upper() + full_path).encode()
    sig = b64encode(
        private_key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
    ).decode()
    return {
        "KALSHI-ACCESS-KEY": api_key,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Content-Type": "application/json",
    }


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  BTC price feed
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def get_btc_price() -> float | None:
    """Return the most recent BTC price, or None if no data received yet."""
    return _am_get_price("BTC")


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Market fetching
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

async def fetch_current_market(session: aiohttp.ClientSession, return_all: bool = False) -> dict | None | list:
    """
    Fetch open BTC 15-minute market(s) from Kalshi.
    Results are cached for MARKET_CACHE_TTL seconds to avoid hammering the API.

    Args:
        return_all: if True, return the full sorted list of valid markets.
                    if False (default), return only the soonest-expiring one.

    Returns:
        Market dict (or None) when return_all=False.
        List of market dicts (possibly empty) when return_all=True.
    """
    global _market_cache, _market_cache_ts, _all_markets_cache, _all_markets_cache_ts

    now = time.time()
    if _market_cache and (now - _market_cache_ts) < MARKET_CACHE_TTL:
        return _all_markets_cache if return_all else _market_cache

    path = "/markets"
    _SERIES_SEARCH_ORDER = ("KXBTC15M", "KXBTCD", "BTCD-B")



    all_markets = []
    seen_tickers: set[str] = set()
    for series in _SERIES_SEARCH_ORDER:
        params = {"series_ticker": series, "status": "open", "limit": 20}
        try:
            async with session.get(
                KALSHI_BASE_URL + path,
                headers=kalshi_headers("GET", path),
                params=params,
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.error(f"Market fetch HTTP {resp.status} (series={series}): {body[:300]}")
                    continue
                data = await resp.json()
            batch = data.get("markets", [])
            new_count = 0
            for m in batch:
                t = m.get("ticker", "")
                if t and t not in seen_tickers:
                    seen_tickers.add(t)
                    all_markets.append(m)
                    new_count += 1
            if new_count:
                log.info(f"Series {series!r} returned {new_count} new markets: "
                         + ", ".join(m.get("ticker", "?") for m in batch[:5]))
        except Exception as exc:
            log.error(f"Market fetch error (series={series}): {exc}")

    if not all_markets:
        log.warning("All series tickers returned no markets.")
        return _all_markets_cache if return_all else _market_cache

    # Log all markets with their close times so we can see what's available
    now_utc = datetime.now(timezone.utc)
    for m in all_markets:
        try:
            close_dt = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
            mins_left = (close_dt - now_utc).total_seconds() / 60
        except Exception:
            mins_left = -1
        log.info(f"  Market: {m.get('ticker')} | closes in {mins_left:.1f}m | {m.get('title','')[:60]}")

    # Drop obviously long-duration markets by title keyword.
    # "range" = KXBTC daily price-range markets (~1500 min). Keep "above/below" (KXBTCD).
    all_markets = [m for m in all_markets
                   if "range" not in m.get("title", "").lower()
                   and "daily" not in m.get("title", "").lower()]

    if not all_markets:
        log.warning("No valid short-duration markets after title filtering. Waiting for next window.")
        return [] if return_all else None

    # Compute duration for each market
    def market_duration_minutes(m):
        try:
            open_str  = m.get("open_time") or m.get("open_date")
            close_str = m.get("close_time")
            if not open_str or not close_str:
                return None
            open_dt  = datetime.fromisoformat(open_str.replace("Z", "+00:00"))
            close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
            return (close_dt - open_dt).total_seconds() / 60
        except Exception:
            return None

    # Accept 15-min markets (reject anything > 20 min)
    short_dur = [m for m in all_markets
                 if (lambda d: d is not None and 1 <= d <= 20)(market_duration_minutes(m))]

    if short_dur:
        log.info(f"Found {len(short_dur)} short-duration market(s). "
                 + " | ".join(f"{m.get('ticker')} {market_duration_minutes(m):.0f}m" for m in short_dur[:5]))
        pool = short_dur
    else:
        # Fall back: any market closing within 60 minutes
        soon = [
            m for m in all_markets
            if (lambda c: 0 < c <= 60)(
                (datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")) - now_utc).total_seconds() / 60
                if m.get("close_time") else -1
            )
        ]
        if soon:
            log.info(f"No short-duration match by open→close — using {len(soon)} markets closing within 60 min.")
            pool = soon
        else:
            log.warning("No short-duration markets found. Waiting for next window.")
            return [] if return_all else None

    pool.sort(key=lambda m: m.get("close_time", ""))

    # Filter out markets that have already closed — sort picks earliest close_time
    # first, which can be an expired market when multiple windows are returned.
    def _is_open(m):
        ct = m.get("close_time")
        if not ct:
            return True
        try:
            return datetime.fromisoformat(ct.replace("Z", "+00:00")) > now_utc
        except Exception:
            return True

    open_pool = [m for m in pool if _is_open(m)]
    if open_pool:
        pool = open_pool
    # If every market has closed (rare edge case), keep pool as-is so caller sees DONE.

    # Prefer the shortest-duration markets (KXBTC15M 15-min over KXBTCD 60-min).
    # If we have any markets within 5 minutes of the minimum duration, use only those.
    # This prevents 60-min KXBTCD markets from polluting the multi-window pool when
    # a 15-min KXBTC15M market is available.
    durations = [market_duration_minutes(m) for m in pool]
    valid_durations = [d for d in durations if d is not None]
    if valid_durations:
        min_dur = min(valid_durations)
        focused = [m for m, d in zip(pool, durations) if d is not None and d <= min_dur + 5]
        if focused and len(focused) < len(pool):
            log.info(f"Focusing pool from {len(pool)} to {len(focused)} markets "
                     f"(duration â‰¤ {min_dur + 5:.0f}m, dropping {len(pool) - len(focused)} longer-duration markets)")
            pool = focused

    _market_cache    = pool[0]
    _market_cache_ts = now
    _all_markets_cache    = pool
    _all_markets_cache_ts = now
    log.info(
        f"Active market: {_market_cache.get('ticker')} | {_market_cache.get('title')} "
        f"| closes {_market_cache.get('close_time')} | ({len(pool)} window(s) total)"
    )
    return pool if return_all else _market_cache


async def fetch_market_for_asset(session: aiohttp.ClientSession, asset: str) -> dict | None:
    """
    Fetch the soonest-expiring open market for the given non-BTC asset.
    Uses the kalshi_series priority list from ASSET_CONFIG.
    Accepts windows up to 20 minutes (15-min markets only).
    Returns None if no suitable market found.
    """
    series_list = ASSET_CONFIG.get(asset, {}).get("kalshi_series", ())
    path = "/markets"
    all_markets: list[dict] = []
    seen_tickers: set[str] = set()
    for series in series_list:
        params = {"series_ticker": series, "status": "open", "limit": 20}
        try:
            async with session.get(
                KALSHI_BASE_URL + path,
                headers=kalshi_headers("GET", path),
                params=params,
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json()
            for m in data.get("markets", []):
                t = m.get("ticker", "")
                if t and t not in seen_tickers:
                    seen_tickers.add(t)
                    all_markets.append(m)
        except Exception as exc:
            log.warning(f"fetch_market_for_asset [{asset}] series={series}: {exc}")

    if not all_markets:
        log.debug(f"fetch_market_for_asset [{asset}]: no markets found")
        return None

    # Drop daily/range markets; keep short-duration only
    all_markets = [m for m in all_markets
                   if "range" not in m.get("title", "").lower()
                   and "daily" not in m.get("title", "").lower()]
    if not all_markets:
        return None

    # Return soonest-expiring market with > 0 seconds left, < 20 min window
    now_utc = datetime.now(timezone.utc)
    def secs_to_close(m: dict) -> float:
        try:
            return (datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")) - now_utc).total_seconds()
        except Exception:
            return -1.0

    valid = [m for m in all_markets if 0 < secs_to_close(m) < 20 * 60]
    if not valid:
        return None

    valid.sort(key=secs_to_close)
    chosen = valid[0]
    log.debug(f"fetch_market_for_asset [{asset}]: {chosen.get('ticker')} ({secs_to_close(chosen):.0f}s left)")
    return chosen


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Strike parsing
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def parse_strike(market: dict) -> float | None:
    """
    Extract the strike price from market data.

    Priority:
      1. Structured fields: floor_strike, cap_strike, strike_price
      2. Regex search in title/subtitle for $XX,XXX or $XXX,XXX

    Returns:
        Strike as float, or None if unparseable (market will be skipped).
    """
    for field in ("floor_strike", "cap_strike", "strike_price"):
        val = market.get(field)
        if val is not None:
            try:
                strike = float(val)
                log.info(f"Strike parsed from field '{field}': {strike}")
                return strike
            except (ValueError, TypeError):
                pass

    text = (market.get("title", "") + " " + market.get("subtitle", "")
            + " " + (market.get("yes_sub_title") or ""))
    match = re.search(r"\$(\d{1,3}(?:,\d{3})*(?:\.\d+)?)", text)
    if match:
        strike = float(match.group(1).replace(",", ""))
        log.info(f"Strike parsed from title regex: {strike} (text: {text[:80]})")
        return strike

    yes_sub = market.get("yes_sub_title") or ""
    if "TBD" in yes_sub:
        log.debug(f"Cannot parse strike (TBD): {market.get('ticker')}")
    else:
        log.warning(f"Cannot parse strike. Full market fields: { {k: market.get(k) for k in ('ticker','title','subtitle','floor_strike','cap_strike','strike_price','result','yes_sub_title','no_sub_title')} }")
    return None


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Timing helpers
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def seconds_remaining(market: dict) -> float:
    """Seconds until the market closes. Returns 0 if already expired."""
    close_str = market.get("close_time", "")
    try:
        close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
        remaining = (close_dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, remaining)
    except Exception as exc:
        log.error(f"close_time parse error ({close_str!r}): {exc}")
        return 0.0


def seconds_elapsed(market: dict) -> float:
    """Estimate seconds since market open, using the market's actual duration."""
    try:
        open_str  = market.get("open_time") or market.get("open_date")
        close_str = market.get("close_time")
        if open_str and close_str:
            open_dt  = datetime.fromisoformat(open_str.replace("Z", "+00:00"))
            close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
            duration_secs = (close_dt - open_dt).total_seconds()
            return max(0.0, duration_secs - seconds_remaining(market))
    except Exception:
        pass
    # Fallback: assume 15-minute window
    return max(0.0, 15 * 60 - seconds_remaining(market))


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Orderbook fetching
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

async def fetch_orderbook(
    session: aiohttp.ClientSession,
    ticker: str,
    market: dict | None = None,
) -> dict | None:
    """
    Fetch live prices for one market ticker.

    For AMM markets (KXBTC15M) the /orderbook endpoint returns empty arrays.
    We call GET /markets/{ticker} directly for fresh AMM prices — never using
    the 30-second-cached market object, which can be stale enough to produce
    completely wrong prices after a BTC move.

    Returns:
        Dict with best_yes_ask, best_no_ask, best_yes_bid (all cents), yes_liquidity,
        or None if no price data is available. best_yes_bid may be None.
    """
    def _dollars_to_cents(val) -> int | None:
        try:
            v = int(round(float(val) * 100))
            return v if v >= 0 else None
        except (TypeError, ValueError):
            return None

    # â”€â”€ Step 1: try the orderbook endpoint (populated for limit-order markets)
    ob_path = f"/markets/{ticker}/orderbook"
    try:
        async with session.get(
            KALSHI_BASE_URL + ob_path,
            headers=kalshi_headers("GET", ob_path),
            timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.error(f"Orderbook fetch HTTP {resp.status} for {ticker}: {body[:200]}")
                ob_data = {}
            else:
                ob_data = await resp.json()
    except Exception as exc:
        log.error(f"Orderbook fetch error for {ticker}: {exc}")
        ob_data = {}

    ob = ob_data.get("orderbook", {})
    yes_arr = ob.get("yes", [])
    no_arr  = ob.get("no",  [])
    yes_asks = [(p, q) for p, q in yes_arr if q > 0 and p > 0]
    no_asks  = [(p, q) for p, q in no_arr  if q > 0 and p > 0]

    best_yes_ask = min(p for p, _ in yes_asks) if yes_asks else None
    best_no_ask  = min(p for p, _ in no_asks)  if no_asks  else None
    best_yes_bid = (100 - max(p for p, _ in no_asks)) if no_asks else None

    # â”€â”€ Step 2: AMM fallback — fetch the individual market fresh (not cached).
    #    The market cache TTL is 30s which is too stale for AMM price fields.
    #    A direct GET /markets/{ticker} gives real-time yes_ask/no_ask dollars.
    if best_yes_ask is None or best_no_ask is None:
        fresh_market: dict = {}
        mkt_path = f"/markets/{ticker}"
        try:
            async with session.get(
                KALSHI_BASE_URL + mkt_path,
                headers=kalshi_headers("GET", mkt_path),
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as resp:
                if resp.status == 200:
                    body = await resp.json()
                    fresh_market = body.get("market", {})
                else:
                    body_txt = await resp.text()
                    log.error(f"Market fetch HTTP {resp.status} for {ticker}: {body_txt[:200]}")
        except Exception as exc:
            log.error(f"Market fetch error for {ticker}: {exc}")
            # Last resort: use the cached market object if we have one
            fresh_market = market or {}

        src = fresh_market if fresh_market else (market or {})

        if best_yes_ask is None:
            best_yes_ask = _dollars_to_cents(src.get("yes_ask_dollars"))
        if best_yes_ask is None:
            no_bid = _dollars_to_cents(src.get("no_bid_dollars"))
            if no_bid is not None:
                _derived = 100 - no_bid
                if _derived > 0:
                    best_yes_ask = _derived

        if best_no_ask is None:
            best_no_ask = _dollars_to_cents(src.get("no_ask_dollars"))
        if best_no_ask is None:
            yes_bid = _dollars_to_cents(src.get("yes_bid_dollars"))
            if yes_bid is not None:
                _derived = 100 - yes_bid
                if _derived > 0:
                    best_no_ask = _derived

        if best_yes_bid is None:
            best_yes_bid = _dollars_to_cents(src.get("yes_bid_dollars"))
        if best_yes_bid is None:
            no_ask_raw = _dollars_to_cents(src.get("no_ask_dollars"))
            if no_ask_raw is not None:
                _derived = 100 - no_ask_raw
                if _derived >= 0:
                    best_yes_bid = _derived

        if best_yes_ask is not None or best_no_ask is not None:
            log.info(
                f"AMM prices for {ticker}: "
                f"yes_ask={best_yes_ask}¢  no_ask={best_no_ask}¢  yes_bid={best_yes_bid}¢"
            )

    if best_yes_ask is None or best_no_ask is None:
        # Diagnostic: dump the raw market fields so we can see what Kalshi actually returned.
        # On demo, non-BTC markets sometimes return no AMM fields at all — this log tells us
        # whether the field is missing vs. present but zero vs. present but filtered out.
        _diag_keys = ("yes_ask_dollars", "no_ask_dollars", "yes_bid_dollars", "no_bid_dollars",
                      "last_price", "status", "volume", "liquidity")
        _diag = {k: (src.get(k) if isinstance(src, dict) else None) for k in _diag_keys}
        log.warning(
            f"No price data available for {ticker} "
            f"(yes_ask={best_yes_ask} no_ask={best_no_ask}). "
            f"Raw market fields: {_diag}"
        )
        return None

    # Sanity check — reject prices outside [0, 100]. 0c is valid (sub-cent AMM settled market; EV returns -inf and skips). >100c is data corruption.
    # One side at 100c is valid at window open (e.g. yes_ask=12c, no_ask=100c).
    # Both at 100c means the market hasn't opened yet — reject.
    if not (0 <= best_yes_ask <= 100 and 0 <= best_no_ask <= 100):
        log.warning(
            f"Orderbook prices out of range for {ticker}: "
            f"yes_ask={best_yes_ask}c no_ask={best_no_ask}c — skipping"
        )
        return None
    if best_yes_ask == 100 and best_no_ask == 100:
        log.debug(f"Both sides at ceiling for {ticker} — market not ready yet")
        return None
    if best_yes_ask + best_no_ask < 100:
        log.warning(
            f"Orderbook sum below 100 for {ticker}: "
            f"yes_ask({best_yes_ask}c) + no_ask({best_no_ask}c) = {best_yes_ask+best_no_ask}c — skipping"
        )
        return None
    if best_yes_ask + best_no_ask > 150:
        log.warning(
            f"Orderbook sum very wide for {ticker}: "
            f"yes_ask({best_yes_ask}c) + no_ask({best_no_ask}c) = {best_yes_ask+best_no_ask}c — thin market, passing through"
        )

    # For AMM markets use the reported size; fall back to generous default
    if yes_asks:
        yes_liquidity = sum(q for p, q in yes_asks if p <= best_yes_ask)
    else:
        try:
            yes_liquidity = int(float(market.get("yes_ask_size_fp", 500))) if market else 500
        except (TypeError, ValueError):
            yes_liquidity = 500

    if no_asks:
        no_liquidity = sum(q for p, q in no_asks if p <= best_no_ask)
    else:
        try:
            no_liquidity = int(float(market.get("no_ask_size_fp", 500))) if market else 500
        except (TypeError, ValueError):
            no_liquidity = 500

    return {
        "best_yes_ask": best_yes_ask,
        "best_no_ask":  best_no_ask,
        "best_yes_bid": best_yes_bid,  # may be None
        "yes_liquidity": yes_liquidity,
        "no_liquidity":  no_liquidity,
    }


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  BTC position vs strike
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Contract price velocity tracking
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def track_contract_price(ticker: str, price: float) -> None:
    """Record the latest contract ask price for velocity and lag analysis."""
    if ticker not in _contract_price_history:
        _contract_price_history[ticker] = deque(maxlen=60)
    _contract_price_history[ticker].append((time.time(), price))



# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Adaptive calibration from trade history
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

async def calibrate_from_history() -> None:
    """
    Analyse completed paper + live trades to learn what conditions win.
    Updates _adaptive weights. Runs automatically every 20 completed trades.
    """
    global _adaptive
    try:
        async with aiosqlite.connect(_DB_FILE) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            async with db.execute("""
                SELECT entry_price_cents, seconds_left_at_entry, outcome
                FROM trades WHERE outcome IN ('win','loss')
                ORDER BY ts DESC LIMIT 100
            """) as cur:
                rows = await cur.fetchall()

        total = len(rows)
        if total < 10:
            return

        overall_wr = sum(1 for r in rows if r[2] == "win") / total

        # Did cheap contracts (< 60¢) win?
        cheap = [r for r in rows if r[0] is not None and r[0] < 60]
        if len(cheap) >= 5:
            _adaptive["low_price_wins"] = (
                sum(1 for r in cheap if r[2] == "win") / len(cheap) > 0.50
            )

        # Did near-expiry entries (< 4 min left) win?
        near = [r for r in rows if r[1] is not None and r[1] < 240]
        if len(near) >= 5:
            _adaptive["near_strike_wins"] = (
                sum(1 for r in near if r[2] == "win") / len(near) > 0.50
            )

        # Overall performance → shift threshold
        if overall_wr > 0.65:
            _adaptive["threshold_adjust"] = -8   # winning a lot → be more aggressive
        elif overall_wr < 0.40:
            _adaptive["threshold_adjust"] = +8   # losing a lot → tighten up
        else:
            _adaptive["threshold_adjust"] = 0

        _adaptive["last_calibrated_count"] = total
        log.info(
            f"Adaptive calibration ({total} trades): "
            f"WR={overall_wr:.0%} | low_price_wins={_adaptive['low_price_wins']} | "
            f"near_strike_wins={_adaptive['near_strike_wins']} | "
            f"threshold_adjust={_adaptive['threshold_adjust']:+d}"
        )
    except Exception as exc:
        log.error(f"Calibration error: {exc}")


async def calibrate_brain() -> None:
    """
    Self-calibration for the printer brain. Runs every 5 completed trades.

    Learns:
      1. prob_scale  — were our probability estimates too high or too low?
                       If we said 70% but only won 40% → scale down future estimates.
      2. min_edge    — auto-tune the edge threshold based on overall win rate.
      3. condition_wr — win rate per (distance, time, momentum) bucket so the
                        brain knows which setups actually work.
      4. directional  — are bullish/bearish/neutral momentum trades working?
    """
    global _brain_cal
    try:
        async with aiosqlite.connect(_DB_FILE) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            async with db.execute("""
                SELECT side, entry_price_cents, seconds_left_at_entry,
                       btc_price_at_entry, strike, outcome, model_prob
                FROM trades
                WHERE outcome IN ('win','loss')
                ORDER BY ts DESC LIMIT 200
            """) as cur:
                rows = await cur.fetchall()

        total = len(rows)
        if total < 20:
            return

        wins = sum(1 for r in rows if r[5] == "win")
        overall_wr = wins / total

        # â”€â”€ 1. Probability scale factor â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # Compare avg model_prob (our estimate) to actual win rate.
        # Only update gently — 5-20 trades is far too small a sample to
        # conclude the table is wrong; 0.05 weight prevents overreaction.
        probs = [r[6] for r in rows if r[6] is not None]
        if probs:
            avg_predicted = sum(probs) / len(probs)
            if avg_predicted > 0:
                raw_scale = overall_wr / avg_predicted
                _brain_cal["prob_scale"] = 0.95 * _brain_cal["prob_scale"] + 0.05 * raw_scale
                # Floor at 0.85 — the table is built on 4.5M rows; trust it
                _brain_cal["prob_scale"] = max(0.85, min(1.5, _brain_cal["prob_scale"]))

        # â”€â”€ 2. Win-rate reward tiers (priority = win rate, not profit) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        #   Tier 3 (â‰¥85%) — MAX reward: lowest edge bar, biggest confidence bonus
        #   Tier 2 (â‰¥75%) — HUGE reward: very low bar, large bonus
        #   Tier 1 (â‰¥50%) — reward: lower bar, small bonus
        #   Below 50%     — tighten up, no bonus
        _brain_cal["overall_wr"] = overall_wr
        if overall_wr >= 0.85:
            _brain_cal["reward_tier"]       = 3
            _brain_cal["min_edge_override"] = 0.20   # backtested optimum — never go lower
            _brain_cal["confidence_bonus"]  = 0
            tier_label = "TIER 3 MAX REWARD"
        elif overall_wr >= 0.75:
            _brain_cal["reward_tier"]       = 2
            _brain_cal["min_edge_override"] = 0.20
            _brain_cal["confidence_bonus"]  = 0
            tier_label = "TIER 2 HUGE REWARD"
        elif overall_wr >= 0.50:
            _brain_cal["reward_tier"]       = 1
            _brain_cal["min_edge_override"] = 0.20   # backtested baseline
            _brain_cal["confidence_bonus"]  = 0
            tier_label = "TIER 1 REWARD"
        elif overall_wr >= 0.40:
            _brain_cal["reward_tier"]       = 0
            _brain_cal["min_edge_override"] = 0.25   # tighten above baseline
            _brain_cal["confidence_bonus"]  = 0
            tier_label = "no reward (learning)"
        else:
            _brain_cal["reward_tier"]       = 0
            _brain_cal["min_edge_override"] = 0.30   # losing — tighten hard
            _brain_cal["confidence_bonus"]  = 0
            tier_label = "no reward (rebuild)"

        # â”€â”€ 3. Directional performance â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        yes_rows = [r for r in rows if r[0] == "yes"]
        no_rows  = [r for r in rows if r[0] == "no"]
        if len(yes_rows) >= 10:
            _brain_cal["bullish_wr"] = sum(1 for r in yes_rows if r[5] == "win") / len(yes_rows)
        if len(no_rows) >= 10:
            _brain_cal["bearish_wr"] = sum(1 for r in no_rows  if r[5] == "win") / len(no_rows)

        _brain_cal["last_count"] = total
        brain_log.info(
            f"[CALIBRATION] {total} trades | WR={overall_wr:.0%} -> {tier_label} | "
            f"min_ev={_brain_cal['min_edge_override']:.0%} | "
            f"conf_bonus=+{_brain_cal['confidence_bonus']} | "
            f"prob_scale={_brain_cal['prob_scale']:.2f} | "
            f"yes_wr={_brain_cal['bullish_wr']:.0%} no_wr={_brain_cal['bearish_wr']:.0%}"
        )
        log.info(
            f"[Brain] calibrated — WR={overall_wr:.0%} {tier_label} | "
            f"scale={_brain_cal['prob_scale']:.2f}"
        )

    except Exception as exc:
        log.error(f"Brain calibration error: {exc}")


async def recalibrate_asset_strategies() -> None:
    """
    Fit per-asset AssetCalibrator from actual trade outcomes in the DB.

    Queries raw_p_yes (pre-calibration P(YES wins)) and the actual YES outcome
    per asset, then refits isotonic/Platt calibration. Requires >= 15 trades
    per asset to fit (AssetCalibrator's own minimum).
    """
    if not _S2_SINGLETONS:
        log.debug("recalibrate_asset_strategies: no active strategy singletons — skipping")
        return
    try:
        async with aiosqlite.connect(_DB_FILE) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            for asset, strat in list(_S2_SINGLETONS.items()):
                async with db.execute("""
                    SELECT raw_p_yes, side, outcome
                    FROM trades
                    WHERE asset = ?
                      AND outcome IN ('win', 'loss')
                      AND raw_p_yes IS NOT NULL
                    ORDER BY ts DESC LIMIT 500
                """, (asset,)) as cur:
                    rows = await cur.fetchall()

                if len(rows) < 15:
                    continue

                # Calibrator maps raw_p_yes → better P(YES wins).
                # actual_yes_won = 1 if YES resolved True regardless of which
                # side we took: YES trade won, or NO trade lost.
                raw_probs = [r[0] for r in rows]
                outcomes = [
                    1 if (r[1] == "yes" and r[2] == "win") or
                         (r[1] == "no"  and r[2] == "loss")
                    else 0
                    for r in rows
                ]
                strat.calibrator.refit(raw_probs, outcomes)
                log.info(
                    f"[Calibration] {asset}: {len(rows)} trades → "
                    f"method={strat.calibrator._method} "
                    f"n={strat.calibrator.sample_count}"
                )
    except Exception as exc:
        log.error(f"recalibrate_asset_strategies error: {exc}")


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Printer Brain v3 — Empirically Calibrated from 4.5M rows of BTC 1-min data
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def _session_ev_adjustment() -> float:
    return 0.0


_S2_SINGLETONS: dict = {}  # keyed by asset name — current D3 hybrid
_S1_SINGLETONS: dict = {}  # keyed by asset name — original per-asset strategies
_s1_pending_trades: dict = {}  # ticker → {trade_id, side, entry_price_cents, contracts, strike, asset, mode, entry_ts}
_config_mtime: float = 0.0
_current_window: str = ""  # tracks active time window; cleared on boundary crossing
_s1_config_mtime: float = 0.0
_s1_window: str = ""


def _strategy_name_for(asset, duration_min=15.0):
    """Human-readable strategy name for the dashboard per-asset card."""
    return {"BTC": "B3", "ETH": "E1", "SOL": "S1", "XRP": "X3", "DOGE": "D3"}.get(asset, "15m")


def _get_or_make_strategy_s2(asset: str, config, market_duration_min: float = 15.0):
    """Lazily construct per-asset strategy singleton. Returns None on failure."""
    global _config_mtime, _current_window
    # Fix sys.path FIRST so every strategies.* import resolves to src/strategies/.
    # The root strategies/ directory (YAML configs) would otherwise be picked up as a
    # namespace package and poison sys.modules['strategies'] before src/ is on the path.
    import sys as _sys
    _src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
    if _src not in _sys.path:
        # Purge any stale root-strategies namespace package cached before this fix runs
        for _k in [k for k in _sys.modules if k == "strategies" or k.startswith("strategies.")]:
            del _sys.modules[_k]
        _sys.path.insert(0, _src)
    try:
        mtime = os.path.getmtime(_CONFIG_FILE)
        if mtime != _config_mtime:
            _S2_SINGLETONS.clear()
            _config_mtime = mtime
            log.info("config.json changed — strategy singletons cleared")
    except OSError:
        pass
    try:
        from strategies.signals.time_windows import get_trading_window, get_window_params
        import time as _tw_now
        _new_window = get_trading_window(_tw_now.time(), config.get("timezone", "America/Los_Angeles"))
        if _new_window != _current_window:
            _S2_SINGLETONS.clear()
            _current_window = _new_window
            log.info("Trading window changed to %s — strategy singletons cleared", _current_window)
    except Exception:
        _new_window = _current_window or "normal"

    cache_key = asset
    if cache_key in _S2_SINGLETONS:
        return _S2_SINGLETONS[cache_key]
    try:
        from strategies.skip_layer import SkipConfig
        from strategies.signals.time_windows import get_window_params

        _min_price = float(config.get("min_entry_price_cents", 20.0))
        _max_price = float(config.get("max_entry_price_cents", 76.0))
        _tw = _new_window
        _wp = get_window_params(config, _tw)
        _max_price = min(_max_price, float(_wp["max_entry_price_cents"]))
        if _max_price <= _min_price:
            log.warning(
                "[%s] time_window=%s has max_entry=%.0fc <= min_entry=%.0fc — all entries will be blocked",
                asset, _tw, _max_price, _min_price,
            )
        skip_cfg = SkipConfig(
            max_spread_cents=float(get_asset_config(config, asset, "max_spread_cents", 3.0)),
            min_seconds_left=float(config.get("min_seconds_left", 30.0)),
            min_entry_price_cents=_min_price,
            max_entry_price_cents=_max_price,
            cold_start_samples=int(config.get("cold_start_samples", 60)),
            vol_ratio_threshold=float(get_asset_config(config, asset, "vol_gate_thresh", 1.80)),
        )
        overrides = config.get("asset_overrides", {}).get(asset, {})
        _ev_default = config.get("min_ev_base_15m", config.get("min_ev_base", 8))
        _ev_base = float(overrides.get("min_ev_base", _ev_default)) + float(_wp["min_ev_delta"])
        min_ev = _ev_base / 100.0
        stake = float(config.get("trade_amount_dollars", 25))

        from strategies.fifteen_min_strategy import FifteenMinStrategy
        strat = FifteenMinStrategy(
            asset=asset,
            skip_config=skip_cfg,
            min_ev=min_ev,
            stake_dollars=stake,
        )

        _S2_SINGLETONS[cache_key] = strat
        log.info(f"Strategy initialized: {cache_key} (15m, stake=${stake})")
        return strat
    except Exception as exc:
        log.warning(f"{asset} strategy init failed, falling back to legacy: {exc}")
        return None


def strategy_brain_s2(
    btc_price, strike, yes_ask, no_ask,
    elapsed_seconds, secs_left, ticker,
    min_ev_base=3.0, vol_gate_thresh=1.80, kalshi_fee=0.07,
    asset="BTC", max_entry_price_cents=100.0,
    min_reward_cents=0.0, max_risk_reward_ratio=999.0,
):
    """Dispatch to FifteenMinStrategy (D3 hybrid). Returns brain dict tagged strategy2."""
    config = read_config()

    market_duration_min = (elapsed_seconds + secs_left) / 60.0
    strat = _get_or_make_strategy_s2(asset, config, market_duration_min=market_duration_min)
    if strat is None:
        # No validated strategy for this asset/duration. Skipping is better than
        # using the legacy printer_brain which has no calibrated edge on these markets
        # and produces random-confidence outputs (observed 50/50 win rate in paper trading).
        log.info(
            f"No strategy for {asset} at {market_duration_min:.0f}min "
            f"skipping (no strategy for duration)"
        )
        _above = btc_price > strike if strike > 0 else False
        return {
            "action": "skip",
            "side": "yes" if _above else "no",
            "confidence": 50,
            "reasoning": f"no_strategy:{asset}_{market_duration_min:.0f}min",
            "key_signals": [],
            "signals": {},
            "win_prob": 0.5,
            "mom_label": "no_strategy",
            "mom_pct": 0.0,
            "vel_signal": "neutral",
            "raw_p_yes": None,
            "mins_left": secs_left / 60.0,
            "abs_pct": abs(btc_price - strike) / strike if strike > 0 else 0.0,
            "above": _above,
            "_rv": None,
            "_vol_ratio": None,
            "price_filter_skip": False,
        }

    from strategies.feature_builder import build_features_from_bot_state
    try:
        if asset == "BTC":
            prices_deque = btc_prices
            current_price = btc_price
        else:
            prices_deque = asset_manager._prices.get(asset)
            if not prices_deque:
                return {
                    "action": "skip", "side": "no", "confidence": 50,
                    "reasoning": f"no_price_feed:{asset}",
                    "key_signals": [], "signals": {}, "win_prob": 0.5,
                    "mom_label": "no_data", "mom_pct": 0.0, "vel_signal": "neutral",
                    "raw_p_yes": None, "mins_left": secs_left / 60.0,
                    "abs_pct": 0.0, "above": False, "_rv": None, "_vol_ratio": None,
                    "price_filter_skip": False,
                }
            current_price = prices_deque[-1][1]

        features = build_features_from_bot_state(
            asset=asset,
            ticker=ticker,
            current_price=current_price,
            strike=strike,
            btc_price=btc_price,
            seconds_left=secs_left,
            elapsed_seconds=elapsed_seconds,
            yes_ask=yes_ask,
            no_ask=no_ask,
            yes_bid=max(0.0, yes_ask - 1.0),
            no_bid=max(0.0, no_ask - 1.0),
            prices_deque=prices_deque,
            contract_history=_contract_price_history.get(ticker),
            btc_prices_deque=btc_prices,
        )
    except Exception as exc:
        log.warning(f"{asset} feature_builder failed — skipping (not falling back to legacy): {exc}")
        _above = current_price > strike if strike > 0 else False
        return {
            "action": "skip", "side": "yes" if _above else "no", "confidence": 50,
            "reasoning": f"feature_builder_error:{exc.__class__.__name__}",
            "key_signals": [], "signals": {}, "win_prob": 0.5,
            "mom_label": "error", "mom_pct": 0.0, "vel_signal": "neutral",
            "raw_p_yes": None, "mins_left": secs_left / 60.0,
            "abs_pct": abs(current_price - strike) / strike if strike > 0 else 0.0,
            "above": _above, "_rv": None, "_vol_ratio": None, "price_filter_skip": False,
        }

    try:
        decision = strat.decide(features)
    except Exception as exc:
        log.warning(f"{asset} strat.decide() failed — skipping (not falling back to legacy): {exc}")
        _above = current_price > strike if strike > 0 else False
        return {
            "action": "skip", "side": "yes" if _above else "no", "confidence": 50,
            "reasoning": f"decide_error:{exc.__class__.__name__}",
            "key_signals": [], "signals": {}, "win_prob": 0.5,
            "mom_label": "error", "mom_pct": 0.0, "vel_signal": "neutral",
            "raw_p_yes": None, "mins_left": secs_left / 60.0,
            "abs_pct": abs(current_price - strike) / strike if strike > 0 else 0.0,
            "above": _above, "_rv": None, "_vol_ratio": None, "price_filter_skip": False,
        }

    above = current_price > strike
    naive = "yes" if above else "no"
    if decision.side is not None and decision.side != naive:
        brain_log.info(
            f"ROUTER_FLIPPED {asset} {ticker} | px={current_price:.4f} "
            f"strike={strike:.4f} naive={naive} picked={decision.side} | "
            f"yes_ev={decision.contributing_signals.get('yes_ev', float('nan')):+.3f} "
            f"no_ev={decision.contributing_signals.get('no_ev', float('nan')):+.3f} | "
            f"mode={decision.contributing_signals.get('decision_mode', '?')}"
        )

    abs_pct = abs(current_price - strike) / strike
    # new base.py sets p_model = P(chosen_side_wins) already; no inversion needed
    true_p = decision.p_model
    if decision.action == "trade":
        _st = decision.contributing_signals.get("supertrend_direction")
        _mkt = decision.contributing_signals.get("market_prob")
        log.info("[%s] signal=supertrend st=%s market=%.3f side=%s",
                 asset, _st, _mkt or 0, decision.side)
    return {
        "action": decision.action,
        "side": decision.side if decision.side else naive,
        "confidence": int(round(true_p * 100)),
        "reasoning": decision.reason,
        "key_signals": [f"{k}: {v}" for k, v in decision.contributing_signals.items()],
        "signals": dict(decision.contributing_signals),
        "win_prob": float(true_p),  # P(chosen side wins), used by confidence gate
        "mom_label": decision.contributing_signals.get(
            "regime", decision.contributing_signals.get("mom_label", "neutral")
        ),
        "mom_pct": float(decision.contributing_signals.get(
            "regime_adj", decision.contributing_signals.get("mom_adj", 0.0)
        )),
        "vel_signal": decision.contributing_signals.get(
            "velocity", decision.contributing_signals.get("vel_signal", "neutral")
        ),
        "raw_p_yes": decision.contributing_signals.get("raw_p_yes"),
        "mins_left": secs_left / 60.0,
        "abs_pct": abs_pct,
        "above": above,
        "_rv": features.realized_vol_1min,
        "_vol_ratio": None,
        "price_filter_skip": False,
        "strategy_variant": "strategy2",
    }


def _get_or_make_strategy_s1(asset: str, config):
    """Lazily construct original per-asset strategy singleton. Returns None on failure."""
    global _s1_config_mtime, _s1_window
    import sys as _sys
    _src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
    if _src not in _sys.path:
        for _k in [k for k in _sys.modules if k == "strategies" or k.startswith("strategies.")]:
            del _sys.modules[_k]
        _sys.path.insert(0, _src)

    try:
        mtime = os.path.getmtime(_CONFIG_FILE)
        if mtime != _s1_config_mtime:
            _S1_SINGLETONS.clear()
            _s1_config_mtime = mtime
            log.info("config.json changed — S1 strategy singletons cleared")
    except OSError:
        pass
    try:
        from strategies.signals.time_windows import get_trading_window
        import time as _tw_now
        _cur_win = get_trading_window(_tw_now.time(), config.get("timezone", "America/Los_Angeles"))
        if _cur_win != _s1_window:
            _S1_SINGLETONS.clear()
            _s1_window = _cur_win
            log.info("Trading window changed to %s — S1 strategy singletons cleared", _cur_win)
    except Exception:
        pass

    cache_key = asset
    if cache_key in _S1_SINGLETONS:
        return _S1_SINGLETONS[cache_key]

    _ASSET_CLASS_MAP = {
        "BTC":  ("strategies.original.btc_strategy",  "BTCStrategy"),
        "ETH":  ("strategies.original.eth_strategy",  "ETHStrategy"),
        "SOL":  ("strategies.original.sol_strategy",  "SOLStrategy"),
        "XRP":  ("strategies.original.xrp_strategy",  "XRPStrategy"),
        "DOGE": ("strategies.original.doge_strategy", "DOGEStrategy"),
    }
    if asset not in _ASSET_CLASS_MAP:
        return None

    try:
        from strategies.skip_layer import SkipConfig
        from strategies.signals.time_windows import get_window_params

        _min_price = float(config.get("min_entry_price_cents", 20.0))
        _max_price = float(config.get("max_entry_price_cents", 76.0))
        _tw = _s1_window or "normal"
        _wp = get_window_params(config, _tw)
        _max_price = min(_max_price, float(_wp["max_entry_price_cents"]))
        if _max_price <= _min_price:
            log.warning(
                "[S1:%s] time_window=%s has max_entry=%.0fc <= min_entry=%.0fc — all entries will be blocked",
                asset, _tw, _max_price, _min_price,
            )
        skip_cfg = SkipConfig(
            max_spread_cents=float(get_asset_config(config, asset, "max_spread_cents", 3.0)),
            min_seconds_left=float(config.get("min_seconds_left", 30.0)),
            min_entry_price_cents=_min_price,
            max_entry_price_cents=_max_price,
            cold_start_samples=int(config.get("cold_start_samples", 60)),
            vol_ratio_threshold=float(get_asset_config(config, asset, "vol_gate_thresh", 1.80)),
        )
        overrides = config.get("asset_overrides", {}).get(asset, {})
        _ev_default = config.get("min_ev_base_15m", config.get("min_ev_base", 8))
        min_ev = (float(overrides.get("min_ev_base", _ev_default)) + float(_wp["min_ev_delta"])) / 100.0
        stake = float(config.get("trade_amount_dollars", 25))

        module_path, class_name = _ASSET_CLASS_MAP[asset]
        import importlib
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        strat = cls(skip_config=skip_cfg, min_ev=min_ev, stake_dollars=stake)
        _S1_SINGLETONS[cache_key] = strat
        log.info("S1 Strategy initialized: %s (window=%s, max_entry=%.0fc, min_ev=%.2f%%)",
                 cache_key, _tw, _max_price, min_ev * 100)
        return strat
    except Exception as exc:
        log.warning(f"S1 {asset} strategy init failed: {exc}")
        return None


def strategy_brain_s1(
    btc_price, strike, yes_ask, no_ask,
    elapsed_seconds, secs_left, ticker,
    asset="BTC",
):
    """Dispatch to original per-asset strategy. Returns brain dict tagged strategy1."""
    config = read_config()
    strat = _get_or_make_strategy_s1(asset, config)
    if strat is None:
        _above = btc_price > strike if strike > 0 else False
        return {
            "action": "skip", "side": "yes" if _above else "no", "confidence": 50,
            "reasoning": f"s1_no_strategy:{asset}",
            "key_signals": [], "signals": {}, "win_prob": 0.5,
            "mom_label": "no_strategy", "mom_pct": 0.0, "vel_signal": "neutral",
            "raw_p_yes": None, "mins_left": secs_left / 60.0,
            "abs_pct": abs(btc_price - strike) / strike if strike > 0 else 0.0,
            "above": _above, "_rv": None, "_vol_ratio": None, "price_filter_skip": False,
            "strategy_variant": "strategy1",
        }

    from strategies.feature_builder import build_features_from_bot_state
    try:
        if asset == "BTC":
            prices_deque = btc_prices
            current_price = btc_price
        else:
            prices_deque = asset_manager._prices.get(asset)
            if not prices_deque:
                _above = btc_price > strike if strike > 0 else False
                return {
                    "action": "skip", "side": "no", "confidence": 50,
                    "reasoning": f"s1_no_price_feed:{asset}",
                    "key_signals": [], "signals": {}, "win_prob": 0.5,
                    "mom_label": "no_data", "mom_pct": 0.0, "vel_signal": "neutral",
                    "raw_p_yes": None, "mins_left": secs_left / 60.0,
                    "abs_pct": 0.0, "above": False, "_rv": None, "_vol_ratio": None,
                    "price_filter_skip": False, "strategy_variant": "strategy1",
                }
            current_price = prices_deque[-1][1]

        features = build_features_from_bot_state(
            asset=asset,
            ticker=ticker,
            current_price=current_price,
            strike=strike,
            btc_price=btc_price,
            seconds_left=secs_left,
            elapsed_seconds=elapsed_seconds,
            yes_ask=yes_ask,
            no_ask=no_ask,
            yes_bid=max(0.0, yes_ask - 1.0),
            no_bid=max(0.0, no_ask - 1.0),
            prices_deque=prices_deque,
            contract_history=_contract_price_history.get(ticker),
            btc_prices_deque=btc_prices,
        )
    except Exception as exc:
        log.warning(f"S1 {asset} feature_builder failed: {exc}")
        _above = btc_price > strike if strike > 0 else False
        return {
            "action": "skip", "side": "yes" if _above else "no", "confidence": 50,
            "reasoning": f"s1_feature_error:{exc.__class__.__name__}",
            "key_signals": [], "signals": {}, "win_prob": 0.5,
            "mom_label": "error", "mom_pct": 0.0, "vel_signal": "neutral",
            "raw_p_yes": None, "mins_left": secs_left / 60.0,
            "abs_pct": abs(btc_price - strike) / strike if strike > 0 else 0.0,
            "above": _above, "_rv": None, "_vol_ratio": None, "price_filter_skip": False,
            "strategy_variant": "strategy1",
        }

    try:
        decision = strat.decide(features)
    except Exception as exc:
        log.warning(f"S1 {asset} strat.decide() failed: {exc}")
        _above = current_price > strike if strike > 0 else False
        return {
            "action": "skip", "side": "yes" if _above else "no", "confidence": 50,
            "reasoning": f"s1_decide_error:{exc.__class__.__name__}",
            "key_signals": [], "signals": {}, "win_prob": 0.5,
            "mom_label": "error", "mom_pct": 0.0, "vel_signal": "neutral",
            "raw_p_yes": None, "mins_left": secs_left / 60.0,
            "abs_pct": abs(current_price - strike) / strike if strike > 0 else 0.0,
            "above": _above, "_rv": None, "_vol_ratio": None,
            "price_filter_skip": False, "strategy_variant": "strategy1",
        }

    above = current_price > strike
    naive = "yes" if above else "no"
    abs_pct = abs(current_price - strike) / strike if strike > 0 else 0.0
    true_p = decision.p_model if decision.side == "yes" else (1.0 - decision.p_model)
    return {
        "action": decision.action,
        "side": decision.side if decision.side else naive,
        "confidence": int(round(true_p * 100)),
        "reasoning": decision.reason,
        "key_signals": [f"{k}: {v}" for k, v in decision.contributing_signals.items()],
        "signals": dict(decision.contributing_signals),
        "win_prob": float(true_p),
        "mom_label": decision.contributing_signals.get("regime", "neutral"),
        "mom_pct": float(decision.contributing_signals.get("regime_adj", 0.0)),
        "vel_signal": decision.contributing_signals.get("velocity", "neutral"),
        "raw_p_yes": decision.contributing_signals.get("raw_p_yes"),
        "mins_left": secs_left / 60.0,
        "abs_pct": abs_pct,
        "above": above,
        "_rv": features.realized_vol_1min,
        "_vol_ratio": None,
        "price_filter_skip": False,
        "strategy_variant": "strategy1",
    }


# â”€â”€ End feature-flagged routing â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Reversal signal
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def calculate_contracts(
    trade_amount_dollars: float,
    entry_price_cents: int,
    liquidity: int,
) -> tuple[int, float]:
    """
    Fixed position sizing — always spend exactly trade_amount_dollars.

    Returns:
        (contracts, dollars_used)
    """
    if entry_price_cents <= 0:
        return 0, 0.0

    price_dollars = entry_price_cents / 100.0

    def _taker_fee(n: int) -> float:
        raw = 0.07 * n * price_dollars * (1.0 - price_dollars)
        return math.ceil(raw * 100) / 100.0

    contracts = int(trade_amount_dollars * 100 / entry_price_cents)
    contracts = min(contracts, liquidity)
    contracts = max(contracts, 0)

    # Reduce until stake + fee fits within budget (fee is paid at purchase time)
    while contracts > 0 and contracts * price_dollars + _taker_fee(contracts) > trade_amount_dollars:
        contracts -= 1

    dollars_used = contracts * price_dollars

    log.info(
        f"Fixed sizing: price={entry_price_cents}c "
        f"bet=${dollars_used:.2f} fee=${_taker_fee(contracts):.2f} -> {contracts} contracts"
    )
    return contracts, dollars_used


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Probability helpers
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def implied_prob(contract_price_cents: float) -> float:
    """Convert contract price in cents to implied probability (0—1)."""
    return contract_price_cents / 100.0



# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Order placement


async def _portfolio_has_position(
    session: aiohttp.ClientSession,
    ticker: str,
    side: str,
) -> bool:
    """Check whether the portfolio already holds a position for (ticker, side)."""
    if not ticker or not side:
        return False
    try:
        pos_path = f"/portfolio/positions?ticker={ticker}"
        async with session.get(
            KALSHI_BASE_URL + pos_path,
            headers=kalshi_headers("GET", pos_path),
            timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                return False
            pos_data = await resp.json()
        positions = pos_data.get("market_positions") or pos_data.get("positions") or []
        for p in positions:
            if p.get("ticker") == ticker:
                held = p.get("position", 0)
                if side == "yes" and held > 0:
                    return True
                if side == "no" and held < 0:
                    return True
    except Exception as exc:
        log.warning(f"_portfolio_has_position error for {ticker}: {exc}")
    return False

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

async def _verify_order_fill(
    session: aiohttp.ClientSession,
    order_id: str,
    expected_filled: int,
    ticker: str = "",
    side: str = "",
) -> bool:
    """
    Confirm a fill is recorded in Kalshi by re-fetching the order.

    Returns True if Kalshi confirms at least one contract filled.
    On HTTP errors, falls back to a portfolio position check (requires ticker+side).
    On network exceptions, returns False — the conservative choice is to not book a
    phantom position rather than assume a fill that may not exist.
    """
    try:
        chk_path = f"/portfolio/orders/{order_id}"
        async with session.get(
            KALSHI_BASE_URL + chk_path,
            headers=kalshi_headers("GET", chk_path),
            timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                log.warning(f"_verify_order_fill: GET {order_id} HTTP {resp.status} — checking portfolio")
                return await _portfolio_has_position(session, ticker, side)
            chk = await resp.json()
        order = chk.get("order") or chk
        status = order.get("status", "")
        total     = order.get("contracts_count") or expected_filled
        remaining = order.get("remaining_count")
        fc        = order.get("filled_count")
        if fc is not None:
            confirmed_filled = fc
        elif remaining is not None:
            confirmed_filled = total - remaining
        else:
            # Can't determine — trust the POST response
            confirmed_filled = expected_filled
        log.info(
            f"_verify_order_fill: {order_id} status={status!r} "
            f"filled={confirmed_filled}/{total}"
        )
        return confirmed_filled > 0
    except Exception as exc:
        log.warning(f"_verify_order_fill error for {order_id}: {exc} — returning False (conservative)")
        return False


async def place_order(
    session: aiohttp.ClientSession,
    ticker: str,
    side: str,
    contracts: int,
    entry_price_cents: int,
    mode: str,
    market: dict | None = None,
    asset: str = "BTC",
    secs_left: float = 900.0,
) -> dict:
    """
    Place a market order on Kalshi.

    Fills at best available price immediately. No price-bumping or GTC/IOC.

    In paper mode, simulates an instant fill without hitting the API.

    Returns:
        Dict with keys: fill_confirmed (bool), fill_price_cents (int|None),
        order_id (str|None).
    """
    if contracts <= 0:
        log.error(f"place_order called with contracts={contracts} — refusing to send invalid order")
        return {"fill_confirmed": False, "fill_price_cents": None, "order_id": None}

    # Preserve the strategy-chosen entry price for fill-verification telemetry.
    _original_strategy_target_c = entry_price_cents

    # Re-fetch fresh orderbook — used for paper slippage simulation AND non-paper
    # telemetry. Must run before the paper branch so paper fills reflect the live ask.
    fresh_ob = None
    try:
        fresh_ob = await fetch_orderbook(session, ticker, market)
        if fresh_ob is not None:
            fp = fresh_ob["best_yes_ask"] if side == "yes" else fresh_ob["best_no_ask"]
            if fp is not None and fp != entry_price_cents:
                log.info(f"Price updated {entry_price_cents}c -> {fp}c for {side.upper()} on {ticker}")
                entry_price_cents = fp
    except Exception as _fe:
        log.warning(f"Fresh price fetch failed: {_fe}")

    _market_ask_at_post_c = None
    try:
        _ob = fresh_ob if isinstance(fresh_ob, dict) else {}
        _market_ask_at_post_c = _ob.get("best_yes_ask") if side == "yes" else _ob.get("best_no_ask")
    except Exception:
        _market_ask_at_post_c = None

    if mode == "paper":
        paper_fill = _market_ask_at_post_c if _market_ask_at_post_c is not None else entry_price_cents
        log.info(f"[PAPER] Simulated BUY {side} {contracts}x @ {paper_fill}c on {ticker}")
        return {
            "fill_confirmed": True,
            "fill_price_cents": paper_fill,
            "order_id": f"paper_{int(time.time() * 1000)}",
        }

    path = "/portfolio/orders"
    price_this_attempt = entry_price_cents

    # â”€â”€ Demo mode: post + poll â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Demo API behaviour is unpredictable with order types; poll for a fill
    # rather than relying on an immediate response.
    if mode == "demo":
        client_order_id = f"demo_{int(time.time() * 1000)}"
        body = {
            "ticker": ticker,
            "side": side,
            "type": "market",
            "count": contracts,
            "action": "buy",
            "client_order_id": client_order_id,
        }
        log.info(f"[demo] market {side.upper()} {contracts}x on {ticker}")
        try:
            async with session.post(
                KALSHI_BASE_URL + path,
                headers=kalshi_headers("POST", path),
                json=body,
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as resp:
                data = await resp.json()
                http_status = resp.status
        except Exception as exc:
            log.error(f"[demo] Order POST failed: {exc}")
            return {"fill_confirmed": False, "fill_price_cents": None, "order_id": None}

        if http_status not in (200, 201):
            log.error(f"[demo] Order HTTP {http_status}: {data}")
            return {"fill_confirmed": False, "fill_price_cents": None, "order_id": None}

        order_id = (data.get("order") or {}).get("order_id") or data.get("order_id")
        if not order_id:
            log.error(f"[demo] No order_id in response: {data}")
            return {"fill_confirmed": False, "fill_price_cents": None, "order_id": None}

        _poll_interval = 3.0
        _poll_timeout  = 30.0
        _elapsed       = 0.0
        filled_order   = None
        while _elapsed < _poll_timeout:
            await asyncio.sleep(_poll_interval)
            _elapsed += _poll_interval
            try:
                chk_path = f"/portfolio/orders/{order_id}"
                async with session.get(
                    KALSHI_BASE_URL + chk_path,
                    headers=kalshi_headers("GET", chk_path),
                    timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
                ) as chk_resp:
                    chk_data = await chk_resp.json()
                chk_order = chk_data.get("order") or chk_data
                status = chk_order.get("status", "")
                if status not in ("resting", "pending"):
                    filled_order = chk_order
                    break
            except Exception as pe:
                log.warning(f"[demo] Poll error: {pe}")

        if filled_order is None:
            log.info(f"[demo] No fill after {_poll_timeout:.0f}s — cancelling {order_id}")
            try:
                del_path = f"/portfolio/orders/{order_id}"
                async with session.delete(
                    KALSHI_BASE_URL + del_path,
                    headers=kalshi_headers("DELETE", del_path),
                    timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
                ) as del_resp:
                    await del_resp.json()
            except Exception as ce:
                log.warning(f"[demo] Cancel failed: {ce}")
            return {"fill_confirmed": False, "fill_price_cents": None, "order_id": order_id}

        status = filled_order.get("status", "")
        if status in ("cancelled", "canceled", "expired"):
            return {"fill_confirmed": False, "fill_price_cents": None, "order_id": order_id}

        _total     = filled_order.get("contracts_count")
        _remaining = filled_order.get("remaining_count")
        _fc        = filled_order.get("filled_count")
        cc = _fc if _fc is not None else ((_total - _remaining) if (_total is not None and _remaining is not None) else contracts)
        fp_raw = filled_order.get("yes_price", entry_price_cents)
        fp     = fp_raw if side == "yes" else (100 - fp_raw)  # yes_price is integer cents 0-100
        log.info(f"[demo] Order {order_id} filled={cc}x @ {fp}c status={status!r}")
        _fill_yes_price = fp_raw
        await _maybe_fill_verification_notify(
            asset, ticker, side, market, secs_left,
            _original_strategy_target_c, price_this_attempt,
            _market_ask_at_post_c, _fill_yes_price,
        )
        return {"fill_confirmed": cc > 0, "fill_price_cents": fp, "order_id": order_id, "filled_contracts": cc}

    # â”€â”€ Live mode: single market order â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Notify for late-window entries (<5 min) — user watches Telegram closely here.
    if secs_left < 300.0:
        _placed_mins = int(secs_left // 60)
        _placed_secs = int(secs_left % 60)
        _placed_elapsed = seconds_elapsed(market) if market else 0.0
        _placed_ctx = _notify_ctx(
            asset, ticker, (_placed_elapsed + secs_left) / 60.0,
            _phase_for_eth(asset, _placed_elapsed),
        )
        asyncio.create_task(send_telegram(
            f"<b>[S2 D3 Hybrid] {_placed_ctx} MARKET ORDER PLACED</b>\n"
            f"<b>{side.upper()} — {'UP' if side == 'yes' else 'DOWN'}</b>  {contracts} contracts\n"
            f"Expires in {_placed_mins}m {_placed_secs}s"
        ))

    log.info(f"[live] market {side.upper()} {contracts}x on {ticker} ({secs_left:.0f}s left)")

    for attempt in range(2):  # 2 attempts intentional — late-window 15m trades can't afford long retry loops
        if attempt > 0:
            log.info(f"[live] Market order retry {attempt}/1...")
            await asyncio.sleep(1.0)

        client_order_id = f"kalshi_{int(time.time() * 1000)}_{attempt}"
        body = {
            "ticker": ticker,
            "side": side,
            "type": "market",
            "count": contracts,
            "action": "buy",
            "client_order_id": client_order_id,
        }

        try:
            async with session.post(
                KALSHI_BASE_URL + path,
                headers=kalshi_headers("POST", path),
                json=body,
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as resp:
                data = await resp.json()
                http_status = resp.status
        except Exception as exc:
            log.error(f"[live] Order POST failed (attempt {attempt}): {exc}")
            try:
                chk_path = f"/portfolio/positions?ticker={ticker}"
                async with session.get(
                    KALSHI_BASE_URL + chk_path,
                    headers=kalshi_headers("GET", chk_path),
                    timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
                ) as chk_resp:
                    chk_data = await chk_resp.json()
                positions = chk_data.get("market_positions") or chk_data.get("positions") or []
                for p in positions:
                    if p.get("ticker") == ticker:
                        held = p.get("position", 0)
                        if (side == "yes" and held > 0) or (side == "no" and held < 0):
                            held_count = abs(held)
                            log.info(f"POST exception but portfolio shows {held_count}x — treating as filled")
                            return {"fill_confirmed": True, "fill_price_cents": price_this_attempt, "order_id": None, "filled_contracts": held_count}
            except Exception as chk_exc:
                log.error(f"Portfolio check after POST exception failed: {chk_exc}")
            continue

        if http_status == 429:
            log.warning(f"[live] Rate-limited (429) on attempt {attempt} — waiting 2s")
            await asyncio.sleep(2.0)
            continue

        if http_status not in (200, 201):
            log.error(f"[live] Order HTTP {http_status}: {data}")
            err_code = (data.get("error") or {}).get("code", "")
            _non_retryable = {
                "insufficient_funds", "authentication_error", "not_found", "forbidden",
                "market_not_open", "market_settled", "market_not_found",
                "order_limit_exceeded", "contract_limit_exceeded", "position_limit_exceeded",
                "invalid_count", "invalid_order", "min_contracts_not_met",
            }
            if err_code in _non_retryable:
                log.error(f"Non-retryable error ({err_code}). Stopping order attempts.")
                _failed_elapsed = seconds_elapsed(market) if market else 0.0
                _failed_ctx = _notify_ctx(
                    asset, ticker, (_failed_elapsed + secs_left) / 60.0,
                    _phase_for_eth(asset, _failed_elapsed),
                )
                await send_telegram(
                    f"<b>[S2 D3 Hybrid] {_failed_ctx} MARKET ORDER FAILED</b>  —  {err_code}\n"
                    f"{side.upper()}  {contracts}x"
                )
                break
            continue

        order_id = (data.get("order") or {}).get("order_id") or data.get("order_id")
        if not order_id:
            log.error(f"[live] No order_id in response: {data}")
            continue

        post_order  = data.get("order") or data
        post_status = post_order.get("status", "")
        log.info(f"[live] Order {order_id} POST status={post_status!r}")

        # Market orders should not rest, but poll briefly as a safety net
        if post_status in ("resting", "pending"):
            log.warning(f"[live] Market order {order_id} returned {post_status!r} — polling 5s")
            for _pi in range(5):
                await asyncio.sleep(1.0)
                try:
                    _op = f"/portfolio/orders/{order_id}"
                    async with session.get(
                        KALSHI_BASE_URL + _op,
                        headers=kalshi_headers("GET", _op),
                        timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
                    ) as _r:
                        _d = await _r.json()
                    _polled = _d.get("order") or _d
                    if _polled.get("status", "") not in ("resting", "pending"):
                        post_order  = _polled
                        post_status = _polled.get("status", "")
                        log.info(f"[live] Order {order_id} status changed to {post_status!r} (poll {_pi+1}/5)")
                        break
                except Exception:
                    break
            if post_status in ("resting", "pending"):
                try:
                    del_path = f"/portfolio/orders/{order_id}"
                    async with session.delete(
                        KALSHI_BASE_URL + del_path,
                        headers=kalshi_headers("DELETE", del_path),
                        timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
                    ) as _del_resp:
                        await _del_resp.json()
                except Exception as _de:
                    log.warning(f"[live] Cancel of resting market order {order_id} failed: {_de}")
                continue

        if post_status in ("canceled", "cancelled"):
            _total     = post_order.get("contracts_count") or contracts
            _remaining = post_order.get("remaining_count")
            _remaining = _remaining if _remaining is not None else _total
            _filled    = _total - _remaining
            if _filled > 0:
                _fp_raw      = post_order.get("yes_price", price_this_attempt)
                _fp_canceled = _fp_raw if side == "yes" else (100 - _fp_raw)
                log.info(f"[live] Market order {order_id} partial fill: {_filled}/{_total} @ {_fp_canceled}c")
                _fill_yes_price = _fp_raw
                await _maybe_fill_verification_notify(
                    asset, ticker, side, market, secs_left,
                    _original_strategy_target_c, price_this_attempt,
                    _market_ask_at_post_c, _fill_yes_price,
                )
                _verified = await _verify_order_fill(session, order_id, _filled, ticker, side)
                return {"fill_confirmed": _verified, "fill_price_cents": _fp_canceled, "order_id": order_id, "filled_contracts": _filled}
            log.info(f"[live] Market order {order_id} zero-fill")
            break

        _total     = post_order.get("contracts_count")
        _remaining = post_order.get("remaining_count")
        _fc        = post_order.get("filled_count")
        if _fc is not None:
            filled_count = _fc
        elif _total is not None and _remaining is not None:
            filled_count = _total - _remaining
        elif _total is not None:
            filled_count = _total
        else:
            filled_count = contracts

        if filled_count == 0:
            log.warning(f"[live] Order {order_id} status={post_status!r} but filled_count=0")
            continue

        _fill_yes_price = post_order.get("yes_price", price_this_attempt)
        fill_price = _fill_yes_price if side == "yes" else (100 - _fill_yes_price)
        log.info(f"[live] Order FILLED: {order_id} @ {fill_price}c x{filled_count} status={post_status!r}")
        await _maybe_fill_verification_notify(
            asset, ticker, side, market, secs_left,
            _original_strategy_target_c, price_this_attempt,
            _market_ask_at_post_c, _fill_yes_price,
        )
        _verified = await _verify_order_fill(session, order_id, filled_count, ticker, side)
        return {"fill_confirmed": _verified, "fill_price_cents": fill_price, "order_id": order_id, "filled_contracts": filled_count}

    # Portfolio check — ground truth after all attempts exhausted
    log.warning(f"Market order not confirmed for {ticker} — checking portfolio")
    try:
        pos_path = f"/portfolio/positions?ticker={ticker}"
        async with session.get(
            KALSHI_BASE_URL + pos_path,
            headers=kalshi_headers("GET", pos_path),
            timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
        ) as resp:
            pos_data = await resp.json()
        positions = pos_data.get("market_positions") or pos_data.get("positions") or []
        for p in positions:
            if p.get("ticker") == ticker:
                held = p.get("position", 0)
                # _attempted_tickers (set before place_order) prevents matching a prior trade's held position
                if side == "yes" and held > 0:
                    log.info(f"Portfolio check: found YES position {held}x on {ticker} — order DID fill")
                    return {"fill_confirmed": True, "fill_price_cents": entry_price_cents, "order_id": None, "filled_contracts": held}
                if side == "no" and held < 0:
                    held_no = abs(held)
                    log.info(f"Portfolio check: found NO position {held_no}x on {ticker} — order DID fill")
                    return {"fill_confirmed": True, "fill_price_cents": entry_price_cents, "order_id": None, "filled_contracts": held_no}
        log.info(f"Portfolio check: no position found for {ticker}")
    except Exception as exc:
        log.error(f"Portfolio check error: {exc}")

    log.error(f"Market order not filled for {ticker} {side}")
    _nofill_elapsed = seconds_elapsed(market) if market else 0.0
    _nofill_ctx = _notify_ctx(
        asset, ticker, (_nofill_elapsed + secs_left) / 60.0,
        _phase_for_eth(asset, _nofill_elapsed),
    )
    await send_telegram(
        f"<b>[S2 D3 Hybrid] {_nofill_ctx} MARKET ORDER NOT FILLED</b>  —  no liquidity\n"
        f"{side.upper()} — {'UP' if side == 'yes' else 'DOWN'}  {contracts}x"
    )
    return {"fill_confirmed": False, "fill_price_cents": None, "order_id": None}


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Daily limits
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

async def check_daily_limits(config: dict) -> tuple[bool, str]:
    """
    Check daily loss limit and profit target for live/demo mode.

    live  — DLL/profit target flips mode to 'paper' in config.json
    demo  — DLL disables bot entirely and fires Telegram; profit target flips to paper

    Returns:
        (triggered: bool, reason: str)
    """
    global limit_triggered, limit_reason, pre_limit_mode

    mode = config.get("mode", "paper")
    if mode == "paper":
        return False, ""

    pnl = await db_get_today_pnl(mode)

    if pnl < 0 and abs(pnl) >= config.get("daily_loss_limit_dollars", 20):
        if not limit_triggered:
            limit_triggered = True
            limit_reason = "daily loss limit reached"
            pre_limit_mode = mode
            cfg = read_config()
            if mode == "demo":
                cfg["bot_enabled"] = False
                write_config(cfg)
                log.warning(f"Demo DLL hit (${pnl:.2f}). Bot disabled.")
                await send_telegram(
                    f"<b>[DEMO] Daily loss limit — bot disabled</b>\n"
                    f"PnL today: <b>${pnl:.2f}</b>\n"
                    f"Bot has been disabled. Re-enable manually in config."
                )
            else:
                cfg["mode"] = "paper"
                write_config(cfg)
                log.warning(f"Daily loss limit hit (${pnl:.2f}). Switched to paper mode.")
                await send_telegram(
                    f"<b>Daily loss limit triggered</b>\n"
                    f"PnL today: <b>${pnl:.2f}</b>\n"
                    f"Switched to paper mode."
                )
        return True, limit_reason

    if pnl > 0 and pnl >= config.get("daily_profit_target_dollars", 50):
        if not limit_triggered:
            limit_triggered = True
            limit_reason = "daily profit target reached"
            pre_limit_mode = mode
            cfg = read_config()
            cfg["mode"] = "paper"
            write_config(cfg)
            log.info(f"Daily profit target hit (${pnl:.2f}). Switched to paper mode.")
        return True, limit_reason

    return False, ""


def midnight_reset() -> None:
    """
    Reset daily-limit state at UTC midnight.
    Restores the pre-limit mode if limits had previously triggered.
    """
    global limit_triggered, limit_reason, pre_limit_mode, daily_reset_date

    today = datetime.now(timezone.utc).date()
    if daily_reset_date is None:
        daily_reset_date = today
        return

    if today > daily_reset_date:
        daily_reset_date = today
        log.info("Midnight UTC: resetting daily limits.")
        if limit_triggered and pre_limit_mode:
            cfg = read_config()
            cfg["mode"] = pre_limit_mode
            write_config(cfg)
            log.info(f"Restored mode to '{pre_limit_mode}' after midnight reset.")
        limit_triggered = False
        limit_reason = ""
        pre_limit_mode = None


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  State file
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

_STRIKE_RE_T_SUFFIX = re.compile(r"-T(\d+)$")
_STRIKE_RE_NUMERIC_SUFFIX = re.compile(r"-(\d+)$")

def _parse_strike_from_ticker(ticker):
    """Parse strike price out of a Kalshi ticker.

    Hourly tickers use `-T<strike>` suffix; 15-minute tickers use `-<strike>`.
    Returns None if no pattern matches or ticker is falsy.
    """
    if not ticker:
        return None
    m = _STRIKE_RE_T_SUFFIX.search(ticker)
    if m:
        return int(m.group(1))
    m = _STRIKE_RE_NUMERIC_SUFFIX.search(ticker)
    if m:
        return int(m.group(1))
    return None

async def write_state_file(
    config: dict,
    market: dict | None,
    phase: str,
    secs_left: float,
    btc_price: float | None,
    score: int,
    breakdown: dict,
    action: str,
    skip_reason: str,
) -> None:
    """Write a JSON snapshot of current bot state for server.py to serve."""
    state = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "market_ticker": market.get("ticker", "") if market else "",
        "market_title": market.get("title", "") if market else "",
        "phase": phase,
        "seconds_remaining": secs_left,
        "btc_price": btc_price,
        "confidence_score": score,
        "confidence_breakdown": breakdown,
        "reward_tier": _brain_cal["reward_tier"],
        "brain_wr": _brain_cal["overall_wr"],
        "brain_min_edge": _brain_cal["min_edge_override"],
        "brain_n": _brain_cal["last_count"],
        "last_action": action,
        "last_skip_reason": skip_reason,
        "mode": config.get("mode", "paper"),
        "today_live_pnl": await db_get_today_pnl("live"),
        "today_paper_pnl": await db_get_today_pnl("paper"),
        "today_demo_pnl": await db_get_today_pnl("demo"),
        "config": {**config,
                   "min_ev_pct": round((config.get("min_ev_base", 3.0) / 100.0 + _session_ev_adjustment()) * 100),
                   "vol_gate_thresh": config.get("vol_gate_thresh", 1.80)},
        "limit_triggered": limit_triggered,
        "limit_reason": limit_reason,
        "open_position": current_position,
        "consecutive_losses": _consecutive_losses,
    }

    # Per-asset snapshot for multi-asset dashboard display
    assets_snap: dict = {}
    # Non-BTC assets — pulled from the in-memory _asset_states dict
    for _a, _st in _asset_states.items():
        _m  = _st.get("market")
        _sl = seconds_remaining(_m) if _m else 0
        _ev = _st.get("eval", {})
        _a_phase = _st.get("phase", "DONE")
        _a_status = "TRADING" if _a_phase == "LOCKED" else (_ev.get("status") or _a_phase)
        # Dashboard-extension fields (session_type / strategy_name / strike / phase).
        # Market duration is derived from open_time/close_time when available, and
        # falls back to elapsed+remaining when the timestamps are missing.
        _a_ticker = _m.get("ticker", "") if _m else ""
        try:
            _a_elapsed_sec = seconds_elapsed(_m) if _m else 0.0
        except Exception:
            _a_elapsed_sec = 0.0
        _a_duration_min = (float(_a_elapsed_sec) + float(_sl)) / 60.0 if _m else 0.0
        _a_session_type = "15m"
        _a_strategy_name = _strategy_name_for(_a, _a_duration_min)
        _a_strike = _parse_strike_from_ticker(_a_ticker)
        if _a_strike is None:
            _a_strike = _ev.get("strike")
            if _a_strike is None and _m:
                _a_strike = _m.get("strike_price")
        _a_window_phase = _phase_for_eth(_a, _a_elapsed_sec)
        assets_snap[_a] = {
            "price":        _am_get_price(_a),
            "phase":        _a_phase,
            "ticker":       _a_ticker,
            "market_title": _m.get("title",  "") if _m else "",
            "secs_left":    _sl,
            "price_age":    _am_price_age(_a),
            "strike":       _a_strike,
            "distance_pct": _ev.get("distance_pct"),
            "direction":    _ev.get("direction"),
            "yes_ask":      _ev.get("yes_ask"),
            "no_ask":       _ev.get("no_ask"),
            "ev":           _ev.get("ev"),
            "win_prob":     _ev.get("win_prob"),
            "status":       _a_status,
            "skip_reason":  _ev.get("skip_reason"),
            "signals":      _ev.get("signals", {}),
            "position":     _st.get("position"),
            "session_type": _a_session_type,
            "strategy_name": _a_strategy_name,
            "phase_label":  _a_window_phase,
            "window_phase": _a_window_phase,
        }
    # BTC — uses separate globals; _asset_eval["BTC"] holds last eval snapshot
    _btc_ev = _asset_eval.get("BTC", {})
    _btc_status = "TRADING" if phase == "LOCKED" else (_btc_ev.get("status") or phase)
    _btc_ticker = market.get("ticker", "") if market else ""
    try:
        _btc_elapsed_sec = seconds_elapsed(market) if market else 0.0
    except Exception:
        _btc_elapsed_sec = 0.0
    _btc_duration_min = (float(_btc_elapsed_sec) + float(secs_left)) / 60.0 if market else 0.0
    _btc_session_type = "15m"
    _btc_strategy_name = _strategy_name_for("BTC", _btc_duration_min)
    _btc_strike = _parse_strike_from_ticker(_btc_ticker)
    if _btc_strike is None:
        _btc_strike = _btc_ev.get("strike")
        if _btc_strike is None and market:
            _btc_strike = market.get("strike_price")
    _btc_window_phase = _phase_for_eth("BTC", _btc_elapsed_sec)  # always None for BTC
    assets_snap["BTC"] = {
        "price":        btc_price,
        "phase":        phase,
        "ticker":       _btc_ticker,
        "market_title": market.get("title",  "") if market else "",
        "secs_left":    secs_left,
        "price_age":    _am_price_age("BTC"),
        "strike":       _btc_strike,
        "distance_pct": _btc_ev.get("distance_pct"),
        "direction":    _btc_ev.get("direction"),
        "yes_ask":      _btc_ev.get("yes_ask"),
        "no_ask":       _btc_ev.get("no_ask"),
        "ev":           _btc_ev.get("ev"),
        "win_prob":     _btc_ev.get("win_prob"),
        "status":       _btc_status,
        "skip_reason":  skip_reason or _btc_ev.get("skip_reason", ""),
        "signals":      _btc_ev.get("signals", {}),
        "position":     current_position,
        "session_type": _btc_session_type,
        "strategy_name": _btc_strategy_name,
        "phase_label":  _btc_window_phase,
        "window_phase": _btc_window_phase,
    }
    state["assets"] = assets_snap

    try:
        atomic_write_json(state, _STATE_FILE)
    except Exception as exc:
        log.error(f"State file write error: {exc}")


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Phase handlers
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

async def _log_entry(
    market: dict,
    phase: str,
    secs_left: float,
    btc_price: float,
    strike: float,
    contract_price: int | None,
    score: int,
    action: str,
    skip_reason: str,
    mode: str,
) -> None:
    """Write one row to market_log."""
    await db_write_market_log({
        "ts": datetime.now(timezone.utc).isoformat(),
        "market_id": market.get("ticker", ""),
        "market_title": market.get("title", ""),
        "phase": phase,
        "seconds_left": int(secs_left),
        "btc_price": btc_price,
        "strike": strike,
        "contract_price_cents": contract_price,
        "confidence_score": score,
        "action": action,
        "skip_reason": skip_reason,
        "mode": mode,
    })


async def _execute_s1_trade(
    session: "aiohttp.ClientSession",
    brain_s1: dict,
    ticker: str,
    btc_price: float,
    strike: float,
    yes_ask: float,
    no_ask: float,
    elapsed_seconds: float,
    secs_left: float,
    asset: str,
    config: dict,
    mode: str,
    ob: dict,
    market: "dict | None" = None,
) -> None:
    """Place a real S1 order alongside S2 and track it in _s1_pending_trades."""
    global _s1_pending_trades
    if brain_s1.get("action") != "trade":
        return
    if ticker in _s1_pending_trades:
        return  # already have an open S1 trade on this ticker

    side = brain_s1.get("side", "yes")
    entry_price_cents = yes_ask if side == "yes" else no_ask
    avail_liquidity = ob["yes_liquidity"] if side == "yes" else ob["no_liquidity"]
    trade_amount = float(config.get("trade_amount_dollars", 25))
    contracts, dollars_used = calculate_contracts(trade_amount, int(entry_price_cents), avail_liquidity)
    if contracts == 0 or dollars_used < trade_amount * 0.90:
        return

    result = await place_order(session, ticker, side, contracts, int(entry_price_cents), mode, market, asset=asset, secs_left=secs_left)
    if not result["fill_confirmed"]:
        log.info(f"[S1] {ticker}: order not filled -- skipping")
        return
    _fp = result.get("fill_price_cents")
    fill_price = _fp if _fp is not None else int(entry_price_cents)
    _fc = result.get("filled_contracts")
    contracts = _fc if _fc is not None else contracts

    _fee_rate = config.get("kalshi_fee_per_contract_cents", 7) / 100.0
    _entry_p  = fill_price / 100.0
    _fee      = _fee_rate * (1.0 - _entry_p)
    win_prob  = brain_s1.get("win_prob", 0.5)
    ev_val    = round((win_prob - _entry_p - _fee) * 100, 1)
    _ev_str   = f"+{ev_val}%" if ev_val >= 0 else f"{ev_val}%"

    import json as _json
    trade_data = {
        "ts":                   datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "market_id":            ticker,
        "market_title":         ticker,
        "mode":                 mode,
        "side":                 side,
        "contracts":            contracts,
        "entry_price_cents":    fill_price,
        "trade_amount_dollars": round(dollars_used, 2),
        "confidence_score":     brain_s1.get("confidence", 50),
        "model_prob":           win_prob,
        "implied_prob":         _entry_p,
        "btc_price_at_entry":   btc_price,
        "strike":               strike,
        "seconds_left_at_entry": int(secs_left),
        "fill_confirmed":       1,
        "outcome":              "pending",
        "order_id":             result.get("order_id"),
        "asset":                asset,
        "raw_p_yes":            brain_s1.get("raw_p_yes"),
        "entry_signals":        _json.dumps(brain_s1.get("signals", {})),
        "strategy_variant":     "strategy1",
    }
    trade_id = await db_write_trade(trade_data)
    if trade_id is None:
        return

    _s1_pending_trades[ticker] = {
        "trade_id":          trade_id,
        "side":              side,
        "entry_price_cents": fill_price,
        "contracts":         contracts,
        "strike":            strike,
        "asset":             asset,
        "mode":              mode,
        "entry_ts":          time.time(),
        "market_close_time": (market or {}).get("close_time", ""),
    }

    mode_icon = {"paper": "[PAPER]", "demo": "[DEMO]"}.get(mode, "[LIVE]")
    _win_pct  = int(win_prob * 100)
    _payout   = round((100 - fill_price) * contracts / 100, 2)
    _cost     = round(fill_price * contracts / 100, 2)
    log.info(f"[S1] {ticker}: ORDER FILLED -- {side.upper()} {contracts}x @ {fill_price}c")
    await send_telegram(
        f"<b>[S1 Original] {asset} {mode_icon} ORDER FILLED</b>\n"
        f"<b>{side.upper()} -- {'UP' if side == 'yes' else 'DOWN'}</b>  {contracts} contracts @ <b>{fill_price}c</b>\n"
        f"Cost: ${_cost:.2f}  |  Max payout: ${_payout:.2f}\n"
        f"Win prob: {_win_pct}%  |  EV: {_ev_str}\n"
        f"Strike: ${strike:,.0f}  |  {asset}: ${btc_price:,.0f}\n"
        f"Expires {int(secs_left // 60)}m {int(secs_left % 60)}s"
    )


async def _settle_s1_trade(
    ticker: str,
    market_result: "str | None",
    btc_price: float,
    config: dict,
    asset: str,
) -> None:
    """Settle a pending S1 real trade. market_result is 'yes', 'no', or None (price fallback)."""
    global _s1_pending_trades
    s1_pos = _s1_pending_trades.pop(ticker, None)
    if s1_pos is None:
        return

    if market_result == "yes":
        outcome = "win" if s1_pos["side"] == "yes" else "loss"
    elif market_result == "no":
        outcome = "win" if s1_pos["side"] == "no" else "loss"
    else:
        outcome = "win" if (
            (s1_pos["side"] == "yes" and btc_price > s1_pos["strike"]) or
            (s1_pos["side"] == "no"  and btc_price <= s1_pos["strike"])
        ) else "loss"

    exit_price = 100 if outcome == "win" else 0
    _entry_p   = s1_pos["entry_price_cents"] / 100.0
    _fee_rate  = config.get("kalshi_fee_per_contract_cents", 7) / 100.0
    fee  = math.ceil(_fee_rate * s1_pos["contracts"] * _entry_p * (1.0 - _entry_p) * 100) / 100
    pnl  = (exit_price - s1_pos["entry_price_cents"]) * s1_pos["contracts"] / 100 - fee
    profit_pct = (exit_price - s1_pos["entry_price_cents"]) / s1_pos["entry_price_cents"] * 100 \
                 if s1_pos["entry_price_cents"] else 0

    await db_update_trade(s1_pos["trade_id"], {
        "exit_price_cents": exit_price,
        "exit_reason":      "expiry",
        "outcome":          outcome,
        "pnl_dollars":      round(pnl, 2),
        "profit_percent":   round(profit_pct, 2),
    })
    log.info(f"[S1] {ticker}: settled -- {outcome}, P&L=${pnl:.2f}")

    pnl_str     = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
    outcome_str = "WIN" if pnl >= 0 else "LOSS"
    pct_str     = f"+{profit_pct:.0f}%" if profit_pct >= 0 else f"{profit_pct:.0f}%"
    mode_icon   = {"paper": "[PAPER]", "demo": "[DEMO]"}.get(s1_pos["mode"], "[LIVE]")
    _time_str   = datetime.now(timezone(timedelta(hours=-7))).strftime("%b %d %I:%M %p PST")
    _now        = time.time()
    _dur_secs   = int(_now - s1_pos.get("entry_ts", _now))
    _dur_str    = f"{_dur_secs // 60}m {_dur_secs % 60}s"
    await send_telegram(
        f"<b>[S1 Original] {asset} {mode_icon} {outcome_str}  {pnl_str}  ({pct_str})</b>  --  {_time_str}\n"
        f"{s1_pos['side'].upper()}  {s1_pos['contracts']} contracts  |  held {_dur_str}\n"
        f"Entry: {s1_pos['entry_price_cents']}c  ->  Expiry: {exit_price}c\n"
        f"Strike: ${s1_pos['strike']:,.0f}"
    )


async def _try_settle_orphaned_s1(
    session: "aiohttp.ClientSession",
    ticker: str,
    btc_price: float,
    config: dict,
    asset: str,
) -> None:
    """Settle an S1 trade when the market expired but S2 never locked."""
    if ticker not in _s1_pending_trades:
        return
    market_result = None
    for _attempt in range(3):
        try:
            _path = f"/markets/{ticker}"
            async with session.get(
                KALSHI_BASE_URL + _path,
                headers=kalshi_headers("GET", _path),
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as _resp:
                _mdata = await _resp.json()
            market_result = (_mdata.get("market") or _mdata).get("result")
            if market_result in ("yes", "no"):
                break
        except Exception as _exc:
            log.warning(f"[S1 orphan] market result fetch error (attempt {_attempt}): {_exc}")
        await asyncio.sleep(5)
    await _settle_s1_trade(ticker, market_result, btc_price, config, asset)

async def handle_ready_phase(
    session: aiohttp.ClientSession,
    config: dict,
    market: dict,
    ticker: str,
    btc_price: float,
    secs_left: float,
    strike: float,
    elapsed: float,
    asset: str = "BTC",
    state: dict | None = None,
) -> None:
    """
    Evaluate entry conditions for one READY-phase iteration.
    Advances to LOCKED on a successful fill, or logs the skip reason.

    asset: which asset this market is for ("BTC", "ETH", etc.)
    state: per-asset state dict (non-BTC); mutations go here instead of globals.
           Must contain keys: "phase", "position", "order_attempted" (set).
    """
    global current_phase, current_position
    global last_confidence_score, last_confidence_breakdown
    global last_action, last_skip_reason

    _use_state = state is not None
    mode = config.get("mode", "paper")

    # Hard expiry gate — truly nothing to do in the last 90 seconds
    if secs_left < 90:
        log.info(f"{ticker}: < 90s remaining. Moving to DONE.")
        if _use_state: state["phase"] = "DONE"
        else: current_phase = "DONE"
        return

    # Early-window gate — skip first 90s while price is still anchoring
    _elapsed = seconds_elapsed(market)
    if _elapsed < 90:
        log.debug(f"{ticker}: {_elapsed:.0f}s elapsed — price anchoring, skipping")
        return

    # â”€â”€ Multi-window best-pick (BTC only) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # If multiple 15-min windows are open simultaneously, evaluate all of them
    # and trade the one with the highest EV. Falls back to primary market if
    # only one window is open or fetching alternatives fails.
    # Non-BTC assets use a single market per cycle (no multi-window support).
    global current_market
    if asset == "BTC":
        try:
            all_windows = await fetch_current_market(session, return_all=True)
            if isinstance(all_windows, list) and len(all_windows) > 1:
                log.info(f"Multi-window: {len(all_windows)} open windows — evaluating all for best EV.")
                best_market  = market
                best_ticker  = ticker
                best_ev      = None
                best_ob      = None
                best_strike  = strike
                for candidate in all_windows:
                    try:
                        c_ticker   = candidate.get("ticker", "")
                        c_strike   = parse_strike(candidate)
                        if c_strike is None:
                            continue
                        # Use each window's own timing — they may have different close times
                        c_secs_left = seconds_remaining(candidate)
                        c_elapsed   = seconds_elapsed(candidate)
                        # Skip windows that are too close to expiry — same gate as primary market.
                        # Without this, the multi-window picker can select a market with 40s left
                        # AFTER the 90s gate already passed for the primary market.
                        if c_secs_left < 90:
                            log.info(f"  Window {c_ticker}: skipping — only {c_secs_left:.0f}s left")
                            continue
                        c_ob = await fetch_orderbook(session, c_ticker, candidate)
                        if c_ob is None:
                            continue
                        c_brain = strategy_brain_s2(
                            btc_price, c_strike,
                            c_ob["best_yes_ask"], c_ob["best_no_ask"],
                            c_elapsed, c_secs_left, c_ticker,
                            min_ev_base=get_asset_config(config, asset, "min_ev_base", 3.0),
                            vol_gate_thresh=get_asset_config(config, asset, "vol_gate_thresh", 1.80),
                            kalshi_fee=config.get("kalshi_fee_per_contract_cents", 7) / 100,
                            max_entry_price_cents=get_asset_config(config, asset, "max_entry_price_cents", 100.0),
                            min_reward_cents=get_asset_config(config, asset, "min_reward_cents", 0.0),
                            max_risk_reward_ratio=get_asset_config(config, asset, "max_risk_reward_ratio", 999.0),
                            asset=asset,
                        )
                        c_win_prob = c_brain.get("win_prob", 0.5)
                        c_entry    = c_ob["best_yes_ask"] if c_brain["side"] == "yes" else c_ob["best_no_ask"]
                        _c_fee_rate = config.get("kalshi_fee_per_contract_cents", 7) / 100
                        _c_p        = c_entry / 100.0
                        _c_fee      = _c_fee_rate * (1.0 - _c_p)
                        c_ev        = c_win_prob - _c_p - _c_fee
                        log.info(f"  Window {c_ticker}: ev={c_ev:+.1%} side={c_brain['side']} strike=${c_strike:,.0f}")
                        if best_ev is None or c_ev > best_ev:
                            best_ev     = c_ev
                            best_market = candidate
                            best_ticker = c_ticker
                            best_ob     = c_ob
                            best_strike = c_strike
                    except Exception as _exc:
                        log.warning(f"  Multi-window eval error for {candidate.get('ticker','?')}: {_exc}")
                if best_ticker != ticker:
                    log.info(f"Multi-window: switching to {best_ticker} (EV {best_ev:+.1%}) over {ticker}")
                    market   = best_market
                    ticker   = best_ticker
                    strike   = best_strike
                    secs_left = seconds_remaining(best_market)
                    elapsed   = seconds_elapsed(best_market)
                    current_market = best_market
                ob = best_ob
            else:
                ob = None  # fetch below
        except Exception as _mw_exc:
            log.warning(f"Multi-window evaluation error: {_mw_exc}")
            ob = None
    else:
        ob = None  # non-BTC: no multi-window, orderbook fetched below

    # Orderbook — retry next cycle if temporarily unavailable
    def _no_data_eval(reason: str) -> dict:
        return {
            "strike":       strike,
            "distance_pct": round(abs(btc_price - strike) / strike * 100, 3) if strike else None,
            "direction":    None,
            "yes_ask":      None,
            "no_ask":       None,
            "ev":           None,
            "win_prob":     None,
            "status":       "NO_DATA",
            "skip_reason":  reason,
            "signals":      {},
        }

    if ob is None:
        try:
            ob = await fetch_orderbook(session, ticker, market)
        except Exception as exc:
            log.error(f"[{asset}] Orderbook error in READY: {exc}")
            _snap = _no_data_eval(f"orderbook error: {exc}")
            if _use_state: state["eval"] = _snap
            else: _asset_eval[asset] = _snap
            return

    if ob is None:
        log.warning(f"[{asset}] {ticker}: orderbook returned no price data — retrying next cycle")
        _snap = _no_data_eval("no orderbook data — retrying")
        if _use_state: state["eval"] = _snap
        else: _asset_eval[asset] = _snap
        last_action, last_skip_reason = "watching", "no price data — retrying"
        return

    yes_ask = ob["best_yes_ask"]
    no_ask  = ob["best_no_ask"]   # fetched directly from no_ask_dollars, not derived

    # â”€â”€ Price validation: compare simulated vs real prices â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Logs to price_validation_log.csv so we can audit whether the backtest's
    # AMM simulation matches live Kalshi prices (reviewer flagged 8-15c gap risk).
    try:
        _sim_yes, _sim_no = _simulated_amm_midpoint(btc_price, strike)
        _log_price_validation(
            ts=datetime.now(timezone.utc).isoformat(),
            ticker=ticker,
            btc_price=btc_price,
            strike=strike,
            sim_yes=_sim_yes,
            sim_no=_sim_no,
            real_yes=yes_ask,
            real_no=no_ask,
            mins_remaining=secs_left / 60,
        )
    except Exception as _pv_exc:
        log.debug(f"Price validation log error: {_pv_exc}")

    # Track YES price for velocity signal
    track_contract_price(ticker, yes_ask)

    # â”€â”€ Printer Brain — primary decision engine (always runs, no API needed) â”€â”€
    brain = strategy_brain_s2(btc_price, strike, yes_ask, no_ask, elapsed, secs_left, ticker,
                     min_ev_base=get_asset_config(config, asset, "min_ev_base", 3.0),
                     vol_gate_thresh=get_asset_config(config, asset, "vol_gate_thresh", 1.80),
                     kalshi_fee=config.get("kalshi_fee_per_contract_cents", 7) / 100,
                     max_entry_price_cents=get_asset_config(config, asset, "max_entry_price_cents", 100.0),
                     min_reward_cents=get_asset_config(config, asset, "min_reward_cents", 0.0),
                     max_risk_reward_ratio=get_asset_config(config, asset, "max_risk_reward_ratio", 999.0),
                     asset=asset)
    brain_s1 = strategy_brain_s1(btc_price, strike, yes_ask, no_ask, elapsed, secs_left, ticker, asset=asset)
    await _execute_s1_trade(
        session, brain_s1, ticker, btc_price, strike, yes_ask, no_ask,
        elapsed, secs_left, asset, config, mode, ob, market,
    )
    side     = brain["side"]
    score    = brain["confidence"]
    do_trade = brain["action"] == "trade"
    skip_reason_ai = brain["reasoning"]

    # â”€â”€ allowed_sides gate — disable NO side when model is uncalibrated â”€â”€â”€â”€â”€â”€
    _side_aliases = {"up": "yes", "down": "no"}
    side = _side_aliases.get(side.lower(), side.lower()) if side else side
    _allowed_sides = get_asset_config(config, asset, "allowed_sides", config.get("allowed_sides"))
    _allowed_norm  = [_side_aliases.get(s.lower(), s.lower()) for s in (_allowed_sides or [])]
    if do_trade and _allowed_norm and side not in _allowed_norm:
        skip_reason_ai = f"side={side} not in allowed_sides={_allowed_sides}"
        do_trade = False


    # â”€â”€ Consecutive price-filter skip tracking â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    global _consecutive_price_skips
    if brain.get("price_filter_skip"):
        _consecutive_price_skips += 1
        if _consecutive_price_skips == 20:
            _max_ep = get_asset_config(config, asset, "max_entry_price_cents", 82)
            log.warning(
                f"Price filter: {_consecutive_price_skips} consecutive skips — "
                f"all entry prices > {_max_ep}c"
            )
    else:
        _consecutive_price_skips = 0

    entry_price_cents = yes_ask if side == "yes" else no_ask
    _fee_rate = config.get("kalshi_fee_per_contract_cents", 7) / 100
    _entry_p  = entry_price_cents / 100.0
    _fee      = _fee_rate * (1.0 - _entry_p)  # fee/stake ≈ fee_rate*(1-p); matches ev.py formula
    brain_ev  = brain.get("win_prob", 0.5) - _entry_p - _fee
    brain_win_prob = brain.get("win_prob", 0.5)

    # Dashboard eval snapshot — updated at every exit point below
    _eval_snap = {
        "strike":       strike,
        "distance_pct": round(abs(btc_price - strike) / strike * 100, 3) if strike else None,
        "direction":    "UP" if side == "yes" else "DOWN",
        "yes_ask":      yes_ask,
        "no_ask":       no_ask,
        "ev":           round(brain_ev * 100, 1),
        "win_prob":     round(brain_win_prob * 100, 1),
        "status":       "WATCHING",
        "skip_reason":  "",
        "signals":      brain.get("signals", {}),
    }

    entry_price_cents = yes_ask if side == "yes" else no_ask

    # Dashboard breakdown from Brain v3 components
    win_p_raw   = 0.70  # Supertrend assumed probability
    _mom_label  = brain.get("mom_label",  "neutral")
    _vel_signal = brain.get("vel_signal", "neutral")
    _abs_pct    = brain.get("abs_pct", abs((btc_price - strike) / strike))
    # Time score: less time remaining = outcome more certain = higher score (0→20)
    _time_score = round(max(0.0, min(20.0, 20.0 * (1.0 - secs_left / (13 * 60)))), 1)
    # Distance score: farther from strike = higher score (0→30, caps at 0.5%)
    _dist_score = round(min(30.0, _abs_pct * 100.0 / 0.5 * 30.0), 1)
    _brain_rv       = brain.get("_rv")
    _brain_vol_ratio = brain.get("_vol_ratio")
    breakdown = {
        "win_prob_raw":   round(win_p_raw * 100, 1),
        "win_prob_final": round(brain.get("win_prob", win_p_raw) * 100, 1),
        "ev":             round((brain.get("win_prob", 0.5) - entry_price_cents / 100 - _fee) * 100, 1),
        "contract_c":     round(entry_price_cents, 1),
        "momentum":       30 if _mom_label in ("bullish", "bearish") else 0,
        "momentum_label": _mom_label,
        "velocity":       30 if _vel_signal == "favorable" else (10 if _vel_signal == "neutral" else 0),
        "velocity_label": _vel_signal,
        "time":           _time_score,
        "distance":       _dist_score,
        "distance_pct":   round(_abs_pct * 100, 3),
        "side":           side,
        "vol_per_min":    round(_brain_rv * 100, 4) if _brain_rv is not None else None,
        "vol_ratio":      round(_brain_vol_ratio, 3) if _brain_vol_ratio is not None else None,
    }

    last_confidence_score = score
    last_confidence_breakdown = breakdown

    raw_win_pct = int(brain.get("win_prob", 0) * 100)
    conf_threshold = int(get_asset_config(config, asset, "confidence_threshold", config.get("confidence_threshold", 65)))
    if do_trade and raw_win_pct < conf_threshold:
        skip_reason_ai = f"win prob {raw_win_pct}% below floor {conf_threshold}%"
        do_trade = False

    # â”€â”€ Reversal model — runs whenever main strategy skips â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    _is_reversal = False

    if not do_trade:
        log.info(f"{ticker}: watching — {skip_reason_ai}")
        await _log_entry(market, "READY", secs_left, btc_price, strike,
                         int(entry_price_cents), score, "skip", skip_reason_ai, mode)
        _eval_snap.update({"status": "SKIPPED", "skip_reason": skip_reason_ai})
        if _use_state: state["eval"] = dict(_eval_snap)
        else: _asset_eval[asset] = dict(_eval_snap)
        last_action, last_skip_reason = "watching", skip_reason_ai
        return

    # Daily limits — may flip mode to paper
    limit_hit, _ = await check_daily_limits(config)
    if limit_hit:
        config = read_config()
        mode = config.get("mode", "paper")

    # Cooldown disabled — trade every session regardless of prior outcome

    # Position sizing — flat fixed amount
    # Reversal trades use 50% of configured amount (contrarian = smaller size)
    trade_amount = config.get("trade_amount_dollars", 25)
    avail_liquidity = ob["yes_liquidity"] if side == "yes" else ob["no_liquidity"]
    contracts, dollars_used = calculate_contracts(
        trade_amount, int(entry_price_cents), avail_liquidity,
    )
    if contracts == 0 or dollars_used < float(trade_amount) * 0.90:
        if contracts > 0:
            reason = (
                f"insufficient_liquidity: only {avail_liquidity} contracts available "
                f"(${dollars_used:.2f} of ${float(trade_amount):.0f} target — skip partial fill)"
            )
        else:
            reason = "trade amount too small for current contract price"
        log.info(f"{ticker}: {reason}")
        await _log_entry(market, "READY", secs_left, btc_price, strike,
                         int(entry_price_cents), score, "skip", reason, mode)
        _eval_snap.update({"status": "SKIPPED", "skip_reason": reason})
        if _use_state: state["eval"] = dict(_eval_snap)
        else: _asset_eval[asset] = dict(_eval_snap)
        last_action, last_skip_reason = "skip", reason
        return

    # Place order — mark ticker as attempted BEFORE placing so re-entry is blocked
    # even if the bot crashes or fill_confirmed comes back False
    if _use_state: state["order_attempted"].add(ticker)
    else: _order_attempted_tickers.add(ticker)
    log.info(f"{ticker}: TRADE {side} {contracts}x @ {int(entry_price_cents)}c (score={score}, mode={mode})")
    result = await place_order(session, ticker, side, contracts, int(entry_price_cents), mode, market, asset=asset, secs_left=secs_left)

    fill_confirmed = result["fill_confirmed"]
    _fp = result.get("fill_price_cents")
    fill_price = _fp if _fp is not None else int(entry_price_cents)
    order_id = result.get("order_id")
    # Use actual filled contract count (IOC may fill fewer than requested).
    # Must use explicit None check — 0 is falsy but a valid (unfilled) count.
    _fc = result.get("filled_contracts")
    contracts = _fc if _fc is not None else contracts

    trade_ts = datetime.now(timezone.utc).isoformat()

    await _log_entry(
        market, "READY", secs_left, btc_price, strike, int(entry_price_cents),
        score, "trade" if fill_confirmed else "skip",
        "" if fill_confirmed else "order not filled",
        mode,
    )

    if not fill_confirmed:
        if _use_state: state["phase"] = "DONE"
        else: current_phase = "DONE"
        _eval_snap.update({"status": "SKIPPED", "skip_reason": "order not filled"})
        if _use_state: state["eval"] = dict(_eval_snap)
        else: _asset_eval[asset] = dict(_eval_snap)
        last_action, last_skip_reason = "skip", "order not filled"
        log.info(f"{ticker}: order not filled. Moving to DONE.")
        return

    # Only write to trades DB when the order actually filled.
    # Unfilled attempts are recorded in market_log (via _log_entry above).
    trade_data = {
        "ts": trade_ts,
        "market_id": ticker,
        "market_title": market.get("title", ""),
        "mode": mode,
        "side": side,
        "contracts": contracts,
        "entry_price_cents": fill_price,
        "trade_amount_dollars": round(dollars_used, 2),
        "confidence_score": score,
        "model_prob": brain.get("win_prob", 0.5),
        "implied_prob": implied_prob(entry_price_cents),
        "btc_price_at_entry": btc_price,
        "strike": strike,
        "seconds_left_at_entry": int(secs_left),
        "fill_confirmed": 1,
        "exit_price_cents": None,
        "exit_reason": None,
        "outcome": "pending",
        "pnl_dollars": None,
        "profit_percent": None,
        "order_id":          order_id,
        "asset":             asset,
        "raw_p_yes":         brain.get("raw_p_yes"),
        "entry_signals":    json.dumps({
            "supertrend_direction": (brain.get("signals") or {}).get("supertrend_direction"),
            "supertrend_side":      (brain.get("signals") or {}).get("supertrend_side"),
            "market_prob":          (brain.get("signals") or {}).get("market_prob"),
            "p_ev":                 (brain.get("signals") or {}).get("p_ev"),
            "decision_mode":        (brain.get("signals") or {}).get("decision_mode"),
        }),
        "strategy_variant": "strategy2",
    }
    trade_id = await db_write_trade(trade_data)

    _entry_ts = time.time()
    _abs_pct_at_entry = abs(btc_price - strike) / strike
    _mins_left_at_entry = secs_left / 60
    # Record the market's total duration (elapsed + remaining at entry) so
    # exit-side notifications can reference the market's window length.
    try:
        _market_elapsed_at_entry = seconds_elapsed(market) if market else 0.0
    except Exception:
        _market_elapsed_at_entry = 0.0
    try:
        _market_duration_min = (_market_elapsed_at_entry + float(secs_left)) / 60.0
    except (TypeError, ValueError):
        _market_duration_min = 0.0
    _new_position = {
        "trade_id": trade_id,
        "ticker": ticker,
        "side": side,
        "contracts": contracts,
        "entry_price_cents": fill_price,
        "mode": mode,
        "strike": strike,
        "entry_ts": _entry_ts,
        "market_duration_min": _market_duration_min,
        "elapsed_at_entry": _market_elapsed_at_entry,  # used by exit-side _phase_for_eth
        "market_close_time": market.get("close_time", ""),
        "order_id": order_id,
        "asset": asset,
    }
    if _use_state:
        state["position"] = _new_position
        state["phase"] = "LOCKED"
    else:
        current_position = _new_position
        current_phase = "LOCKED"
    _eval_snap.update({"status": "TRADING", "skip_reason": ""})
    if _use_state: state["eval"] = dict(_eval_snap)
    else: _asset_eval[asset] = dict(_eval_snap)
    last_action, last_skip_reason = "trade", ""
    log.info(f"{ticker}: LOCKED.")
    mode_icon  = {"paper": "[PAPER]", "demo": "[DEMO]"}.get(mode, "[LIVE]")
    dir_icon   = "YES" if side == "yes" else "NO"
    _win_prob_used = _rev["prob"] if _is_reversal and _rev else brain.get("win_prob", 0)
    _win_pct   = int(_win_prob_used * 100)
    _ev        = round((_win_prob_used - fill_price / 100 - _fee) * 100, 1)
    _ev_str    = f"+{_ev}%" if _ev >= 0 else f"{_ev}%"
    _payout    = round((100 - fill_price) * contracts / 100, 2)
    _cost      = round(fill_price * contracts / 100, 2)
    _time_str  = datetime.now(timezone(timedelta(hours=-7))).strftime("%b %d %I:%M %p PST")
    _expiry_dt = datetime.now(timezone(timedelta(hours=-7))) + timedelta(seconds=secs_left)
    _expiry_str = _expiry_dt.strftime("%I:%M %p PST")
    _strat_tag = "REVERSAL" if _is_reversal else "ORDER FILLED"
    _fill_ctx = _notify_ctx(
        asset, ticker, (elapsed + secs_left) / 60.0,
        _phase_for_eth(asset, elapsed),
    )
    await send_telegram(
        f"<b>[S2 D3 Hybrid] {_fill_ctx} {mode_icon} {_strat_tag}</b>  —  {_time_str}\n"
        f"<b>{side.upper()} — {'UP' if side == 'yes' else 'DOWN'}</b>  {contracts} contracts @ <b>{fill_price}c</b>\n"
        f"Cost: ${_cost:.2f}  |  Max payout: ${_payout:.2f}\n"
        f"Win prob: {_win_pct}%  |  EV: {_ev_str}\n"
        f"Strike: ${strike:,.0f}  |  {asset}: ${btc_price:,.0f}\n"
        f"Expires {int(secs_left // 60)}m {int(secs_left % 60)}s -> {_expiry_str}"
    )


async def handle_locked_phase(
    session: aiohttp.ClientSession,
    btc_price: float,
    secs_left: float,
    config: dict,
    asset: str = "BTC",
    state: dict | None = None,
) -> None:
    """
    Hold an open position to expiry — exit at settlement.
    Exit only when the market settles and fetch the official Kalshi result.
    secs_left is passed as a fallback; the position's stored close_time is
    used when available so market rollovers don't break expiry detection.

    asset: which asset this position is for ("BTC", "ETH", etc.)
    state: per-asset state dict (non-BTC); mutations go here instead of globals.
    """
    global current_phase, current_position

    _use_state = state is not None
    _cur_pos = state["position"] if _use_state else current_position

    if _cur_pos is None:
        log.warning("LOCKED phase with no position. Moving to DONE.")
        if _use_state: state["phase"] = "DONE"
        else: current_phase = "DONE"
        return

    pos = _cur_pos
    ticker = pos["ticker"]
    strike = pos["strike"]

    # Compute secs_left from the position's stored market close time.
    # This is immune to market rollovers — the passed secs_left can be stale
    # (from a new market) when the old market has already expired.
    _stored_close = pos.get("market_close_time", "")
    if _stored_close:
        try:
            close_dt = datetime.fromisoformat(_stored_close.replace("Z", "+00:00"))
            secs_left = max(0.0, (close_dt - datetime.now(timezone.utc)).total_seconds())
        except Exception:
            pass  # fall back to caller-supplied secs_left

    # Expiry check
    if secs_left <= 0:
        # Ask Kalshi for the official settlement result — retry up to 6x (30s)
        # to give the exchange time to settle the market.
        market_result = None
        for _attempt in range(6):
            try:
                _path = f"/markets/{ticker}"
                async with session.get(
                    KALSHI_BASE_URL + _path,
                    headers=kalshi_headers("GET", _path),
                    timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
                ) as _resp:
                    _mdata = await _resp.json()
                market_result = (_mdata.get("market") or _mdata).get("result")
                if market_result in ("yes", "no"):
                    break
            except Exception as _exc:
                log.warning(f"Market result fetch error (attempt {_attempt}): {_exc}")
            await asyncio.sleep(5)

        if market_result == "yes":
            outcome = "win" if pos["side"] == "yes" else "loss"
        elif market_result == "no":
            outcome = "win" if pos["side"] == "no" else "loss"
        else:
            # Kalshi didn't settle in time — fall back to BTC price comparison
            log.warning(f"{ticker}: settlement result unavailable, falling back to BTC price check")
            outcome = "win" if (
                (pos["side"] == "yes" and btc_price > pos["strike"]) or
                (pos["side"] == "no"  and btc_price <= pos["strike"])
            ) else "loss"

        log.info(f"{ticker}: result={market_result!r} → {outcome}")
        exit_price = 100 if outcome == "win" else 0
        _entry_p = pos["entry_price_cents"] / 100.0
        _fee_rate = config.get("kalshi_fee_per_contract_cents", 7) / 100.0
        fee = math.ceil(_fee_rate * pos["contracts"] * _entry_p * (1.0 - _entry_p) * 100) / 100
        pnl = (exit_price - pos["entry_price_cents"]) * pos["contracts"] / 100 - fee
        profit_pct = (exit_price - pos["entry_price_cents"]) / pos["entry_price_cents"] * 100 \
                     if pos["entry_price_cents"] else 0

        log.info(f"{ticker} expired. Outcome={outcome}, P&L=${pnl:.2f} (fee=${fee:.2f})")
        pnl_str    = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        outcome_str = "✅ WIN" if pnl >= 0 else "❌ LOSS"


        await db_update_trade(pos["trade_id"], {
            "exit_price_cents": exit_price,
            "exit_reason": "expiry",
            "outcome": outcome,
            "pnl_dollars": round(pnl, 2),
            "profit_percent": round(profit_pct, 2),
        })

        # Clear position immediately — before notifications so a Telegram failure
        # never leaves the asset stuck in LOCKED indefinitely.
        if _use_state:
            state["position"] = None
            state["phase"] = "DONE"
        else:
            current_position = None
            current_phase = "DONE"

        # Consecutive-loss tracker (no pause — informational only)
        global _consecutive_losses
        if outcome == "win":
            _consecutive_losses = 0
        else:
            _consecutive_losses += 1
            max_cl = config.get("max_consecutive_losses", 5)
            if _consecutive_losses >= max_cl:
                _resume_str = "n/a"
                # Prefer the stored market duration so the session label is
                # stable regardless of how long the trade was held. Falls back
                # to held-time for any positions created before this field.
                _cl_dur_min = pos.get("market_duration_min") or (
                    (time.time() - pos.get("entry_ts", time.time())) / 60.0
                )
                _cl_ctx = _notify_ctx(
                    asset, pos.get("ticker", "?"), _cl_dur_min,
                    _phase_for_eth(asset, pos.get("elapsed_at_entry", 0)),
                )
                await send_telegram(
                    f"<b>[S2 D3 Hybrid] {_cl_ctx} {_consecutive_losses} consecutive losses</b>"
                )

        pct_str   = f"+{profit_pct:.0f}%" if profit_pct >= 0 else f"{profit_pct:.0f}%"
        mode_icon = {"paper": "[PAPER]", "demo": "[DEMO]"}.get(pos["mode"], "[LIVE]")
        _time_str = datetime.now(timezone(timedelta(hours=-7))).strftime("%b %d %I:%M %p PST")
        _dur_secs = int(time.time() - pos.get("entry_ts", time.time()))
        _dur_str  = f"{_dur_secs // 60}m {_dur_secs % 60}s"
        # Prefer the stored market duration so the session label is stable
        # regardless of hold length; fall back to held-time for backward
        # compat with positions created before this field existed.
        _close_dur_min = pos.get("market_duration_min") or (_dur_secs / 60.0)
        _close_ctx = _notify_ctx(
            asset, pos.get("ticker", ticker), _close_dur_min,
            _phase_for_eth(asset, pos.get("elapsed_at_entry", 0)),
        )
        await send_telegram(
            f"<b>[S2 D3 Hybrid] {_close_ctx} {mode_icon} {outcome_str}  {pnl_str}  ({pct_str})</b>  —  {_time_str}\n"
            f"{pos['side'].upper()}  {pos['contracts']} contracts  |  held {_dur_str}\n"
            f"Entry: {pos['entry_price_cents']}c  ->  Expiry: {exit_price}c\n"
            f"{asset}: ${btc_price:,.0f}  vs  Strike: ${pos['strike']:,.0f}"
        )
        await _settle_s1_trade(ticker, market_result, btc_price, config, asset)
        return

    # Still in the market — just hold and log
    log.info(
        f"[HOLDING] {ticker} | side={pos['side'].upper()} | entry={pos['entry_price_cents']}c "
        f"| price=${btc_price:,.4g} | strike=${pos['strike']:,.4g} | {secs_left:.0f}s left"
    )


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Non-BTC asset processing
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def _init_asset_state(asset: str) -> dict:
    """Return a fresh per-asset state dict."""
    return {
        "phase": "DONE",
        "position": None,
        "order_attempted": set(),
        "prev_ticker": None,
        "market": None,
    }


async def _process_asset(
    session: aiohttp.ClientSession,
    config: dict,
    asset: str,
) -> None:
    """
    Run one iteration of the trading state machine for a non-BTC asset.
    Called every cycle from _non_btc_asset_loop.
    """
    global _asset_states
    if asset not in _asset_states:
        _asset_states[asset] = _init_asset_state(asset)
    st = _asset_states[asset]

    # Price check
    price = _am_get_price(asset)
    if price is None:
        log.debug(f"[{asset}] no price yet — skipping")
        return
    age = _am_price_age(asset)
    if age is not None and age > 60:
        log.warning(f"[{asset}] price stale ({age:.0f}s) — skipping")
        return

    # Market fetch
    try:
        market = await fetch_market_for_asset(session, asset)
    except Exception as exc:
        log.warning(f"[{asset}] market fetch error: {exc}")
        return

    if market is None:
        # If a position is open with a stored close time, still run locked-phase handler
        # so it can settle. Repeated warnings each cycle are expected until close_time elapses.
        if st["phase"] == "LOCKED" and st.get("position") is not None:
            _close_time = st["position"].get("market_close_time", "")
            if not _close_time:
                log.error(f"[{asset}] LOCKED position missing market_close_time — cannot safely settle without market. Skipping.")
            else:
                log.warning(f"[{asset}] no active market — still processing open LOCKED position.")
                try:
                    await handle_locked_phase(session, price, 0, config, asset=asset, state=st)
                except Exception as exc:
                    log.error(f"[{asset}] LOCKED phase error (no market): {exc}", exc_info=True)
        else:
            log.debug(f"[{asset}] no active market")
            st["phase"] = "DONE"
            st["prev_ticker"] = None
        return

    st["market"] = market
    ticker = market.get("ticker", "")
    secs_left = seconds_remaining(market)
    elapsed = seconds_elapsed(market)

    # Strike
    try:
        strike = parse_strike(market)
    except Exception:
        strike = None
    if strike is None:
        yes_sub = market.get("yes_sub_title") or ""
        if "TBD" in yes_sub:
            strike = _am_get_price(asset)
            if strike:
                log.info(f"[{asset}] strike TBD — using live price {strike:.2f}")
            else:
                log.warning(f"[{asset}] cannot parse strike — skipping")
                return
        else:
            log.warning(f"[{asset}] cannot parse strike — skipping")
            return

    # Ticker rollover detection
    prev_ticker = st.get("prev_ticker")
    if prev_ticker is None:
        st["prev_ticker"] = ticker
        if st["phase"] == "DONE":
            st["phase"] = "WATCH"
            log.info(f"[{asset}] First market: {ticker}. Starting WATCH.")
    elif ticker != prev_ticker:
        if st["phase"] == "LOCKED":
            log.info(f"[{asset}] Market rolled to {ticker} but position still open on {prev_ticker} — staying LOCKED.")
            ticker = prev_ticker
            market = st.get("market") or market
        else:
            log.info(f"[{asset}] New market: {ticker} (was {prev_ticker}). Resetting to WATCH.")
            if prev_ticker in _s1_pending_trades:
                asyncio.create_task(_try_settle_orphaned_s1(session, prev_ticker, price, config, asset))
            st["phase"] = "WATCH"
            st["position"] = None
            st["order_attempted"].discard(prev_ticker)
            st["prev_ticker"] = ticker

    # WATCH
    if st["phase"] == "WATCH":
        if elapsed > WATCH_PHASE_SECONDS:
            log.info(f"[{asset}] {ticker}: elapsed {elapsed:.0f}s → READY.")
            st["phase"] = "READY"
        else:
            log.info(f"[{asset}] {ticker}: WATCH ({elapsed:.0f}s elapsed).")
            return

    # LOCKED
    if st["phase"] == "LOCKED":
        try:
            await handle_locked_phase(session, price, secs_left, config, asset=asset, state=st)
        except Exception as exc:
            log.error(f"[{asset}] LOCKED phase error: {exc}", exc_info=True)
        return

    # DONE
    if st["phase"] == "DONE":
        if secs_left > 3 * 60 and ticker not in st["order_attempted"]:
            log.info(f"[{asset}] DONE → READY re-entry: {ticker} has {secs_left:.0f}s left.")
            st["phase"] = "READY"
        else:
            log.info(f"[{asset}] DONE. {secs_left:.0f}s left — waiting for next market.")
            return

    # READY
    if st["phase"] == "READY":
        try:
            await handle_ready_phase(
                session, config, market, ticker,
                price, secs_left, strike, elapsed,
                asset=asset, state=st,
            )
        except Exception as exc:
            log.error(f"[{asset}] READY phase error: {exc}", exc_info=True)


async def _non_btc_asset_loop(session: aiohttp.ClientSession) -> None:
    """
    Independent 10-second loop processing all non-BTC enabled assets.
    Runs as a background asyncio task alongside main_loop (which handles BTC).
    """
    while True:
        try:
            config = read_config()
            if not config.get("bot_enabled", False):
                # Populate PAUSED state so dashboard shows prices instead of OFFLINE
                for _pa in config.get("enabled_assets", ["ETH", "SOL", "XRP"]):
                    if _pa == "BTC":
                        continue
                    if _pa not in _asset_states:
                        _asset_states[_pa] = {"phase": "PAUSED", "market": None, "eval": {}}
                    else:
                        _asset_states[_pa]["phase"] = "PAUSED"
                await asyncio.sleep(10)
                continue
            for asset in config.get("enabled_assets", ["ETH", "SOL", "XRP"]):
                if asset == "BTC":
                    continue
                try:
                    await _process_asset(session, config, asset)
                except Exception as exc:
                    log.error(f"Non-BTC asset loop error [{asset}]: {exc}", exc_info=True)
        except Exception as exc:
            log.error(f"Non-BTC asset loop outer error: {exc}", exc_info=True)
        await asyncio.sleep(10)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Main loop
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

async def main_loop() -> None:
    """
    Permanent 10-second loop driving all trading logic.
    All exceptions are caught per-iteration to prevent crashes.
    """
    global current_market, current_phase, current_position
    global last_confidence_score, last_confidence_breakdown
    global last_action, last_skip_reason
    global _order_attempted_tickers
    global _consecutive_losses

    prev_ticker: str | None = None

    # â”€â”€ Recover open position and consecutive-loss state after a crash/restart â”€
    try:
        with open(_STATE_FILE, "r") as _sf:
            _saved = json.load(_sf)
        _saved_pos = _saved.get("open_position")
        _saved_phase = _saved.get("phase", "")
        if _saved_pos and _saved_phase == "LOCKED" and _saved_pos.get("trade_id"):
            current_position = _saved_pos
            current_phase    = "LOCKED"
            log.warning(
                f"Recovered open position from state file: "
                f"trade_id={_saved_pos.get('trade_id')} "
                f"side={_saved_pos.get('side')} "
                f"ticker={_saved_pos.get('ticker')}"
            )
        saved_cl = _saved.get("consecutive_losses", 0)
        if isinstance(saved_cl, int) and saved_cl > 0:
            _consecutive_losses = saved_cl
    except Exception:
        pass  # fresh start, no state to recover

    # TCPConnector with keepalive_timeout prevents stale pooled connections
    # from silently breaking API calls after many hours of uptime.
    connector = aiohttp.TCPConnector(keepalive_timeout=30, limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        # Non-BTC assets run in a separate background task so they aren't
        # gated by the BTC state machine's continue/sleep cycle.
        asyncio.create_task(_non_btc_asset_loop(session))

        while True:
            try:
                midnight_reset()


                # Fresh config read
                try:
                    config = read_config()
                except Exception as exc:
                    log.error(f"Config read error: {exc}")
                    await asyncio.sleep(10)
                    continue

                if not config.get("bot_enabled", False):
                    await write_state_file(config, current_market, "PAUSED", 0,
                                           get_btc_price(), last_confidence_score,
                                           last_confidence_breakdown, last_action, last_skip_reason)
                    await asyncio.sleep(10)
                    continue

                if "BTC" not in config.get("enabled_assets", []):
                    await write_state_file(config, None, "DONE", 0,
                                           get_btc_price(), 0, {}, "btc_disabled", "")
                    await asyncio.sleep(10)
                    continue

                btc_price = get_btc_price()
                if btc_price is None:
                    log.warning("Waiting for BTC price...")
                    await write_state_file(config, current_market, current_phase, 0,
                                           None, last_confidence_score,
                                           last_confidence_breakdown, last_action, last_skip_reason)
                    await asyncio.sleep(10)
                    continue
                _btc_age = _am_price_age("BTC")
                if _btc_age is not None and _btc_age > 60:
                    age = int(_btc_age)
                    log.warning(f"BTC price stale ({age}s old) — skipping cycle.")
                    await write_state_file(config, current_market, current_phase, 0,
                                           btc_price, last_confidence_score,
                                           last_confidence_breakdown, "skip", f"btc_stale_{age}s")
                    await asyncio.sleep(10)
                    continue

                # Fetch active market
                try:
                    market = await fetch_current_market(session)
                except Exception as exc:
                    log.error(f"Market fetch error: {exc}")
                    await asyncio.sleep(10)
                    continue

                if market is None:
                    # If we have an open position with a stored close time, still run the
                    # locked-phase handler so the trade can settle even when no new market
                    # is visible. Repeated warnings each cycle until close_time elapses
                    # are expected — not errors.
                    if current_phase == "LOCKED" and current_position is not None:
                        _close_time = current_position.get("market_close_time", "")
                        if not _close_time:
                            log.error("LOCKED position missing market_close_time — cannot safely settle without market. Skipping.")
                        else:
                            log.warning("No active BTC markets — still processing open LOCKED position.")
                            try:
                                await handle_locked_phase(session, btc_price, 0, config)
                            except Exception as exc:
                                log.error(f"LOCKED phase error (no market): {exc}", exc_info=True)
                        await write_state_file(config, None, current_phase, 0, btc_price,
                                               last_confidence_score, last_confidence_breakdown,
                                               last_action, last_skip_reason)
                    else:
                        log.warning("No active BTC markets found.")
                        await write_state_file(config, None, "DONE", 0, btc_price,
                                               last_confidence_score, last_confidence_breakdown,
                                               last_action, last_skip_reason)
                    await asyncio.sleep(10)
                    continue

                current_market = market
                ticker = market.get("ticker", "")

                # Detect new market (different ticker)
                if prev_ticker is None:
                    prev_ticker = ticker
                    if current_phase == "DONE":
                        current_phase = "WATCH"
                        log.info(f"First market: {ticker}. Starting WATCH.")
                elif ticker != prev_ticker:
                    if current_phase == "LOCKED":
                        # Never reset a live position when the market rolls over.
                        # The position is on the OLD ticker — keep monitoring it.
                        log.info(f"Market rolled to {ticker} but position still open on {prev_ticker} — staying LOCKED.")
                        # Keep using the old market object for SL monitoring this cycle
                        ticker = prev_ticker
                        market = _market_cache if _market_cache and _market_cache.get("ticker") == prev_ticker else market
                    else:
                        log.info(f"New market: {ticker} (was {prev_ticker}). Resetting to WATCH.")
                        if prev_ticker in _s1_pending_trades:
                            asyncio.create_task(_try_settle_orphaned_s1(session, prev_ticker, btc_price, config, "BTC"))
                        current_phase = "WATCH"
                        current_position = None
                        _order_attempted_tickers.discard(prev_ticker)
                        prev_ticker = ticker

                secs_left = seconds_remaining(market)
                elapsed = seconds_elapsed(market)

                # Parse strike
                try:
                    strike = parse_strike(market)
                except Exception as exc:
                    log.error(f"Strike parse exception: {exc}")
                    strike = None

                if strike is None:
                    yes_sub = market.get("yes_sub_title") or ""
                    if "TBD" in yes_sub:
                        strike = _am_get_price("BTC")
                        if strike:
                            log.info(f"{ticker}: strike TBD — using live price {strike:.2f}")
                        else:
                            log.warning(f"{ticker}: cannot parse strike. Skipping cycle.")
                            await asyncio.sleep(10)
                            continue
                    else:
                        log.warning(f"{ticker}: cannot parse strike. Skipping cycle.")
                        await asyncio.sleep(10)
                        continue

                # â”€â”€ WATCH â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                if current_phase == "WATCH":
                    if elapsed > WATCH_PHASE_SECONDS:
                        log.info(f"{ticker}: elapsed {elapsed:.0f}s → READY.")
                        current_phase = "READY"
                    else:
                        log.info(f"{ticker}: WATCH ({elapsed:.0f}s elapsed).")
                        await _log_entry(market, "WATCH", secs_left, btc_price, strike,
                                         None, 0, "skip", f"WATCH phase, {elapsed:.0f}s elapsed",
                                         config.get("mode", "paper"))
                        await write_state_file(config, market, current_phase, secs_left,
                                               btc_price, 0, {}, "watch", "")
                        await asyncio.sleep(10)
                        continue

                # â”€â”€ LOCKED â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                if current_phase == "LOCKED":
                    try:
                        await handle_locked_phase(
                            session, btc_price, secs_left, config
                        )
                    except Exception as exc:
                        log.error(f"LOCKED phase error: {exc}", exc_info=True)
                    await write_state_file(config, market, current_phase, secs_left, btc_price,
                                           last_confidence_score, last_confidence_breakdown,
                                           last_action, last_skip_reason)
                    await asyncio.sleep(10)
                    continue

                # â”€â”€ DONE â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                if current_phase == "DONE":
                    # Re-enter READY only if no order was attempted for this ticker.
                    # This prevents duplicate orders when fill_confirmed=False but
                    # the order actually went through on Kalshi.
                    if secs_left > 3 * 60 and ticker not in _order_attempted_tickers:
                        log.info(
                            f"DONE → READY re-entry: {ticker} has {secs_left:.0f}s left."
                        )
                        current_phase = "READY"
                        # Fall through to READY handler below
                    else:
                        log.info(f"DONE phase. {secs_left:.0f}s left — waiting for next market.")
                        await write_state_file(config, market, current_phase, secs_left, btc_price,
                                               last_confidence_score, last_confidence_breakdown,
                                               last_action, last_skip_reason)
                        await asyncio.sleep(10)
                        continue

                # â”€â”€ READY â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                if current_phase == "READY":
                    try:
                        await handle_ready_phase(
                            session, config, market, ticker,
                            btc_price, secs_left, strike, elapsed
                        )
                    except Exception as exc:
                        log.error(f"READY phase error: {exc}", exc_info=True)

                await write_state_file(config, market, current_phase, secs_left, btc_price,
                                       last_confidence_score, last_confidence_breakdown,
                                       last_action, last_skip_reason)

                if current_phase == "READY":
                    await asyncio.sleep(5)
                    continue

            except Exception as exc:
                log.error(f"Main loop unhandled error: {exc}", exc_info=True)

            await asyncio.sleep(10)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Entry point
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

async def verify_kalshi_connection(session: aiohttp.ClientSession) -> None:
    """Verify Kalshi credentials work and log all available BTC market series."""
    # Auth check via /portfolio/balance — avoids market query-exchange service entirely
    balance_path = "/portfolio/balance"
    try:
        async with session.get(
            KALSHI_BASE_URL + balance_path,
            headers=kalshi_headers("GET", balance_path),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json()
            if resp.status == 401:
                log.error("KALSHI AUTH FAILED (401) — check KALSHI_API_KEY and KALSHI_PRIVATE_KEY")
                sys.exit(1)
            if resp.status != 200:
                log.error(f"Kalshi connection check failed: HTTP {resp.status} — {data}")
                sys.exit(1)
            balance = data.get("balance", "?")
            log.info(f"Kalshi auth OK. Account balance: {balance} cents")
    except SystemExit:
        raise
    except Exception as exc:
        log.error(f"Kalshi connection check failed: {exc}")
        sys.exit(1)

    path = "/markets"

    # â”€â”€ Market discovery: log everything BTC-related so we can find the right ticker â”€â”€
    now_utc = datetime.now(timezone.utc)
    log.info("=== KALSHI MARKET DISCOVERY START ===")

    # 1. Try every known series ticker (KXBTCD / BTCD-B first — the active "above/below" BTC markets)
    for series in ("KXBTCD", "BTCD-B", "KXBTC15M", "KXBTC", "BTC15M", "BTC", "BTCUSD", "KXBTCUSD", "KXBTCUSD15M"):
        try:
            async with session.get(
                KALSHI_BASE_URL + path,
                headers=kalshi_headers("GET", path),
                params={"series_ticker": series, "status": "open", "limit": 20},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                d = await resp.json()
                markets = d.get("markets", [])
                log.info(f"  series={series!r} -> {len(markets)} markets")
                for m in markets[:5]:
                    try:
                        close_dt = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
                        mins_left = (close_dt - now_utc).total_seconds() / 60
                        open_dt   = datetime.fromisoformat(m.get("open_time","").replace("Z", "+00:00"))
                        duration  = (close_dt - open_dt).total_seconds() / 60
                    except Exception:
                        mins_left = duration = -1
                    log.info(f"    ticker={m.get('ticker')} closes_in={mins_left:.1f}m dur={duration:.0f}m title={m.get('title','')[:60]}")
        except Exception as exc:
            log.info(f"  series={series!r} -> ERROR: {exc}")

    # 2. Broad scan (avoids Kalshi 500 on filterless queries)
    for scan_series in ("KXBTCD", "BTCD-B", "KXBTC", "KXBTC15M", "BTC"):
        try:
            async with session.get(
                KALSHI_BASE_URL + path,
                headers=kalshi_headers("GET", path),
                params={"status": "open", "series_ticker": scan_series, "limit": 100},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    log.info(f"  scan series={scan_series!r} -> HTTP {resp.status}")
                    continue
                d = await resp.json()
                all_short = d.get("markets", [])
            log.info(f"  scan series={scan_series!r} -> {len(all_short)} markets")
            for m in all_short[:10]:
                try:
                    close_dt = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
                    mins_left = (close_dt - now_utc).total_seconds() / 60
                    open_dt   = datetime.fromisoformat(m.get("open_time","").replace("Z", "+00:00"))
                    duration  = (close_dt - open_dt).total_seconds() / 60
                except Exception:
                    mins_left = duration = -1
                log.info(f"    ticker={m.get('ticker')} closes_in={mins_left:.1f}m dur={duration:.0f}m title={m.get('title','')[:60]}")
        except Exception as exc:
            log.info(f"  scan series={scan_series!r} ERROR: {exc}")

    # 3. /series endpoint — find any BTC-related series
    try:
        async with session.get(
            KALSHI_BASE_URL + "/series",
            headers=kalshi_headers("GET", "/series"),
            params={"limit": 200},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                d = await resp.json()
                all_series = d.get("series", [])
                btc_series = [s for s in all_series
                              if "btc" in s.get("ticker","").lower()
                              or "bitcoin" in s.get("title","").lower()
                              or "btc" in s.get("title","").lower()]
                log.info(f"  /series: {len(all_series)} total, {len(btc_series)} BTC-related")
                for s in btc_series:
                    log.info(f"    series_ticker={s.get('ticker')} title={s.get('title','')[:60]}")
            else:
                log.info(f"  /series returned HTTP {resp.status}")
    except Exception as exc:
        log.info(f"  /series ERROR: {exc}")

    log.info("=== KALSHI MARKET DISCOVERY END ===")


async def run_preflight_checks(config: dict) -> None:
    """
    Runs before first trade. Prints warnings and blocks live trading
    if critical validations haven't been completed.

    LIVE mode + unresolved issues  → sys.exit(1). Hard stop.
    PAPER mode + unresolved issues → warn and continue (bot must run to collect data).
    preflight_override: true in config.json  → skip the live-mode block (NOT RECOMMENDED).
    """
    issues: list[str] = []
    W = 60

    # â”€â”€ Check 1: Price validation data â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if not os.path.isfile(_PRICE_VAL_CSV):
        issues.append(
            "NO PRICE VALIDATION DATA — price_validation_log.csv does not exist. "
            "Run paper mode for 200+ cycles first."
        )
    else:
        try:
            with open(_PRICE_VAL_CSV, encoding="utf-8") as _f:
                row_count = max(0, sum(1 for _ in _f) - 1)   # minus header
        except Exception:
            row_count = 0
        if row_count < 200:
            issues.append(
                f"INSUFFICIENT PRICE VALIDATION — only {row_count}/200 samples collected. "
                "Keep running paper mode."
            )

    # â”€â”€ Check 2: Fee constant is set and > 0 â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    fee = config.get("kalshi_fee_per_contract_cents", 0)
    if not (isinstance(fee, (int, float)) and fee > 0):
        issues.append(
            f"FEE NOT CONFIGURED — kalshi_fee_per_contract_cents={fee!r}. "
            "Set to 7 (Kalshi charges 7c/contract)."
        )

    # â”€â”€ Check 3: Daily loss limit is real â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    dll = config.get("daily_loss_limit_dollars", 999999)
    if dll > 500:
        issues.append(
            f"DAILY LOSS LIMIT TOO HIGH — currently ${dll}. "
            "Set to a realistic value (e.g. $50)."
        )

    # â”€â”€ Check 4: mode gate â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    mode      = config.get("mode", "paper")
    is_live   = mode == "live"   # demo uses simulated funds — only block for real-money live
    override  = bool(config.get("preflight_override", False))

    if is_live and issues:
        print("=" * W)
        print("LIVE TRADING BLOCKED — PRE-FLIGHT CHECK FAILED")
        print("=" * W)
        for issue in issues:
            print(f"  [FAIL] {issue}")
        print()
        print("Switch to paper mode or resolve these issues before trading live.")
        print("To override (NOT RECOMMENDED): set preflight_override: true in config.json")
        print("=" * W)
        await send_telegram(
            "<b>LIVE TRADING BLOCKED — pre-flight failed</b>\n"
            + "\n".join(f"- {i}" for i in issues)
            + "\nResolve all issues before retrying live mode."
        )
        if not override:
            sys.exit(2)
        else:
            log.warning("PRE-FLIGHT OVERRIDE ACTIVE — proceeding into live mode despite failures. "
                        "This is NOT recommended.")
            print()
            print("  *** OVERRIDE ACTIVE — LIVE MODE STARTING ANYWAY ***")
            print("  *** THIS IS NOT RECOMMENDED. YOU WERE WARNED.   ***")
            print("=" * W)

    elif issues:
        # Paper mode — warn but continue; the bot must run to collect validation data.
        print("=" * W)
        print("PRE-FLIGHT WARNINGS (paper mode — not blocking)")
        print("=" * W)
        for issue in issues:
            print(f"  [WARN] {issue}")
        print("=" * W)

    else:
        log.info("=" * W)
        log.info(f"  Pre-flight: PASS — {mode.upper()} mode.  "
                 f"fee={fee}c  dll=${dll}  "
                 f"reversal={'ON' if config.get('enable_reversal_signal') else 'OFF'}")
        log.info("=" * W)


async def main() -> None:
    """Bootstrap: load credentials, init DB, start BTC feed, run main loop."""
    _init_config()
    load_credentials(mode=read_config().get("mode", "paper"))
    init_db()
    test_db_write()

    # Clean up zombie "pending" trades from prior crashed sessions.
    # Any trade still pending after 30+ minutes never settled — mark it as expired.
    try:
        conn = sqlite3.connect(_DB_FILE)
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


    # Verify Kalshi credentials and log account balance before doing anything.
    # Skipped in paper mode — no real credentials are loaded there.
    if read_config().get("mode", "paper") != "paper":
        async with aiohttp.ClientSession() as verify_session:
            await verify_kalshi_connection(verify_session)

    # Start Coinbase price feed for all assets
    _startup_config = read_config()
    _enabled = _startup_config.get("enabled_assets", ["ETH", "SOL", "XRP"])
    # Always subscribe BTC regardless of enabled_assets — other strategies use
    # btc_prices_60m for correlation signals and the deque must stay populated.
    _feed_assets = list(dict.fromkeys(["BTC"] + _enabled))
    from asset_manager import seed_price_history
    await seed_price_history(_feed_assets)
    asyncio.create_task(coinbase_price_task(_feed_assets))

    # Wait for the first price from any enabled asset (timeout after 120s so Railway doesn't hang)
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
        log.warning("Price feed not available after 120s — continuing anyway; prices will populate shortly.")
    else:
        log.info(f"Price feed ready after {waited}s. {_first_asset}: ${_first_price:,.2f}")
    _startup_cfg = read_config()
    _btc_display = f"${get_btc_price():,.2f}" if get_btc_price() is not None else f"{_first_asset}: ${_first_price:,.2f}" if _first_price else "price N/A"
    await send_telegram(f"<b>Printer bot started</b>\n{_btc_display}\nMode: {_startup_cfg.get('mode','?').upper()}  |  Bot enabled: {_startup_cfg.get('bot_enabled', False)}")

    # Pre-flight check runs once before trading begins.
    # LIVE mode with unresolved issues → sys.exit(1). Paper mode → warn and continue.
    await run_preflight_checks(_startup_cfg)

    await main_loop()


if __name__ == "__main__":
    asyncio.run(main())


