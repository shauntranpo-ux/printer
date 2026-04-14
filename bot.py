"""
bot.py — Core trading logic for the Kalshi BTC 15-minute prediction market bot.

Connects to Coinbase WebSocket for live BTC prices, polls Kalshi for the
soonest-expiring BTC 15-minute market, evaluates a four-component confidence
score, places paper or live orders, and enforces daily
loss / profit limits. Writes bot_state.json every cycle for server.py to read.

Start via runner.py, not directly.
"""

import asyncio
import json
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

import aiohttp
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

try:
    import anthropic as _anthropic_module
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

# ─────────────────────────── logging ──────────────────────────────────────────
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

# ─────────────────────────── constants ────────────────────────────────────────
KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_PATH_PREFIX = "/trade-api/v2"  # included in signature but not in the path arg
COINBASE_WS = "wss://advanced-trade-ws.coinbase.com"
API_TIMEOUT = 10          # seconds for every Kalshi HTTP call
MARKET_CACHE_TTL = 30     # seconds to cache the active market
WATCH_PHASE_SECONDS = 30   # wait 30s into each 15-min session before evaluating

# Kalshi platform fee: ~7c per $1 contract, subtracted from gross EV.
# Without this, every EV calculation was 7% optimistic — trades that looked
# +5% edge were actually -2% after fees.
KALSHI_FEE = 0.07


# ── Telegram notifications (optional — set env vars to enable) ────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "")

# ─────────────────────────── file paths (env-overridable for multi-strategy) ──
_CONFIG_FILE = os.environ.get("BOT_CONFIG_FILE", "config.json")
_DB_FILE     = os.environ.get("BOT_DB_FILE",     "kalshi_bot.db")
_STATE_FILE  = os.environ.get("BOT_STATE_FILE",  "bot_state.json")

# ─────────────────────────── global state ─────────────────────────────────────
btc_prices: deque = deque(maxlen=500)   # (unix_ts, price_float)

private_key = None    # loaded on startup
api_key: str = ""

# Market / phase
current_market: dict | None = None
current_phase: str = "DONE"   # WATCH | READY | LOCKED | DONE
current_position: dict | None = None
_order_attempted_tickers: set = set()  # tickers where an order was attempted this session

# Market cache
_market_cache: dict | None = None
_market_cache_ts: float = 0.0
_all_markets_cache: list = []
_all_markets_cache_ts: float = 0.0

# Live BV3 correction table — tracks actual win rates per (dist_idx, time_idx) bucket
# Updated after every resolved trade. Blended into _empirical_win_prob().
_bv3_corrections: dict = {}  # (dist_idx, time_idx) -> [wins, total]
_BV3_CORRECTIONS_FILE = "bv3_corrections.json"

# Daily-limit tracking
limit_triggered: bool = False
limit_reason: str = ""
pre_limit_mode: str | None = None
daily_reset_date = None

# Price-validator CSV — counts rows collected and running totals for summary
_PRICE_VAL_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "price_validation_log.csv")
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
last_reversal_reason: str = ""   # most recent reversal signal evaluation (pass/fail)

# Contract price history — tracks YES ask over time per ticker for velocity signal
_contract_price_history: dict = {}   # ticker → deque[(ts, price)]

# Claude AI client
_claude_client = None       # AsyncAnthropic instance, set in main()
_claude_cache: dict = {}    # cache_key → {ts, result}
CLAUDE_CACHE_TTL = 15       # seconds between Claude calls for same market+side

# Real-time market context cache (Fear & Greed, news headlines, social signals)
_market_context: dict = {"fear_greed": None, "news": []}
_market_context_ts: float = 0.0
_MARKET_CONTEXT_TTL: int = 120   # refresh every 2 minutes

# Claude's last analysis (written to state file for dashboard)
last_claude_reasoning: str = ""
last_claude_key_signals: list = []

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

# Consecutive-loss circuit breaker
_consecutive_losses: int = 0
_consecutive_loss_pause_until: float | None = None  # unix ts; None = not paused


# ══════════════════════════════════════════════════════════════════════════════
#  Atomic JSON write utility
# ══════════════════════════════════════════════════════════════════════════════

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


# ══════════════════════════════════════════════════════════════════════════════
#  Price-validator CSV logger
# ══════════════════════════════════════════════════════════════════════════════

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
    spread = 4.5  # midpoint of backtest's 3.0–6.0 spread

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


# ══════════════════════════════════════════════════════════════════════════════
#  Config helpers
# ══════════════════════════════════════════════════════════════════════════════

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
        "confidence_threshold": 72,
        "daily_loss_limit_dollars": 50,          # 2× trade size — real guard, not $5M decoration
        "daily_profit_target_dollars": 200,
        "max_consecutive_losses": 5,             # pause 15 min after this many losses in a row
        "enable_reversal_signal": False,         # disabled by default — no backtested evidence yet
        "enable_claude_filter": False,           # disabled by default — LLM adds latency/cost, no proven edge
        "use_fixed_sizing": False,               # when True, bypasses Kelly and uses fixed trade_amount
        "kelly_cap": 0.25,                       # quarter-Kelly cap; Kelly fraction never exceeds this
        "min_ev_base": 8,                        # raised from 3 to 8 after adding fee accounting
        "kalshi_fee_per_contract_cents": 7,      # Kalshi platform fee; update if pricing changes
        "preflight_override": False,             # set true ONLY to bypass pre-flight hard stop — not recommended
        # claude_enabled: config key unused — Claude activates automatically via ANTHROPIC_API_KEY
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

    # Railway env var overrides — set once, persist forever
    if "BOT_MODE" in os.environ:
        cfg["mode"] = os.environ["BOT_MODE"].strip().lower()
    if "BOT_ENABLED" in os.environ:
        cfg["bot_enabled"] = os.environ["BOT_ENABLED"].strip().lower() in ("1", "true", "yes")

    # Fill in any missing keys with defaults
    for k, v in defaults.items():
        cfg.setdefault(k, v)

    write_config(cfg)
    log.info(f"Config ready: mode={cfg['mode']} enabled={cfg['bot_enabled']}")


# ══════════════════════════════════════════════════════════════════════════════
#  Database
# ══════════════════════════════════════════════════════════════════════════════

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
                claude_confidence     INTEGER,
                claude_signals        TEXT
            )
        """)

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
            ("claude_confidence", "INTEGER"),
            ("claude_signals",    "TEXT"),
            ("order_id",          "TEXT"),
            ("asset",             "TEXT DEFAULT 'BTC'"),  # multi-asset support
        ):
            try:
                c.execute(f"ALTER TABLE trades ADD COLUMN {col} {typedef}")
            except Exception:
                pass  # column already exists

        conn.commit()
        conn.close()
        log.info("Database initialized.")
    except Exception as exc:
        log.error(f"DB init error: {exc}")
        raise


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
                    claude_confidence, claude_signals, order_id, asset
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                trade.get("claude_confidence"), trade.get("claude_signals"),
                trade.get("order_id"), trade.get("asset", "BTC"),
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


# ══════════════════════════════════════════════════════════════════════════════
#  Kalshi auth
# ══════════════════════════════════════════════════════════════════════════════

async def send_telegram(text: str) -> None:
    """Send a Telegram notification. Silent no-op if env vars are not set."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return  # silently skip — Telegram is optional
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with aiohttp.ClientSession() as tg:
            async with tg.post(
                url,
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    log.warning(f"Telegram notify failed: HTTP {resp.status} — {body}")
                else:
                    log.info(f"Telegram notification sent OK")
    except Exception as exc:
        log.warning(f"Telegram notify error: {exc}")


def load_credentials() -> None:
    """
    Load KALSHI_API_KEY and KALSHI_PRIVATE_KEY from environment variables.
    KALSHI_PRIVATE_KEY may be a PEM string or a path to a PEM file.
    Exits with a clear message if either variable is missing.
    """
    global api_key, private_key

    api_key = os.environ.get("KALSHI_API_KEY", "").strip()
    pem_val = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()

    if not api_key:
        print("ERROR: KALSHI_API_KEY environment variable is not set.")
        print("       Set it with: export KALSHI_API_KEY=your_api_key_here")
        sys.exit(1)

    if not pem_val:
        print("ERROR: KALSHI_PRIVATE_KEY environment variable is not set.")
        print("       Set it to your PEM string or a file path to your .pem file.")
        sys.exit(1)

    if os.path.exists(pem_val):
        with open(pem_val, "rb") as fh:
            pem_bytes = fh.read()
        log.info(f"Loaded private key from file: {pem_val}")
    else:
        pem_bytes = pem_val.encode()
        log.info("Loaded private key from environment variable string.")

    private_key = serialization.load_pem_private_key(pem_bytes, password=None)
    log.info("Credentials loaded successfully.")


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


# ══════════════════════════════════════════════════════════════════════════════
#  BTC price feed
# ══════════════════════════════════════════════════════════════════════════════

async def btc_feed_task() -> None:
    """
    Connect to the Coinbase Advanced Trade WebSocket and maintain a rolling
    deque of the last 500 (timestamp, price) tuples for BTC-USD.
    Reconnects automatically on any error.
    """
    while True:
        try:
            async with websockets.connect(COINBASE_WS) as ws:
                await ws.send(json.dumps({
                    "type": "subscribe",
                    "channel": "ticker",
                    "product_ids": ["BTC-USD"],
                }))
                log.info("Connected to Coinbase BTC feed.")
                async for raw in ws:
                    try:
                        data = json.loads(raw)
                        for event in data.get("events", []):
                            for ticker in event.get("tickers", []):
                                price = float(ticker["price"])
                                now_ts = time.time()
                                if not btc_prices or (now_ts - btc_prices[-1][0]) >= 5.0:
                                    btc_prices.append((now_ts, price))
                    except Exception as parse_exc:
                        log.debug(f"BTC feed parse error: {parse_exc}")
        except Exception as exc:
            log.error(f"BTC feed disconnected: {exc}. Reconnecting in 3s...")
            await asyncio.sleep(3)


def get_btc_price() -> float | None:
    """Return the most recent BTC price, or None if no data received yet."""
    if not btc_prices:
        return None
    return btc_prices[-1][1]


# ══════════════════════════════════════════════════════════════════════════════
#  Market fetching
# ══════════════════════════════════════════════════════════════════════════════

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
    # Priority order: KXBTCD/BTCD-B are the active "Above/below" short-duration BTC markets.
    # KXBTC15M is legacy (no active markets as of 2026). KXBTC returns 25-hour daily range
    # markets that get filtered out — still included as fallback.
    _SERIES_SEARCH_ORDER = ("KXBTCD", "BTCD-B", "KXBTC15M", "BTC15M", "KXBTC", "BTC")
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

    # Accept any short-duration market: 1-60 minutes (covers 5-min, 15-min, 30-min, hourly)
    short_dur = [m for m in all_markets
                 if (lambda d: d is not None and 1 <= d <= 75)(market_duration_minutes(m))]

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
                     f"(duration ≤ {min_dur + 5:.0f}m, dropping {len(pool) - len(focused)} longer-duration markets)")
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


# ══════════════════════════════════════════════════════════════════════════════
#  Strike parsing
# ══════════════════════════════════════════════════════════════════════════════

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

    text = market.get("title", "") + " " + market.get("subtitle", "")
    match = re.search(r"\$(\d{1,3}(?:,\d{3})*(?:\.\d+)?)", text)
    if match:
        strike = float(match.group(1).replace(",", ""))
        log.info(f"Strike parsed from title regex: {strike} (text: {text[:80]})")
        return strike

    log.warning(f"Cannot parse strike. Full market fields: { {k: market.get(k) for k in ('ticker','title','subtitle','floor_strike','cap_strike','strike_price','result','yes_sub_title','no_sub_title')} }")
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  Timing helpers
# ══════════════════════════════════════════════════════════════════════════════

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


# ══════════════════════════════════════════════════════════════════════════════
#  Orderbook fetching
# ══════════════════════════════════════════════════════════════════════════════

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
            return int(round(float(val) * 100))
        except (TypeError, ValueError):
            return None

    # ── Step 1: try the orderbook endpoint (populated for limit-order markets)
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
    yes_asks = [(p, q) for p, q in yes_arr if q > 0]
    no_asks  = [(p, q) for p, q in no_arr  if q > 0]

    best_yes_ask = min(p for p, _ in yes_asks) if yes_asks else None
    best_no_ask  = min(p for p, _ in no_asks)  if no_asks  else None
    best_yes_bid = (100 - max(p for p, _ in no_asks)) if no_asks else None

    # ── Step 2: AMM fallback — fetch the individual market fresh (not cached).
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
                best_yes_ask = 100 - no_bid

        if best_no_ask is None:
            best_no_ask = _dollars_to_cents(src.get("no_ask_dollars"))
        if best_no_ask is None:
            yes_bid = _dollars_to_cents(src.get("yes_bid_dollars"))
            if yes_bid is not None:
                best_no_ask = 100 - yes_bid

        if best_yes_bid is None:
            best_yes_bid = _dollars_to_cents(src.get("yes_bid_dollars"))
        if best_yes_bid is None:
            no_ask_raw = _dollars_to_cents(src.get("no_ask_dollars"))
            if no_ask_raw is not None:
                best_yes_bid = 100 - no_ask_raw

        if best_yes_ask is not None or best_no_ask is not None:
            log.info(
                f"AMM prices for {ticker}: "
                f"yes_ask={best_yes_ask}¢  no_ask={best_no_ask}¢  yes_bid={best_yes_bid}¢"
            )

    if best_yes_ask is None or best_no_ask is None:
        log.warning(f"No price data available for {ticker} (yes_ask={best_yes_ask} no_ask={best_no_ask})")
        return None

    # Sanity check — reject prices that are clearly wrong
    if not (1 <= best_yes_ask <= 99 and 1 <= best_no_ask <= 100):
        log.error(
            f"Orderbook sanity FAIL for {ticker}: "
            f"yes_ask={best_yes_ask}c no_ask={best_no_ask}c — out of valid range, skipping"
        )
        return None
    if best_yes_ask + best_no_ask < 100:
        # Sum below 100 is impossible (would allow riskless arbitrage)
        log.error(
            f"Orderbook sanity FAIL for {ticker}: "
            f"yes_ask({best_yes_ask}c) + no_ask({best_no_ask}c) = {best_yes_ask+best_no_ask}c < 100, skipping"
        )
        return None
    if best_yes_ask + best_no_ask > 115:
        # Spread > 15c is extreme — likely stale data
        log.error(
            f"Orderbook sanity FAIL for {ticker}: "
            f"yes_ask({best_yes_ask}c) + no_ask({best_no_ask}c) = {best_yes_ask+best_no_ask}c > 115, skipping"
        )
        return None

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


# ══════════════════════════════════════════════════════════════════════════════
#  BTC position vs strike
# ══════════════════════════════════════════════════════════════════════════════

def btc_position(btc_price: float, strike: float) -> str:
    """
    Classify BTC price relative to the strike.

    Returns:
        'clearly_above', 'clearly_below', or 'at_strike'.
    """
    pct = (btc_price - strike) / strike
    if pct > 0.010:
        return "clearly_above"
    if pct < -0.010:
        return "clearly_below"
    return "at_strike"


# ══════════════════════════════════════════════════════════════════════════════
#  Momentum
# ══════════════════════════════════════════════════════════════════════════════

def calculate_momentum() -> tuple[float, str]:
    """
    Calculate BTC price momentum over the last 180 seconds.

    Returns:
        (pct_change, label) where label is 'bullish', 'bearish', or 'neutral'.
    """
    if not btc_prices:
        return 0.0, "neutral"

    cutoff = time.time() - 180
    oldest = None
    for ts, price in btc_prices:
        if ts >= cutoff:
            oldest = price
            break

    if oldest is None:
        return 0.0, "neutral"

    current = btc_prices[-1][1]
    pct = (current - oldest) / oldest

    if pct > 0.005:
        return pct, "bullish"
    if pct < -0.005:
        return pct, "bearish"
    return pct, "neutral"


def btc_realized_vol() -> float | None:
    """
    Realized BTC volatility: std dev of 1-minute percentage returns over the
    last 10 minutes. Returns None if fewer than 4 samples are available.

    Used to gate entries — if BTC is moving fast enough to plausibly cross the
    strike before expiry, the empirical win-prob table understates the risk.
    """
    if len(btc_prices) < 2:
        return None
    now = time.time()
    samples: list[float] = []
    for minutes_ago in range(10, 0, -1):
        target = now - minutes_ago * 60
        best_price: float | None = None
        best_delta = float("inf")
        for ts, price in btc_prices:
            delta = abs(ts - target)
            if delta < best_delta and delta < 45:
                best_delta = delta
                best_price = price
        if best_price is not None:
            samples.append(best_price)
    if len(samples) < 4:
        return None
    returns = [(samples[i] - samples[i - 1]) / samples[i - 1] for i in range(1, len(samples))]
    if len(returns) < 3:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return variance ** 0.5  # std dev of 1-min returns (as decimal)


# ══════════════════════════════════════════════════════════════════════════════
#  Contract price velocity tracking
# ══════════════════════════════════════════════════════════════════════════════

def track_contract_price(ticker: str, price: float) -> None:
    """Record the latest contract ask price for velocity analysis."""
    if ticker not in _contract_price_history:
        _contract_price_history[ticker] = deque(maxlen=30)
    _contract_price_history[ticker].append((time.time(), price))


def contract_velocity(ticker: str, side: str) -> str:
    """
    Determine whether the contract price is moving in a favorable direction.

    For YES buys: YES ask price falling toward entry zone = opportunity.
    For NO  buys: YES ask price rising away from us = NO getting cheaper = opportunity.

    Returns 'favorable', 'neutral', or 'unfavorable'.
    """
    hist = _contract_price_history.get(ticker)
    if not hist or len(hist) < 3:
        return "neutral"

    prices = [p for _, p in hist]
    oldest, newest = prices[0], prices[-1]
    change = (newest - oldest) / oldest if oldest else 0

    if side == "yes":
        # YES ask falling toward us (e.g. 91¢ → 65¢) = great opportunity
        if change < -0.05:  return "favorable"
        if change > 0.05:   return "unfavorable"
    else:
        # YES ask rising (NO getting cheaper) = opportunity to buy NO
        if change > 0.05:   return "favorable"
        if change < -0.05:  return "unfavorable"
    return "neutral"


# ══════════════════════════════════════════════════════════════════════════════
#  Adaptive calibration from trade history
# ══════════════════════════════════════════════════════════════════════════════

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

        # ── 1. Probability scale factor ───────────────────────────────────────
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

        # ── 2. Win-rate reward tiers (priority = win rate, not profit) ───────────
        #   Tier 3 (≥85%) — MAX reward: lowest edge bar, biggest confidence bonus
        #   Tier 2 (≥75%) — HUGE reward: very low bar, large bonus
        #   Tier 1 (≥50%) — reward: lower bar, small bonus
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

        # ── 3. Directional performance ────────────────────────────────────────
        yes_rows = [r for r in rows if r[0] == "yes"]
        no_rows  = [r for r in rows if r[0] == "no"]
        if len(yes_rows) >= 10:
            _brain_cal["bullish_wr"] = sum(1 for r in yes_rows if r[5] == "win") / len(yes_rows)
        if len(no_rows) >= 10:
            _brain_cal["bearish_wr"] = sum(1 for r in no_rows  if r[5] == "win") / len(no_rows)

        # ── 5. Learn from Claude's stored decisions ───────────────────────────
        async with aiosqlite.connect(_DB_FILE) as _db2:
            await _db2.execute("PRAGMA journal_mode=WAL")
            async with _db2.execute("""
                SELECT claude_confidence, outcome FROM trades
                WHERE outcome IN ('win','loss') AND claude_confidence IS NOT NULL
                ORDER BY ts DESC LIMIT 50
            """) as _cur2:
                claude_rows = await _cur2.fetchall()

        if len(claude_rows) >= 5:
            # Claude's avg confidence on wins vs losses
            c_wins  = [r[0] for r in claude_rows if r[1] == "win"]
            c_losses= [r[0] for r in claude_rows if r[1] == "loss"]
            if c_wins and c_losses:
                avg_c_win  = sum(c_wins)  / len(c_wins)
                avg_c_loss = sum(c_losses)/ len(c_losses)
                # If Claude's confidence on wins is meaningfully higher than on losses,
                # its probability estimates were directionally correct → trust its scale
                if avg_c_win > avg_c_loss + 10:
                    claude_implied_scale = (avg_c_win / 100) / max(overall_wr, 0.01)
                    # Blend very gently — same conservative rate as the main calibration
                    _brain_cal["prob_scale"] = (
                        0.95 * _brain_cal["prob_scale"] +
                        0.05 * claude_implied_scale
                    )
                    _brain_cal["prob_scale"] = max(0.85, min(1.5, _brain_cal["prob_scale"]))
                    brain_log.info(
                        f"[CLAUDE->BRAIN] Absorbed {len(claude_rows)} Claude decisions. "
                        f"avg_conf wins={avg_c_win:.0f} losses={avg_c_loss:.0f} | "
                        f"prob_scale={_brain_cal['prob_scale']:.2f}"
                    )
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


# ══════════════════════════════════════════════════════════════════════════════
#  Claude AI — primary decision engine
# ══════════════════════════════════════════════════════════════════════════════

def _init_claude() -> None:
    """Initialise the AsyncAnthropic client if the package and API key are present."""
    global _claude_client
    if not _ANTHROPIC_AVAILABLE:
        log.warning("anthropic package not installed — Claude AI disabled (falling back to rule-based).")
        return
    ak = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not ak:
        log.warning("ANTHROPIC_API_KEY not set — Claude AI disabled (falling back to rule-based).")
        return
    _claude_client = _anthropic_module.AsyncAnthropic(api_key=ak)
    log.info("Claude AI client ready (claude-haiku-4-5 — borderline filter mode).")


async def _recent_trades_for_claude() -> list:
    """Return the last 15 completed trades as plain dicts for the Claude prompt."""
    try:
        async with aiosqlite.connect(_DB_FILE) as conn:
            async with conn.execute("""
                SELECT ts, side, entry_price_cents, seconds_left_at_entry,
                       btc_price_at_entry, strike, outcome, pnl_dollars, confidence_score
                FROM trades WHERE outcome IN ('win','loss')
                ORDER BY ts DESC LIMIT 15
            """) as cursor:
                rows = await cursor.fetchall()
        return [
            {
                "ts": r[0], "side": r[1], "entry_cents": r[2],
                "secs_left": r[3], "btc": r[4], "strike": r[5],
                "outcome": r[6], "pnl": r[7], "score": r[8],
            }
            for r in rows
        ]
    except Exception:
        return []


async def _fetch_market_context(session: aiohttp.ClientSession) -> dict:
    """
    Pull real-time BTC market sentiment from free public APIs.

    Sources (always, no auth required):
      - alternative.me Fear & Greed Index  — 0 (extreme fear) to 100 (extreme greed)
      - CryptoPanic BTC news               — latest 5 headlines

    Sources (optional, env-var gated):
      - NewsAPI.org keyword search          — NEWSAPI_KEY (100 req/day free tier)

    Returns cached result if < _MARKET_CONTEXT_TTL seconds old.
    """
    global _market_context, _market_context_ts

    now = time.time()
    if now - _market_context_ts < _MARKET_CONTEXT_TTL:
        return _market_context

    # Single-coroutine path (main_loop is sequential) — no lock needed.
    # If the call graph ever becomes concurrent, add an asyncio.Lock here.
    ctx: dict = {"fear_greed": None, "news": []}
    timeout = aiohttp.ClientTimeout(total=6)

    # ── Fear & Greed Index ──────────────────────────────────────────────────
    try:
        async with session.get(
            "https://api.alternative.me/fng/?limit=1",
            timeout=timeout,
        ) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                entry = data["data"][0]
                ctx["fear_greed"] = {
                    "value": int(entry["value"]),
                    "label": entry["value_classification"],
                }
    except Exception as exc:
        log.debug(f"[ctx] Fear&Greed fetch failed: {exc}")

    # ── CryptoPanic BTC news (requires free API key at cryptopanic.com) ─────
    cryptopanic_key = os.environ.get("CRYPTOPANIC_KEY", "")
    if cryptopanic_key:
        try:
            async with session.get(
                "https://cryptopanic.com/api/free/v1/posts/",
                params={"auth_token": cryptopanic_key, "currencies": "BTC", "public": "true"},
                timeout=timeout,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    for post in (data.get("results") or [])[:5]:
                        ctx["news"].append({
                            "title": post.get("title", ""),
                            "votes": post.get("votes", {}),
                        })
                else:
                    log.debug(f"[ctx] CryptoPanic returned HTTP {resp.status}")
        except Exception as exc:
            log.debug(f"[ctx] CryptoPanic fetch failed: {exc}")

    # ── NewsAPI.org (optional) ──────────────────────────────────────────────
    newsapi_key = os.environ.get("NEWSAPI_KEY", "")
    if newsapi_key and len(ctx["news"]) < 5:
        try:
            async with session.get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": "bitcoin OR crypto",
                    "sortBy": "publishedAt",
                    "pageSize": 5,
                    "language": "en",
                    "apiKey": newsapi_key,
                },
                timeout=timeout,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    for art in (data.get("articles") or [])[:5]:
                        ctx["news"].append({
                            "title": art.get("title", ""),
                            "source": art.get("source", {}).get("name", ""),
                        })
        except Exception as exc:
            log.debug(f"[ctx] NewsAPI fetch failed: {exc}")

    _market_context = ctx
    _market_context_ts = time.time()

    fg = ctx["fear_greed"]
    fg_desc = f"F&G={fg['value']}({fg['label']})" if fg else "F&G=n/a"
    log.info(
        f"[ctx] Market context updated — {fg_desc} news={len(ctx['news'])}"
    )
    return ctx


async def claude_analysis(
    session: aiohttp.ClientSession,
    btc_price: float,
    strike: float,
    yes_ask: float,
    no_ask: float,
    elapsed_seconds: float,
    secs_left: float,
    ticker: str,
) -> dict | None:
    """
    Ask Claude to pick a side AND decide whether to trade.
    Receives both YES and NO prices so it is never blocked by a direction gate.

    Returns dict: {action, side, confidence, reasoning, key_signals}
    Returns None if Claude is unavailable — caller falls back to rule-based.
    """
    global last_claude_reasoning, last_claude_key_signals

    if _claude_client is None:
        return None

    # Cache keyed on ticker + price bucket (refreshes every CLAUDE_CACHE_TTL s)
    cache_key = f"{ticker}:{int(yes_ask // 3) * 3}"
    cached = _claude_cache.get(cache_key)
    if cached and (time.time() - cached["ts"]) < CLAUDE_CACHE_TTL:
        r = cached["result"]
        last_claude_reasoning = r.get("reasoning", "")
        last_claude_key_signals = r.get("key_signals", [])
        return r

    # BTC price history
    now_ts = time.time()
    recent_btc = [(ts, p) for ts, p in btc_prices if ts >= now_ts - 300]
    btc_5m  = recent_btc[0][1]  if recent_btc else btc_price
    btc_1m  = next((p for ts, p in reversed(recent_btc) if ts <= now_ts - 60),  btc_price)
    btc_30s = next((p for ts, p in reversed(recent_btc) if ts <= now_ts - 30),  btc_price)

    mom_pct, mom_label = calculate_momentum()
    pct_from_strike = (btc_price - strike) / strike * 100
    recent_trades = await _recent_trades_for_claude()

    # Real-time market context (Fear & Greed, news, social signals)
    ctx = await _fetch_market_context(session)

    # Build a compact sentiment block for the prompt
    fg = ctx.get("fear_greed")
    fg_line = (
        f"Fear & Greed Index: {fg['value']}/100 — {fg['label']}"
        if fg else "Fear & Greed Index: unavailable"
    )

    news_lines = "\n".join(
        f"  • {n['title'][:120]}"
        for n in ctx.get("news", [])[:5]
    ) or "  (no recent headlines)"

    sentiment_block = f"""\nREAL-TIME MARKET SENTIMENT:
{fg_line}

RECENT CRYPTO NEWS:
{news_lines}"""

    system_prompt = (
        "You are a fast quantitative filter for a Kalshi BTC binary options bot. "
        "The primary model (Brain v3) has already evaluated this trade using empirical win probabilities from 4.5M rows of BTC data. "
        "You are being consulted because the trade is BORDERLINE — the expected value is between 2-5% or confidence is 60-84. "
        "You have access to real-time market sentiment: Fear & Greed Index and breaking crypto news. "
        "Your job: confirm or reject. Be decisive. Only reject if you see a clear red flag "
        "(momentum reversal, vol spike, price stalling at strike, or obvious negative macro catalyst from news). "
        "Respond with valid JSON only — no markdown, no explanation outside the JSON."
    )

    user_prompt = f"""Kalshi BTC 15-minute market. Decide whether to trade and which side.

MARKET: {ticker}
STRIKE: ${strike:,.2f}
BTC NOW: ${btc_price:,.2f}  ({pct_from_strike:+.3f}% from strike)
TIME LEFT: {secs_left/60:.1f} min
ELAPSED: {elapsed_seconds/60:.1f} min into 15-min window

AVAILABLE CONTRACTS (both sides open):
  BUY YES: {yes_ask:.1f}¢  — pays 100¢ if BTC > ${strike:,.2f} at close  (implied prob: {yes_ask:.1f}%)
  BUY NO:  {no_ask:.1f}¢  — pays 100¢ if BTC ≤ ${strike:,.2f} at close  (implied prob: {no_ask:.1f}%)

BTC PRICE MOVEMENT:
  5 min ago: ${btc_5m:,.2f}
  1 min ago: ${btc_1m:,.2f}
  30 s  ago: ${btc_30s:,.2f}
  Now:       ${btc_price:,.2f}
  3-min momentum: {mom_label} ({mom_pct*100:+.3f}%){sentiment_block}

PRINTER BRAIN STATE (your decisions will be stored and feed back into this):
  Overall win rate:     {_brain_cal['overall_wr']:.0%}
  Reward tier:          {_brain_cal['reward_tier']} / 3  (tier 3 = ≥85% WR = max reward)
  Min EV required:      {(_brain_cal['min_edge_override'] or 0.20):.0%}
  Prob scale factor:    {_brain_cal['prob_scale']:.2f}  (1.0 = neutral, <1 = overestimating)
  YES win rate:         {_brain_cal['bullish_wr']:.0%}
  NO  win rate:         {_brain_cal['bearish_wr']:.0%}
  Cheap contracts win:  {_adaptive['low_price_wins']}
  Near-strike wins:     {_adaptive['near_strike_wins']}

RECENT TRADES ({len(recent_trades)} completed):
{json.dumps(recent_trades, indent=2)}

YOUR JOB:
- Compare true probability (BTC distance from strike, momentum, time) vs the contract's implied probability.
- Pick the side with the bigger edge, or skip if neither side is mispriced.
- With {secs_left/60:.1f} min left, momentum and distance matter most.
- Use sentiment/news/social ONLY to break ties or flag obvious macro risk; don't override strong technical signals.

Respond with exactly this JSON:
{{
  "action": "trade" or "skip",
  "side": "yes" or "no",
  "confidence": <integer 0-100>,
  "reasoning": "<2-3 sentences explaining the edge or why skipping>",
  "key_signals": ["<signal 1>", "<signal 2>", "<signal 3>"]
}}"""

    try:
        stream = _claude_client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        async with stream as s:
            response = await s.get_final_message()

        text = ""
        for block in response.content:
            if hasattr(block, "text"):
                text = block.text
                break

        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text.strip())

        result = json.loads(text.strip())

        if "action" not in result or result["action"] not in ("trade", "skip"):
            raise ValueError(f"Invalid action: {result}")
        result.setdefault("side", "yes")
        result.setdefault("confidence", 50)
        result.setdefault("reasoning", "")
        result.setdefault("key_signals", [])

        _claude_cache[cache_key] = {"ts": time.time(), "result": result}
        last_claude_reasoning = result["reasoning"]
        last_claude_key_signals = result["key_signals"]

        log.info(
            f"[Claude] {result['action'].upper()} {result['side'].upper()} "
            f"conf={result['confidence']} | {result['reasoning'][:120]}"
        )
        return result

    except Exception as exc:
        log.error(f"Claude analysis error: {exc}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  Printer Brain v3 — Empirically Calibrated from 4.5M rows of BTC 1-min data
# ══════════════════════════════════════════════════════════════════════════════

# Empirical win-probability table: P(BTC stays on same side at window close)
# Derived from backtest of 4.5M rows binance_api_BTCUSDT_1m.csv (2017-2026 regime)
# Simulated all 15-min KXBTC15M-equivalent windows, measured each minute within.
#
# Rows = distance bucket (abs % BTC is from strike):
#   0: 0-0.1%   1: 0.1-0.2%   2: 0.2-0.3%   3: 0.3-0.4%   4: 0.4-0.5%
#   5: 0.5-0.6% 6: 0.6-0.75%  7: 0.75-1.0%  8: 1.0-1.25%  9: 1.25%+
# Columns = minutes remaining until window close (index 0 = 1 min, index 12 = 13 min)
_BV3_TABLE = [
    # 1min   2min   3min   4min   5min   6min   7min   8min   9min  10min  11min  12min  13min
    [0.850, 0.796, 0.758, 0.727, 0.705, 0.686, 0.672, 0.656, 0.639, 0.624, 0.606, 0.595, 0.578],  # 0.0-0.1%
    [0.980, 0.956, 0.931, 0.904, 0.876, 0.856, 0.833, 0.807, 0.783, 0.752, 0.733, 0.706, 0.675],  # 0.1-0.2%
    [0.994, 0.983, 0.967, 0.951, 0.933, 0.909, 0.889, 0.868, 0.835, 0.811, 0.788, 0.756, 0.713],  # 0.2-0.3%
    [0.997, 0.990, 0.981, 0.968, 0.950, 0.935, 0.917, 0.893, 0.874, 0.840, 0.816, 0.778, 0.741],  # 0.3-0.4%
    [0.998, 0.993, 0.987, 0.977, 0.962, 0.948, 0.932, 0.908, 0.883, 0.869, 0.835, 0.809, 0.782],  # 0.4-0.5%
    [0.998, 0.997, 0.988, 0.979, 0.968, 0.960, 0.944, 0.925, 0.913, 0.876, 0.849, 0.824, 0.781],  # 0.5-0.6%
    [0.999, 0.994, 0.994, 0.979, 0.974, 0.963, 0.947, 0.936, 0.914, 0.897, 0.872, 0.839, 0.817],  # 0.6-0.75%
    [0.999, 0.996, 0.995, 0.988, 0.982, 0.968, 0.963, 0.942, 0.917, 0.905, 0.884, 0.845, 0.818],  # 0.75-1.0%
    [1.000, 0.999, 0.994, 0.992, 0.984, 0.980, 0.967, 0.964, 0.935, 0.919, 0.911, 0.862, 0.820],  # 1.0-1.25%
    [1.000, 0.997, 0.995, 0.991, 0.986, 0.972, 0.971, 0.960, 0.942, 0.921, 0.904, 0.874, 0.820],  # 1.25%+
]
# Upper bound of each distance bucket as a fraction (NOT percent)
_BV3_DIST_BOUNDS = [0.001, 0.002, 0.003, 0.004, 0.005, 0.006, 0.0075, 0.010, 0.0125]


def _empirical_win_prob(abs_pct: float, mins_left: float) -> float:
    """
    Return empirical P(BTC stays on current side at window close).
    abs_pct: absolute fraction distance from strike (e.g. 0.003 = 0.3%)
    mins_left: minutes until window closes
    """
    # Distance bucket
    bidx = len(_BV3_DIST_BOUNDS)  # default = last row (1.25%+)
    for i, bound in enumerate(_BV3_DIST_BOUNDS):
        if abs_pct < bound:
            bidx = i
            break
    bidx = min(bidx, len(_BV3_TABLE) - 1)
    row = _BV3_TABLE[bidx]

    # Sub-1-min: nearly certain (just above the 1-min row value)
    if mins_left < 1.0:
        return min(0.997, row[0] + 0.005)

    # Beyond 13 min: use the 13-min value (worst case in our table)
    if mins_left >= 13.0:
        return row[12]

    # Linear interpolation between integer-minute columns
    t_low  = int(mins_left) - 1   # 0-indexed into row
    t_high = t_low + 1
    frac   = mins_left - int(mins_left)

    if t_high > 12:
        return row[12]

    bv3_val = row[t_low] + (row[t_high] - row[t_low]) * frac

    # ── Blend with live corrections if we have enough samples ─────────────────
    key = (bidx, min(t_low, 12))
    if key in _bv3_corrections:
        wins, total = _bv3_corrections[key]
        if total >= 5:
            live_wr = wins / total
            # alpha grows 0→0.40 as samples grow 5→50; table always dominates early on
            alpha = min(0.40, (total - 5) / 45 * 0.40)
            return bv3_val * (1.0 - alpha) + live_wr * alpha

    return bv3_val


def _bv3_bucket_indices(abs_pct: float, mins_left: float) -> tuple[int, int]:
    """Return (dist_idx, time_idx) for a given trade — used to record outcomes."""
    bidx = len(_BV3_DIST_BOUNDS)
    for i, bound in enumerate(_BV3_DIST_BOUNDS):
        if abs_pct < bound:
            bidx = i
            break
    bidx = min(bidx, len(_BV3_TABLE) - 1)
    t_low = max(0, min(12, int(max(1.0, mins_left)) - 1))
    return bidx, t_low


def _load_bv3_corrections() -> None:
    """Load persisted live BV3 corrections from disk on startup."""
    global _bv3_corrections
    try:
        with open(_BV3_CORRECTIONS_FILE, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        _bv3_corrections = {
            (int(k.split(",")[0]), int(k.split(",")[1])): v
            for k, v in raw.items()
        }
        total_samples = sum(v[1] for v in _bv3_corrections.values())
        log.info(f"BV3 corrections loaded: {len(_bv3_corrections)} buckets, {total_samples} total samples.")
    except FileNotFoundError:
        _bv3_corrections = {}
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        corrupt_path = f"{_BV3_CORRECTIONS_FILE}.corrupt.{int(time.time())}"
        try:
            os.rename(_BV3_CORRECTIONS_FILE, corrupt_path)
            log.warning(f"BV3 corrections corrupted ({exc}). Renamed to {corrupt_path}. Starting fresh.")
        except OSError:
            log.warning(f"BV3 corrections corrupted ({exc}). Starting fresh.")
        _bv3_corrections = {}
    except Exception as exc:
        log.warning(f"BV3 corrections load error: {exc}")
        _bv3_corrections = {}


def _save_bv3_corrections() -> None:
    """Persist live BV3 corrections to disk (atomic write)."""
    try:
        serialisable = {f"{k[0]},{k[1]}": v for k, v in _bv3_corrections.items()}
        atomic_write_json(serialisable, _BV3_CORRECTIONS_FILE)
    except Exception as exc:
        log.warning(f"BV3 corrections save error: {exc}")


def _update_bv3_correction(dist_idx: int, time_idx: int, won: bool) -> None:
    """Record one trade outcome into the live BV3 correction table."""
    key = (dist_idx, time_idx)
    if key not in _bv3_corrections:
        _bv3_corrections[key] = [0, 0]
    _bv3_corrections[key][1] += 1
    if won:
        _bv3_corrections[key][0] += 1
    wins, total = _bv3_corrections[key]
    log.info(f"BV3 correction updated: bucket {key} -> {wins}/{total} ({wins/total:.0%})")
    _save_bv3_corrections()


def _session_ev_adjustment() -> float:
    """
    Return a small EV threshold delta based on UTC hour of day.

    US session (13-20 UTC): lower threshold by 0.02 — clearest trends, easiest holds.
    Asian dead hours (00-06 UTC): raise threshold by 0.02 — choppy/rangebound.
    All other hours: neutral.
    """
    hour = datetime.now(timezone.utc).hour
    if 13 <= hour < 20:
        return -0.02
    if 0 <= hour < 6:
        return +0.02
    return 0.0


def printer_brain(
    btc_price: float,
    strike: float,
    yes_ask: float,
    no_ask: float,
    elapsed_seconds: float,
    secs_left: float,
    ticker: str,
    min_ev_base: float = 3.0,
    vol_gate_thresh: float = 1.80,
    kalshi_fee: float = 0.07,
) -> dict:
    """
    Printer Brain v3 — empirically calibrated from 4.5M rows BTC 1-min data.

    Key findings from backtest (2020-2026 regime):
      - Even within 0.1% of strike BTC stays on same side ~70% of the time
        (momentum / continuation effect — NOT a coin flip)
      - At 0.2% distance with 5 min left: 93% win rate
      - At 0.5% distance with 5 min left: 97% win rate
      - Old Brain v2 was estimating 52-70% for these — massively underconfident

    Decision logic: trade when expected value (EV) is positive.
      EV = P(win) - contract_cost
      e.g. 90% win rate, contract at 80c → EV = +10c per $1 payout
    """
    mom_pct, mom_label = calculate_momentum()
    vel_signal = contract_velocity(ticker, "yes")
    mins_left = secs_left / 60
    pct_above = (btc_price - strike) / strike   # + = BTC above strike
    abs_pct   = abs(pct_above)
    above     = pct_above > 0

    # ── 0. Realized volatility gate ──────────────────────────────────────────
    # Brownian motion: expected BTC move before expiry ≈ vol * sqrt(mins_left).
    # If that expected move ≥ 90% of the distance to strike, the distance is
    # not large enough relative to current volatility — skip the trade.
    _rv        = btc_realized_vol()
    _vol_skip  = False
    _vol_ratio = None
    if _rv is not None and abs_pct > 0:
        _expected_move = _rv * (mins_left ** 0.5)
        _vol_ratio     = _expected_move / abs_pct
        if _vol_ratio >= vol_gate_thresh:
            _vol_skip = True

    # ── 1. Empirical win probability from backtest table ──────────────────────
    win_prob_raw = _empirical_win_prob(abs_pct, mins_left)

    # ── 2. Momentum adjustment (empirical: confirms avg 87% vs opposes 76%) ──────
    # Flat adjustment — no strength multiplier to prevent direction flips.
    # Confirms: BTC moving away from strike (+5%). Opposes: toward strike (-5%).
    if mom_label == "bullish":
        mom_adj = +0.05 if above else -0.05
    elif mom_label == "bearish":
        mom_adj = +0.05 if not above else -0.05
    else:
        mom_adj = 0.0

    # ── 3. Contract velocity ──────────────────────────────────────────────────
    vel_adj = +0.01 if vel_signal == "favorable" else (-0.01 if vel_signal == "unfavorable" else 0.0)

    # ── 4. Combined probability + learned calibration scale ──────────────────
    win_prob = win_prob_raw + mom_adj + vel_adj
    win_prob = 0.50 + (win_prob - 0.50) * _brain_cal["prob_scale"]
    win_prob = max(0.10, min(0.997, win_prob))

    # ── 5. YES / NO win probabilities ────────────────────────────────────────
    prob_yes = win_prob if above else (1.0 - win_prob)
    prob_no  = 1.0 - prob_yes

    # ── 5b. Market-implied probability anchor ────────────────────────────────
    # The BV3 model uses historical average BTC behavior. In volatile/trending
    # regimes (crash days, news spikes) the market's live pricing is more
    # accurate than the backtest table. When our model and the market disagree
    # by >25 percentage points, blend toward market consensus so EV stays
    # realistic and doesn't show fictitious 40-60% edges.
    #
    # Market-implied prob for the side we'd bet:
    mkt_implied = (yes_ask / 100) if above else (no_ask / 100)
    model_side_prob = prob_yes if above else prob_no
    divergence = model_side_prob - mkt_implied   # positive = model more optimistic
    if divergence > 0.25:
        # Blend weight grows from 0→0.5 as divergence goes from 25%→65%
        blend = min(0.50, (divergence - 0.25) / 0.40 * 0.50)
        blended = model_side_prob * (1 - blend) + mkt_implied * blend
        if above:
            prob_yes = blended
            prob_no  = 1.0 - blended
        else:
            prob_no  = blended
            prob_yes = 1.0 - blended
        brain_log.debug(
            f"Market-anchor: model={model_side_prob:.1%} mkt={mkt_implied:.1%} "
            f"div={divergence:.1%} blend={blend:.2f} → {blended:.1%}"
        )

    # ── 6. Expected value vs actual contract price ────────────────────────────
    # yes_ask / no_ask are in cents (0-100). $1 payout.
    # Fee deducted here: Kalshi charges ~7c per contract, reducing net EV by 0.07.
    # A "5% edge" trade before this fix was actually -2% after fees.
    yes_ev = prob_yes - (yes_ask / 100) - kalshi_fee
    no_ev  = prob_no  - (no_ask  / 100) - kalshi_fee

    # Directional track record: penalise if a direction has been losing badly
    if _brain_cal["bullish_wr"] < 0.35: yes_ev -= 0.04
    if _brain_cal["bearish_wr"] < 0.35: no_ev  -= 0.04

    # ── 7. Pick side — always bet with BTC's position (continuation) ────────
    # Best-EV switching caused confidence gate failures: contrarian contracts
    # have 20-35% win prob and always fail the 65% floor. Continuation side
    # naturally has 65-85% win prob; EV gate (3%) handles negative-EV skips.
    if above:
        side, best_ev, entry_c, true_p = "yes", yes_ev, yes_ask, prob_yes
    else:
        side, best_ev, entry_c, true_p = "no",  no_ev,  no_ask,  prob_no

    # ── 8. EV filter ──────────────────────────────────────────────────────────
    # Base 3% floor + session adjustment: US session (13-20 UTC) lowers bar by
    # 2% to 1% — clearest trends, most liquid. Asian dead hours (00-06 UTC)
    # raise bar by 2% to 5% — choppy/rangebound, need stronger edge to justify.
    min_ev = (min_ev_base / 100.0) + _session_ev_adjustment()

    skip_reason = ""
    if _vol_skip:
        _ratio_str = f"{_vol_ratio:.2f}" if _vol_ratio is not None else "?"
        skip_reason = (
            f"vol too high: expected move covers {_ratio_str}x the strike distance "
            f"(dist={abs_pct*100:.2f}% | {mins_left:.1f} min left)"
        )
    elif best_ev < min_ev:
        skip_reason = (
            f"EV {best_ev:+.1%} below {min_ev:.0%} minimum | "
            f"{side.upper()} at {entry_c:.0f}c, win prob {true_p:.1%}"
        )

    action = "skip" if skip_reason else "trade"

    # ── 9. Confidence = win probability 0-100 ────────────────────────────────
    confidence = min(99, max(0, int(true_p * 100) + _brain_cal["confidence_bonus"]))

    _rv_str = f"{_rv*100:.3f}%/min" if _rv is not None else "n/a"
    _ratio_display = f"{_vol_ratio:.2f}" if _vol_ratio is not None else "n/a"
    if action == "trade":
        reasoning = (
            f"BTC is {abs_pct*100:.2f}% {'above' if above else 'below'} strike "
            f"with {mins_left:.1f} min left. Empirical win prob: {true_p:.1%} "
            f"(raw {win_prob_raw:.1%} + mom {mom_adj:+.1%}). "
            f"Contract {entry_c:.0f}c -> EV {best_ev:+.1%}. "
            f"Momentum: {mom_label} ({mom_pct*100:+.2f}%). Vol: {_rv_str} (ratio {_ratio_display})."
        )
    else:
        reasoning = skip_reason or f"No EV edge. Best: {best_ev:+.1%} (need {min_ev:.0%})"

    key_signals = [
        f"BTC {pct_above*100:+.2f}% from strike | {mins_left:.1f} min left",
        f"Win prob: YES={prob_yes:.1%}  NO={prob_no:.1%}  (raw={win_prob_raw:.1%})",
        f"EV: YES={yes_ev:+.1%}  NO={no_ev:+.1%}  (min {min_ev:.0%})",
        f"Momentum: {mom_label} ({mom_pct*100:+.2f}%) | Velocity: {vel_signal}",
        f"Realized vol: {_rv_str} | Vol ratio: {_ratio_display} (skip >=>{vol_gate_thresh:.2f})",
    ]

    brain_log.info(
        f"{action.upper():5} {side.upper():3} conf={confidence:3} | "
        f"dist={abs_pct*100:.3f}% {'UP' if above else 'DN'} | "
        f"ev={best_ev:+.1%} floor={min_ev:.0%} | prob={true_p:.1%} raw={win_prob_raw:.1%} mom={mom_adj:+.1%} | "
        f"contract={entry_c:.0f}c | {mom_label} {mins_left:.1f}min | "
        f"vol={_rv_str} ratio={_ratio_display}"
    )
    return {"action": action, "side": side, "confidence": confidence,
            "reasoning": reasoning, "key_signals": key_signals,
            "win_prob": float(true_p),
            "mom_label": mom_label, "mom_pct": float(mom_pct),
            "vel_signal": vel_signal,
            "mins_left": mins_left, "abs_pct": abs_pct, "above": above,
            "_rv": _rv, "_vol_ratio": _vol_ratio}



# ══════════════════════════════════════════════════════════════════════════════
#  Reversal signal
# ══════════════════════════════════════════════════════════════════════════════

def _reversal_signal(
    abs_pct: float,
    mins_left: float,
    mom_pct: float,
    mom_label: str,
    vel_signal: str,
    above: bool,       # True = BTC currently ABOVE strike
    yes_ask: float,
    no_ask: float,
    vol_ratio: float | None,
) -> dict:
    """
    Evaluate an exhaustion-reversal setup.

    Fires when:
      - BTC has made a strong directional move (momentum in current direction)
      - The opposing contract is very cheap (≤ 20¢) — market prices reversal unlikely
      - Deceleration signals present (velocity unfavorable for current side)
      - Time window optimal (3–12 min remaining)

    Returns a dict: {signal, side, ask, prob, ev, reason}
    """
    # The reversal bets the OPPOSITE of where BTC currently sits
    rev_side = "no" if above else "yes"
    rev_ask  = no_ask if above else yes_ask

    # Gate 1: contract must be cheap — this is what makes reversal bets worth taking
    if rev_ask > 20:
        return {"signal": False, "reason": f"reversal contract {rev_ask:.0f}¢ > 20¢, not cheap enough"}

    # Gate 2: time window — needs 3–12 min for the reversal to play out
    if mins_left < 3 or mins_left > 12:
        return {"signal": False, "reason": f"time {mins_left:.1f}m outside 3–12m reversal window"}

    # Gate 3: momentum must be strongly WITH BTC's current side — need exhaustion to reverse.
    # 0.007 = 0.7% move in 3 min. calculate_momentum() labels anything > 0.5% as bullish/bearish,
    # so this adds a small extra bar above the label threshold to confirm the move is meaningful.
    mom_in_current = (mom_label == "bullish" and above) or (mom_label == "bearish" and not above)
    if not mom_in_current or abs(mom_pct) < 0.007:
        return {
            "signal": False,
            "reason": f"insufficient exhaustion signal (mom={mom_label} {mom_pct*100:+.2f}%, above={above})",
        }

    # Gate 4: distance not too extreme — hard to reverse when BTC is far from strike.
    # 0.010 = 1.0%. At 1%+ distance the BV3 continuation rate is 97-100%; even a
    # cheap opposing contract can't generate enough reversal probability to beat the EV bar.
    if abs_pct > 0.010:
        return {"signal": False, "reason": f"BTC too far from strike ({abs_pct*100:.2f}%) for reliable reversal"}

    # ── Reversal probability ──────────────────────────────────────────────────
    # Base: complement of the BV3 continuation probability
    bv3_cont = _empirical_win_prob(abs_pct, mins_left)
    rev_prob  = 1.0 - bv3_cont

    # Exhaustion boost: stronger momentum → more likely to have exhausted, mean-revert.
    # Grows from 0 at the gate threshold (0.7%) to the 0.08 cap at ~2.7% move.
    # Formula rescaled to match corrected gate: was (pct - 0.10) * 0.30, which was
    # permanently zero because pct never reaches 10%.
    exhaust_boost = min(0.08, max(0.0, (abs(mom_pct) - 0.007) * 4.0))

    # Velocity deceleration boost: price movement slowing = reversal more likely
    vel_boost = 0.04 if vel_signal == "unfavorable" else 0.0

    # High-vol penalty: unpredictable in wild markets.
    # Threshold 1.00 aligns with the main strategy vol gate (skip ≥1.50) —
    # reversal setups are inherently riskier, so penalty starts earlier,
    # but below 1.00 (main gate considers safe) we apply no penalty.
    vol_penalty = 0.0
    if vol_ratio is not None and vol_ratio > 1.00:
        vol_penalty = min(0.10, (vol_ratio - 1.00) * 0.20)

    rev_prob = max(0.05, min(0.45, rev_prob + exhaust_boost + vel_boost - vol_penalty))

    # ── Reversal EV ──────────────────────────────────────────────────────────
    rev_ev = rev_prob - (rev_ask / 100)

    # Lower EV bar than main strategy (8%) since we're buying cheap contrarian contracts
    if rev_ev < 0.08:
        return {
            "signal": False,
            "reason": (
                f"reversal EV {rev_ev:+.1%} below 8% minimum "
                f"(prob={rev_prob:.1%} vs market {rev_ask:.0f}¢)"
            ),
        }

    reason = (
        f"REVERSAL {rev_side.upper()} @ {rev_ask:.0f}¢ | "
        f"prob={rev_prob:.1%} (base={bv3_cont:.1%} cont → {1-bv3_cont:.1%} rev, "
        f"+exhaust={exhaust_boost:.1%} +vel={vel_boost:.1%} -vol={vol_penalty:.1%}) | "
        f"EV={rev_ev:+.1%} | exhaustion: {mom_label} {mom_pct*100:+.2f}%"
    )
    brain_log.info(f"REVERSAL signal: {reason}")
    return {
        "signal": True,
        "side": rev_side,
        "ask": rev_ask,
        "prob": rev_prob,
        "ev": rev_ev,
        "reason": reason,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Position sizing
# ══════════════════════════════════════════════════════════════════════════════

def calculate_contracts(
    trade_amount_dollars: float,
    entry_price_cents: int,
    liquidity: int,
    win_prob: float = 0.85,
    kelly_cap: float = 0.25,
    use_fixed_sizing: bool = False,
) -> tuple[int, float]:
    """
    Kelly Criterion position sizing with quarter-Kelly cap.

    Kelly fraction f* = (p*b - (1-p)) / b
      where b = (100 - price) / price  (decimal payout odds on a $1 contract)

    Normalized so that a "typical" trade (85% win, 80c) = 1.0x base bet.
    Capped at 3x, floored at 0.5x to prevent overbetting and underbetting.

    WARNING: Kelly sizing assumes the probability estimates (from BV3 table) and
    the price estimates (from simulate_amm_prices) are accurate. The quant review
    flagged that simulated prices may be 8-15c cheaper than real Kalshi prices.
    Until price_validation_log.csv confirms pricing accuracy over 200+ samples,
    consider using fixed sizing (use_fixed_sizing=true in config) instead.

    Returns:
        (contracts, kelly_dollars_used)
    """
    if entry_price_cents <= 0:
        return 0, 0.0

    if use_fixed_sizing:
        # Fixed sizing: ignore Kelly entirely, use the configured amount directly
        kelly_dollars = trade_amount_dollars
        contracts = int(kelly_dollars * 100 / entry_price_cents)
        contracts = min(contracts, liquidity)
        contracts = max(contracts, 0)
        log.info(
            f"Fixed sizing: price={entry_price_cents}c "
            f"bet=${kelly_dollars:.2f} -> {contracts} contracts"
        )
        return contracts, kelly_dollars

    # Kelly fraction
    b = (100 - entry_price_cents) / entry_price_cents   # payout odds
    if b <= 0:
        kelly_f = 0.0
    else:
        kelly_f = max(0.0, (win_prob * b - (1.0 - win_prob)) / b)

    # Quarter-Kelly cap: even full Kelly is too aggressive with uncertain probability estimates.
    # Caps the raw fraction before normalization so the relative sizing still scales with edge.
    kelly_f = min(kelly_f, kelly_cap)

    # Normalize to typical trade (85% win, 80c → kelly_f ≈ 0.2125)
    _typical_kelly = 0.2125
    multiplier = kelly_f / _typical_kelly if _typical_kelly > 0 else 1.0
    multiplier = max(0.5, min(3.0, multiplier))

    # Hard cap: Kelly can scale up but never beyond trade_amount_dollars.
    # trade_amount_dollars is the maximum spend per trade, not just a base.
    kelly_dollars = min(trade_amount_dollars, trade_amount_dollars * multiplier)
    trade_cents   = kelly_dollars * 100
    contracts = int(trade_cents / entry_price_cents)
    contracts = min(contracts, liquidity)
    contracts = max(contracts, 0)

    actual_cost = contracts * entry_price_cents / 100
    if actual_cost > kelly_dollars + 0.01:
        contracts = max(0, contracts - 1)

    log.info(
        f"Kelly sizing: win_prob={win_prob:.0%} price={entry_price_cents}c "
        f"f*={kelly_f:.3f} (cap={kelly_cap:.2f}) mult={multiplier:.2f}x "
        f"bet=${kelly_dollars:.2f} (base=${trade_amount_dollars}) -> {contracts} contracts"
    )
    return contracts, kelly_dollars


# ══════════════════════════════════════════════════════════════════════════════
#  Probability helpers
# ══════════════════════════════════════════════════════════════════════════════

def implied_prob(contract_price_cents: float) -> float:
    """Convert contract price in cents to implied probability (0–1)."""
    return contract_price_cents / 100.0



# ══════════════════════════════════════════════════════════════════════════════
#  Order placement
# ══════════════════════════════════════════════════════════════════════════════

async def place_order(
    session: aiohttp.ClientSession,
    ticker: str,
    side: str,
    contracts: int,
    entry_price_cents: int,
    mode: str,
    market: dict | None = None,
) -> dict:
    """
    Place a fill-or-kill limit order on Kalshi.

    Re-fetches a fresh price before each attempt — the price from READY
    evaluation can be stale by the time we reach this call (brain + Claude
    evaluation takes time, and BTC markets reprice fast).

    Retries up to 5 times, bumping by 2c each attempt.
    In paper mode, simulates an instant fill without hitting the API.

    Returns:
        Dict with keys: fill_confirmed (bool), fill_price_cents (int|None),
        order_id (str|None).
    """
    if mode == "paper":
        log.info(f"[PAPER] Simulated BUY {side} {contracts}x @ {entry_price_cents}c on {ticker}")
        return {
            "fill_confirmed": True,
            "fill_price_cents": entry_price_cents,
            "order_id": f"paper_{int(time.time() * 1000)}",
        }

    path = "/portfolio/orders"
    _bump_per_retry = 3   # cents per retry — 3c gives 0/3/6/9/12c spread across 5 attempts
    _max_retries    = 5

    for attempt in range(_max_retries):
        # ── Re-fetch fresh price before each attempt ───────────────────────────
        # Orderbook data from READY eval can be 5-30s stale by here. A fresh
        # fetch ensures we're pricing against the actual current market.
        fresh_price = None
        try:
            fresh_ob = await fetch_orderbook(session, ticker, market)
            if fresh_ob is not None:
                fresh_price = fresh_ob["best_yes_ask"] if side == "yes" else fresh_ob["best_no_ask"]
        except Exception as _fe:
            log.warning(f"Fresh price fetch failed (attempt {attempt}): {_fe}")

        if fresh_price is not None and fresh_price != entry_price_cents:
            log.info(f"Price updated: {entry_price_cents}c -> {fresh_price}c for {side.upper()} on {ticker}")
            entry_price_cents = fresh_price

        # Always bump from the latest fresh price. No static ceiling — the fresh
        # price already tracks the market; we just add the per-attempt bump on top.
        # Hard cap at 99c for YES side; for NO side the 99 cap is effectively unreachable.
        price_this_attempt = min(99, entry_price_cents + attempt * _bump_per_retry)
        yes_price = price_this_attempt if side == "yes" else (100 - price_this_attempt)
        client_order_id = f"btcbot_{int(time.time() * 1000)}_{attempt}"

        body = {
            "ticker": ticker,
            "side": side,
            "type": "limit",
            "count": contracts,
            "yes_price": yes_price,
            "action": "buy",
            "client_order_id": client_order_id,
            "time_in_force": "immediate_or_cancel",
        }

        if attempt > 0:
            log.info(f"Order retry {attempt}/{_max_retries-1} at {price_this_attempt}c (fresh={fresh_price}c)...")
            await asyncio.sleep(0.5)

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
            log.error(f"Order placement error (attempt {attempt}): {exc}")
            # POST failed — check portfolio before retrying to avoid double-fill
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
                            log.info(f"POST exception but portfolio shows {held_count}x position — treating as filled")
                            return {"fill_confirmed": True, "fill_price_cents": price_this_attempt, "order_id": None, "filled_contracts": held_count}
            except Exception as chk_exc:
                log.error(f"Portfolio check after POST exception failed: {chk_exc}")
            continue

        if http_status not in (200, 201):
            log.error(f"Order HTTP {http_status}: {data}")
            err_code = (data.get("error") or {}).get("code", "")
            if err_code in ("insufficient_funds", "authentication_error", "not_found", "forbidden"):
                # fill_or_kill_insufficient_resting_volume is intentionally retryable:
                # bumping the price on the next attempt may find liquidity.
                log.error(f"Non-retryable error ({err_code}). Stopping order attempts.")
                await send_telegram(
                    f"🚫 <b>ORDER FAILED</b>  —  {err_code}\n"
                    f"{side.upper()}  {contracts}x @ {price_this_attempt}¢  |  <code>{ticker}</code>"
                )
                break
            continue

        order_id = (data.get("order") or {}).get("order_id") or data.get("order_id")
        if not order_id:
            log.error(f"No order_id in response: {data}")
            continue

        # ── For IOC orders: "canceled" = zero fill. Everything else = filled.
        # Don't guess specific status strings — Kalshi may return "resting",
        # "executed", "filled", or others for a successful fill.
        post_order  = data.get("order") or data
        post_status = post_order.get("status", "")
        log.info(f"Order {order_id} POST status={post_status!r}")

        if post_status == "canceled":
            # For IOC orders "canceled" means either:
            #   (a) zero fill — contracts_count=0 → retry at higher price
            #   (b) partial fill — contracts_count>0 (filled + remainder canceled)
            _cc_canceled = post_order.get("contracts_count")
            if _cc_canceled:
                _fp_raw = post_order.get("yes_price", price_this_attempt)
                # Kalshi always returns yes_price. For NO buys, convert back to the NO cost.
                _fp_canceled = _fp_raw if side == "yes" else (100 - _fp_raw)
                log.info(f"Order {order_id} IOC partial fill: {_cc_canceled} contracts @ {_fp_canceled}c (canceled remainder)")
                return {"fill_confirmed": True, "fill_price_cents": _fp_canceled, "order_id": order_id, "filled_contracts": _cc_canceled}
            log.info(f"Order {order_id} IOC zero-fill (canceled) — retrying")
            continue

        # Determine actual fill count — must use explicit None check because
        # Kalshi sometimes returns contracts_count=0 on 'executed' status when
        # there was no counter-party.  Python `or` treats 0 as falsy and would
        # incorrectly fall through to the original `contracts` value.
        cc = post_order.get("contracts_count")
        fc = post_order.get("filled_count")
        filled_count = cc if cc is not None else (fc if fc is not None else contracts)

        if filled_count == 0:
            # Non-canceled status but zero fill (Kalshi 'executed' with no counter-party).
            # Treat exactly like a canceled IOC — bump price and retry.
            log.warning(f"Order {order_id} status={post_status!r} but filled_count=0 — bumping price and retrying")
            continue

        _fill_yes_price = post_order.get("yes_price", price_this_attempt)
        # Kalshi always returns yes_price. For NO buys, convert to the actual NO cost.
        fill_price = _fill_yes_price if side == "yes" else (100 - _fill_yes_price)
        log.info(f"Order FILLED: {order_id} @ {fill_price}c x{filled_count} status={post_status!r}")
        return {"fill_confirmed": True, "fill_price_cents": fill_price, "order_id": order_id, "filled_contracts": filled_count}

    # Both attempts exhausted — check portfolio directly as ground truth.
    # Handles the case where the request went through but the response timed out.
    log.warning(f"Order attempts exhausted for {ticker} — checking portfolio for open position")
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
                if side == "yes" and held > 0:
                    log.info(f"Portfolio check: found YES position {held}x on {ticker} — order DID fill")
                    return {"fill_confirmed": True, "fill_price_cents": entry_price_cents, "order_id": None, "filled_contracts": held}
                if side == "no" and held < 0:
                    held_no = abs(held)
                    log.info(f"Portfolio check: found NO position {held_no}x on {ticker} — order DID fill")
                    return {"fill_confirmed": True, "fill_price_cents": entry_price_cents, "order_id": None, "filled_contracts": held_no}
        log.info(f"Portfolio check: no position found for {ticker} — genuinely not filled")
    except Exception as exc:
        log.error(f"Portfolio check error: {exc}")

    log.error(f"Order not filled after {_max_retries} attempts for {ticker} {side}@{entry_price_cents}c")
    await send_telegram(
        f"⚠️ <b>ORDER NOT FILLED</b>  —  no liquidity\n"
        f"{side.upper()}  {contracts}x @ {entry_price_cents}¢  |  <code>{ticker}</code>"
    )
    return {"fill_confirmed": False, "fill_price_cents": None, "order_id": None}


async def sell_position(
    session: aiohttp.ClientSession,
    ticker: str,
    side: str,
    contracts: int,
    mode: str,
    current_bid: int,
) -> int:
    """
    Exit a position via a market IOC sell order. Retries up to 3 times.
    In paper mode, simulates an instant sell at the current bid.

    Returns:
        Exit price in cents.
    """
    if mode == "paper":
        log.info(f"[PAPER] Simulated SELL {side} {contracts}x @ {current_bid}c on {ticker}")
        return current_bid

    path = "/portfolio/orders"

    for attempt in range(3):
        if attempt > 0:
            log.warning(f"Sell retry {attempt}/2...")
            await asyncio.sleep(1)
        body = {
            "ticker": ticker,
            "side": side,
            "type": "market",
            "count": contracts,
            "action": "sell",
            "client_order_id": f"btcbot_exit_{int(time.time() * 1000)}_{attempt}",
            "time_in_force": "immediate_or_cancel",
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
            if http_status in (200, 201):
                fill_price = (data.get("order") or {}).get("yes_price", current_bid)
                log.info(f"Sell filled @ {fill_price}c (attempt {attempt})")
                return fill_price
            log.error(f"Sell HTTP {http_status}: {data}")
        except Exception as exc:
            log.error(f"Sell order error (attempt {attempt}): {exc}")

    log.error(f"Sell failed after 3 attempts — using bid price {current_bid}c for PnL")
    return current_bid


# ══════════════════════════════════════════════════════════════════════════════
#  Daily limits
# ══════════════════════════════════════════════════════════════════════════════

async def check_daily_limits(config: dict) -> tuple[bool, str]:
    """
    Check daily loss limit and profit target for live mode.
    Switches mode to 'paper' in config.json and memory when triggered.

    Returns:
        (triggered: bool, reason: str)
    """
    global limit_triggered, limit_reason, pre_limit_mode

    if config.get("mode") == "paper":
        return False, ""

    live_pnl = await db_get_today_pnl("live")

    if live_pnl < 0 and abs(live_pnl) >= config.get("daily_loss_limit_dollars", 20):
        if not limit_triggered:
            limit_triggered = True
            limit_reason = "daily loss limit reached"
            pre_limit_mode = config["mode"]
            cfg = read_config()
            cfg["mode"] = "paper"
            write_config(cfg)
            log.warning(f"Daily loss limit hit (${live_pnl:.2f}). Switched to paper mode.")
        return True, limit_reason

    if live_pnl > 0 and live_pnl >= config.get("daily_profit_target_dollars", 50):
        if not limit_triggered:
            limit_triggered = True
            limit_reason = "daily profit target reached"
            pre_limit_mode = config["mode"]
            cfg = read_config()
            cfg["mode"] = "paper"
            write_config(cfg)
            log.info(f"Daily profit target hit (${live_pnl:.2f}). Switched to paper mode.")
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


# ══════════════════════════════════════════════════════════════════════════════
#  State file
# ══════════════════════════════════════════════════════════════════════════════

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
        "claude_reasoning": last_claude_reasoning,
        "claude_key_signals": last_claude_key_signals,
        "claude_active": _claude_client is not None,
        "reward_tier": _brain_cal["reward_tier"],
        "brain_wr": _brain_cal["overall_wr"],
        "brain_min_edge": _brain_cal["min_edge_override"],
        "brain_n": _brain_cal["last_count"],
        "last_action": action,
        "last_skip_reason": skip_reason,
        "last_reversal_reason": last_reversal_reason,
        "mode": config.get("mode", "paper"),
        "today_live_pnl": await db_get_today_pnl("live"),
        "today_paper_pnl": await db_get_today_pnl("paper"),
        "config": {**config,
                   "min_ev_pct": round((config.get("min_ev_base", 3.0) / 100.0 + _session_ev_adjustment()) * 100),
                   "vol_gate_thresh": config.get("vol_gate_thresh", 1.80)},
        "limit_triggered": limit_triggered,
        "limit_reason": limit_reason,
        "open_position": current_position,
        "consecutive_losses": _consecutive_losses,
        "consecutive_loss_pause_until": _consecutive_loss_pause_until,
    }
    try:
        atomic_write_json(state, _STATE_FILE)
    except Exception as exc:
        log.error(f"State file write error: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
#  Phase handlers
# ══════════════════════════════════════════════════════════════════════════════

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


async def handle_ready_phase(
    session: aiohttp.ClientSession,
    config: dict,
    market: dict,
    ticker: str,
    btc_price: float,
    secs_left: float,
    strike: float,
    elapsed: float,
) -> None:
    """
    Evaluate entry conditions for one READY-phase iteration.
    Advances to LOCKED on a successful fill, or logs the skip reason.
    """
    global current_phase, current_position
    global last_confidence_score, last_confidence_breakdown
    global last_action, last_skip_reason, last_reversal_reason

    mode = config.get("mode", "paper")

    # Hard expiry gate — truly nothing to do in the last 90 seconds
    if secs_left < 90:
        log.info(f"{ticker}: < 90s remaining. Moving to DONE.")
        current_phase = "DONE"
        return

    # ── Multi-window best-pick ────────────────────────────────────────────────
    # If multiple 15-min windows are open simultaneously, evaluate all of them
    # and trade the one with the highest EV. Falls back to primary market if
    # only one window is open or fetching alternatives fails.
    global current_market
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
                    c_brain = printer_brain(
                        btc_price, c_strike,
                        c_ob["best_yes_ask"], c_ob["best_no_ask"],
                        c_elapsed, c_secs_left, c_ticker,
                        min_ev_base=config.get("min_ev_base", 3.0),
                        vol_gate_thresh=config.get("vol_gate_thresh", 1.80),
                        kalshi_fee=config.get("kalshi_fee_per_contract_cents", 7) / 100,
                    )
                    c_win_prob = c_brain.get("win_prob", 0.5)
                    c_entry    = c_ob["best_yes_ask"] if c_brain["side"] == "yes" else c_ob["best_no_ask"]
                    _c_fee     = config.get("kalshi_fee_per_contract_cents", 7) / 100
                    c_ev       = c_win_prob - c_entry / 100 - _c_fee
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

    # Orderbook — retry next cycle if temporarily unavailable
    if ob is None:
        try:
            ob = await fetch_orderbook(session, ticker, market)
        except Exception as exc:
            log.error(f"Orderbook error in READY: {exc}")
            return

    if ob is None:
        last_action, last_skip_reason = "watching", "no price data — retrying"
        return

    yes_ask = ob["best_yes_ask"]
    no_ask  = ob["best_no_ask"]   # fetched directly from no_ask_dollars, not derived

    # ── Price validation: compare simulated vs real prices ───────────────────
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

    # ── Printer Brain — primary decision engine (always runs, no API needed) ──
    brain = printer_brain(btc_price, strike, yes_ask, no_ask, elapsed, secs_left, ticker,
                          min_ev_base=config.get("min_ev_base", 3.0),
                          vol_gate_thresh=config.get("vol_gate_thresh", 1.80),
                          kalshi_fee=config.get("kalshi_fee_per_contract_cents", 7) / 100)
    side     = brain["side"]
    score    = brain["confidence"]
    do_trade = brain["action"] == "trade"
    skip_reason_ai = brain["reasoning"]

    # ── Claude — blend with brain, don't replace it ──────────────────────────
    # HIGH CONFIDENCE (brain win_prob >= 0.85 and action=trade) → trade immediately, skip Claude
    # HARD SKIP (brain EV < 0.02) → skip, don't waste API call
    # BORDERLINE (EV above 0.02 but below session floor, OR confidence 60-84) → ask Claude
    # Disabled by default (enable_claude_filter=false) — adds latency/cost with no proven edge.
    # The claude_conf >= 75 upgrade path (skip→trade) is particularly dangerous when disabled.
    _active_claude_result = None
    entry_price_cents = yes_ask if side == "yes" else no_ask
    _fee = config.get("kalshi_fee_per_contract_cents", 7) / 100
    brain_ev = brain.get("win_prob", 0.5) - (entry_price_cents / 100) - _fee
    brain_win_prob = brain.get("win_prob", 0.5)

    if _claude_client is not None and config.get("enable_claude_filter", False):
        if brain_win_prob >= 0.95 and do_trade:
            # Very high confidence — go straight through without Claude overhead
            log.info(f"{ticker}: Brain high-conf {score}, skipping Claude")
        elif brain_ev < 0.02:
            # Hard skip — no edge worth evaluating
            pass
        else:
            # Borderline — get Claude's second opinion
            claude_result = await claude_analysis(
                session, btc_price, strike, yes_ask, no_ask, elapsed, secs_left, ticker
            )
            if claude_result is not None:
                _active_claude_result = claude_result
                claude_conf = claude_result.get("confidence", 50)
                # 70% brain, 30% Claude
                blended_score = int(0.70 * score + 0.30 * claude_conf)
                # Claude can upgrade a brain skip to a trade if it's confident enough
                if not do_trade and claude_result["action"] == "trade" and claude_conf >= 75:
                    do_trade = True
                    side = claude_result["side"]
                    score = blended_score
                    skip_reason_ai = ""
                    log.info(f"{ticker}: Claude upgraded skip→trade (claude_conf={claude_conf}, blended={blended_score})")
                # Claude can downgrade a brain trade to skip if it's bearish
                elif do_trade and claude_result["action"] == "skip" and claude_conf < 40:
                    do_trade = False
                    score = blended_score
                    skip_reason_ai = f"Claude vetoed (conf={claude_conf}): {claude_result.get('reasoning', '')[:80]}"
                    log.info(f"{ticker}: Claude vetoed trade (claude_conf={claude_conf})")
                else:
                    # Blend score but keep brain's action
                    score = blended_score
                    log.info(f"{ticker}: Claude blended score {score} (brain={brain['confidence']}, claude={claude_conf})")

    entry_price_cents = yes_ask if side == "yes" else no_ask

    # Dashboard breakdown from Brain v3 components
    win_p_raw   = _empirical_win_prob(abs((btc_price - strike) / strike), secs_left / 60)
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
    conf_threshold = int(config.get("confidence_threshold", 72))
    if do_trade and raw_win_pct < conf_threshold:
        skip_reason_ai = f"win prob {raw_win_pct}% below floor {conf_threshold}%"
        do_trade = False

    # ── Reversal model — runs whenever main strategy skips ───────────────────
    # Evaluates exhaustion-reversal setups independent of the continuation model.
    # Uses 50% of configured trade amount. Never fires when main strategy trades.
    # Disabled by default (enable_reversal_signal=false) — no backtested evidence.
    _rev = None
    if not do_trade and config.get("enable_reversal_signal", False):
        _rev = _reversal_signal(
            abs_pct       = brain.get("abs_pct", abs((btc_price - strike) / strike)),
            mins_left     = secs_left / 60,
            mom_pct       = brain.get("mom_pct", 0.0),
            mom_label     = brain.get("mom_label", "neutral"),
            vel_signal    = brain.get("vel_signal", "neutral"),
            above         = brain.get("above", btc_price > strike),
            yes_ask       = yes_ask,
            no_ask        = no_ask,
            vol_ratio     = brain.get("_vol_ratio"),
        )
        if _rev and _rev["signal"]:
            log.info(f"{ticker}: {_rev['reason']}")
            side              = _rev["side"]
            entry_price_cents = _rev["ask"]
            score             = int(_rev["prob"] * 100)
            do_trade          = True
            skip_reason_ai    = ""
            _is_reversal      = True
            last_reversal_reason = _rev["reason"]
        else:
            rev_reason = _rev["reason"] if _rev else "reversal not evaluated"
            last_reversal_reason = rev_reason
            log.info(f"{ticker}: watching — {skip_reason_ai} | reversal: {rev_reason}")
            await _log_entry(market, "READY", secs_left, btc_price, strike,
                             int(entry_price_cents), score, "skip", skip_reason_ai, mode)
            last_action, last_skip_reason = "watching", skip_reason_ai
            return
    else:
        _is_reversal = False

    if not do_trade:
        log.info(f"{ticker}: watching — {skip_reason_ai}")
        await _log_entry(market, "READY", secs_left, btc_price, strike,
                         int(entry_price_cents), score, "skip", skip_reason_ai, mode)
        last_action, last_skip_reason = "watching", skip_reason_ai
        return

    # Daily limits — may flip mode to paper
    limit_hit, _ = await check_daily_limits(config)
    if limit_hit:
        config = read_config()
        mode = config.get("mode", "paper")

    # Cooldown disabled — trade every session regardless of prior outcome

    # Position sizing — Kelly Criterion
    # Reversal trades use 50% of configured amount (contrarian = smaller size)
    trade_amount = config.get("trade_amount_dollars", 20)
    if _is_reversal:
        trade_amount = trade_amount * 0.50
    avail_liquidity = ob["yes_liquidity"] if side == "yes" else ob["no_liquidity"]
    win_prob_for_kelly = _rev["prob"] if _is_reversal and _rev else brain.get("win_prob", 0.85)
    contracts, kelly_dollars = calculate_contracts(
        trade_amount, int(entry_price_cents), avail_liquidity, win_prob_for_kelly,
        kelly_cap=config.get("kelly_cap", 0.25),
        use_fixed_sizing=config.get("use_fixed_sizing", False),
    )
    if contracts == 0:
        reason = "trade amount too small for current contract price"
        log.info(f"{ticker}: {reason}")
        await _log_entry(market, "READY", secs_left, btc_price, strike,
                         int(entry_price_cents), score, "skip", reason, mode)
        last_action, last_skip_reason = "skip", reason
        return

    # Place order — mark ticker as attempted BEFORE placing so re-entry is blocked
    # even if the bot crashes or fill_confirmed comes back False
    _order_attempted_tickers.add(ticker)
    log.info(f"{ticker}: TRADE {side} {contracts}x @ {int(entry_price_cents)}c (score={score}, mode={mode})")
    result = await place_order(session, ticker, side, contracts, int(entry_price_cents), mode, market)

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
        current_phase = "DONE"
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
        "trade_amount_dollars": round(kelly_dollars, 2),
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
        "claude_confidence": _active_claude_result["confidence"] if _active_claude_result else None,
        "claude_signals":    json.dumps(_active_claude_result["key_signals"]) if _active_claude_result else None,
        "order_id":          order_id,
    }
    trade_id = await db_write_trade(trade_data)

    _entry_ts = time.time()
    _abs_pct_at_entry = abs(btc_price - strike) / strike
    _mins_left_at_entry = secs_left / 60
    _bv3_dist_idx, _bv3_time_idx = _bv3_bucket_indices(_abs_pct_at_entry, _mins_left_at_entry)
    current_position = {
        "trade_id": trade_id,
        "ticker": ticker,
        "side": side,
        "contracts": contracts,
        "entry_price_cents": fill_price,
        "mode": mode,
        "strike": strike,
        "entry_ts": _entry_ts,
        "market_close_time": market.get("close_time", ""),
        "order_id": order_id,
        "_bv3_dist_idx": _bv3_dist_idx,
        "_bv3_time_idx": _bv3_time_idx,
    }
    current_phase = "LOCKED"
    last_action, last_skip_reason = "trade", ""
    log.info(f"{ticker}: LOCKED.")
    mode_icon  = "📄" if mode == "paper" else "💵"
    dir_icon   = "⬆" if side == "yes" else "⬇"
    _win_prob_used = _rev["prob"] if _is_reversal and _rev else brain.get("win_prob", 0)
    _win_pct   = int(_win_prob_used * 100)
    _ev        = round((_win_prob_used - fill_price / 100 - _fee) * 100, 1)
    _ev_str    = f"+{_ev}%" if _ev >= 0 else f"{_ev}%"
    _payout    = round((100 - fill_price) * contracts / 100, 2)
    _cost      = round(fill_price * contracts / 100, 2)
    _time_str  = datetime.now(timezone(timedelta(hours=-7))).strftime("%b %d %I:%M %p PST")
    _strat_tag = "🔄 REVERSAL TRADE" if _is_reversal else "TRADE ENTERED"
    await send_telegram(
        f"{mode_icon} <b>{_strat_tag}</b>  —  {_time_str}\n"
        f"{dir_icon} <b>{side.upper()}</b>  {contracts} contracts @ <b>{fill_price}¢</b>\n"
        f"Cost: ${_cost:.2f}  |  Max payout: ${_payout:.2f}\n"
        f"Win prob: {_win_pct}%  |  EV: {_ev_str}\n"
        f"Strike: ${strike:,.0f}  |  BTC: ${btc_price:,.0f}\n"
        f"Time left: {int(secs_left // 60)}m {int(secs_left % 60)}s  |  <code>{ticker}</code>"
    )


async def handle_locked_phase(
    session: aiohttp.ClientSession,
    btc_price: float,
    secs_left: float,
    config: dict,
) -> None:
    """
    Hold an open position to expiry — exit at settlement.
    Exit only when the market settles and fetch the official Kalshi result.
    secs_left is passed as a fallback; the position's stored close_time is
    used when available so market rollovers don't break expiry detection.
    """
    global current_phase, current_position

    if current_position is None:
        log.warning("LOCKED phase with no position. Moving to DONE.")
        current_phase = "DONE"
        return

    pos = current_position
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
        pnl = (exit_price - pos["entry_price_cents"]) * pos["contracts"] / 100
        profit_pct = (exit_price - pos["entry_price_cents"]) / pos["entry_price_cents"] * 100 \
                     if pos["entry_price_cents"] else 0

        log.info(f"{ticker} expired. Outcome={outcome}, P&L=${pnl:.2f}")

        # Update live BV3 correction table with actual outcome
        _d = pos.get("_bv3_dist_idx")
        _t = pos.get("_bv3_time_idx")
        if _d is not None and _t is not None:
            _update_bv3_correction(_d, _t, outcome == "win")

        await db_update_trade(pos["trade_id"], {
            "exit_price_cents": exit_price,
            "exit_reason": "expiry",
            "outcome": outcome,
            "pnl_dollars": round(pnl, 2),
            "profit_percent": round(profit_pct, 2),
        })

        # Consecutive-loss circuit breaker
        global _consecutive_losses, _consecutive_loss_pause_until
        if outcome == "win":
            _consecutive_losses = 0
        else:
            _consecutive_losses += 1
            max_cl = config.get("max_consecutive_losses", 5)
            if _consecutive_losses >= max_cl:
                _consecutive_loss_pause_until = time.time() + 15 * 60
                log.warning(f"{_consecutive_losses} consecutive losses — pausing trading for 15 min.")
                _resume_str = datetime.fromtimestamp(
                    _consecutive_loss_pause_until,
                    tz=timezone(timedelta(hours=-7))
                ).strftime("%I:%M %p PST")
                await send_telegram(
                    f"⚠️ <b>{_consecutive_losses} consecutive losses</b> — pausing for 15 min.\n"
                    f"Resumes at {_resume_str}"
                )

        result_icon = "✅" if outcome == "win" else "❌"
        pnl_str   = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        pct_str   = f"+{profit_pct:.0f}%" if profit_pct >= 0 else f"{profit_pct:.0f}%"
        mode_icon = "📄" if pos["mode"] == "paper" else "💵"
        _time_str = datetime.now(timezone(timedelta(hours=-7))).strftime("%b %d %I:%M %p PST")
        _dur_secs = int(time.time() - pos.get("entry_ts", time.time()))
        _dur_str  = f"{_dur_secs // 60}m {_dur_secs % 60}s"
        await send_telegram(
            f"{result_icon} <b>{'WIN' if outcome == 'win' else 'LOSS'}  {pnl_str}  ({pct_str})</b>  —  {_time_str}\n"
            f"{mode_icon}  {pos['side'].upper()}  {pos['contracts']} contracts  |  held {_dur_str}\n"
            f"Entry: {pos['entry_price_cents']}¢  →  Expiry: {exit_price}¢\n"
            f"BTC: ${btc_price:,.0f}  vs  Strike: ${pos['strike']:,.0f}  |  <code>{ticker}</code>"
        )

        current_position = None
        current_phase = "DONE"
        return

    # Still in the market — just hold and log
    log.info(
        f"[HOLDING] {ticker} | side={pos['side'].upper()} | entry={pos['entry_price_cents']}c "
        f"| BTC=${btc_price:,.0f} | strike=${pos['strike']:,.0f} | {secs_left:.0f}s left"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Main loop
# ══════════════════════════════════════════════════════════════════════════════

async def main_loop() -> None:
    """
    Permanent 10-second loop driving all trading logic.
    All exceptions are caught per-iteration to prevent crashes.
    """
    global current_market, current_phase, current_position
    global last_confidence_score, last_confidence_breakdown
    global last_action, last_skip_reason
    global _order_attempted_tickers
    global _consecutive_losses, _consecutive_loss_pause_until

    prev_ticker: str | None = None

    # ── Recover open position and consecutive-loss state after a crash/restart ─
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
        # Restore consecutive-loss counter so the pause survives a restart.
        saved_cl = _saved.get("consecutive_losses", 0)
        saved_pause = _saved.get("consecutive_loss_pause_until")
        if isinstance(saved_cl, int) and saved_cl > 0:
            _consecutive_losses = saved_cl
            if saved_pause and time.time() < saved_pause:
                _consecutive_loss_pause_until = saved_pause
                log.warning(
                    f"Restored consecutive-loss state: count={saved_cl}, "
                    f"pause until {datetime.fromtimestamp(saved_pause, tz=timezone.utc).isoformat()}"
                )
            else:
                _consecutive_loss_pause_until = None
    except Exception:
        pass  # fresh start, no state to recover

    # TCPConnector with keepalive_timeout prevents stale pooled connections
    # from silently breaking API calls after many hours of uptime.
    connector = aiohttp.TCPConnector(keepalive_timeout=30, limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            try:
                midnight_reset()

                # Re-calibrate every 5 completed trades
                try:
                    async with aiosqlite.connect(_DB_FILE) as _cal_db:
                        await _cal_db.execute("PRAGMA journal_mode=WAL")
                        async with _cal_db.execute(
                            "SELECT COUNT(*) FROM trades WHERE outcome IN ('win','loss')"
                        ) as _cal_cur:
                            _cal_row = await _cal_cur.fetchone()
                    completed = _cal_row[0] if _cal_row else 0
                    if completed >= _adaptive["last_calibrated_count"] + 20:
                        await calibrate_from_history()
                    if completed >= _brain_cal["last_count"] + 5:
                        await calibrate_brain()
                except Exception:
                    pass

                # Fresh config read
                try:
                    config = read_config()
                except Exception as exc:
                    log.error(f"Config read error: {exc}")
                    await asyncio.sleep(10)
                    continue

                if not config.get("bot_enabled", True):
                    await write_state_file(config, current_market, "PAUSED", 0,
                                           get_btc_price(), last_confidence_score,
                                           last_confidence_breakdown, last_action, last_skip_reason)
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
                if btc_prices and (time.time() - btc_prices[-1][0]) > 60:
                    age = int(time.time() - btc_prices[-1][0])
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
                    log.warning(f"{ticker}: cannot parse strike. Skipping cycle.")
                    await asyncio.sleep(10)
                    continue

                # ── WATCH ──────────────────────────────────────────────────
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

                # ── LOCKED ─────────────────────────────────────────────────
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

                # ── DONE ───────────────────────────────────────────────────
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

                # ── READY ──────────────────────────────────────────────────
                if current_phase == "READY":
                    # Consecutive-loss pause check
                    if _consecutive_loss_pause_until and time.time() < _consecutive_loss_pause_until:
                        _resume_in = int(_consecutive_loss_pause_until - time.time())
                        log.info(f"Consecutive-loss pause active — {_resume_in}s remaining. Skipping READY.")
                        await write_state_file(config, market, current_phase, secs_left, btc_price,
                                               last_confidence_score, last_confidence_breakdown,
                                               "skip", f"consecutive_loss_pause ({_resume_in}s left)")
                        await asyncio.sleep(10)
                        continue
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


# ══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════════

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

    # ── Market discovery: log everything BTC-related so we can find the right ticker ──
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

    # ── Check 1: Price validation data ────────────────────────────────────────
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

    # ── Check 2: Fee constant is set and > 0 ──────────────────────────────────
    fee = config.get("kalshi_fee_per_contract_cents", 0)
    if not (isinstance(fee, (int, float)) and fee > 0):
        issues.append(
            f"FEE NOT CONFIGURED — kalshi_fee_per_contract_cents={fee!r}. "
            "Set to 7 (Kalshi charges 7c/contract)."
        )

    # ── Check 3: Daily loss limit is real ─────────────────────────────────────
    dll = config.get("daily_loss_limit_dollars", 999999)
    if dll > 500:
        issues.append(
            f"DAILY LOSS LIMIT TOO HIGH — currently ${dll}. "
            "Set to a realistic value (e.g. $50)."
        )

    # ── Check 4: mode gate ────────────────────────────────────────────────────
    mode      = config.get("mode", "paper")
    is_live   = (mode == "live")
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
            "\U0001f6a8 <b>LIVE TRADING BLOCKED — pre-flight failed</b>\n"
            + "\n".join(f"• {i}" for i in issues)
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
                 f"claude={'ON' if config.get('enable_claude_filter') else 'OFF'}  "
                 f"reversal={'ON' if config.get('enable_reversal_signal') else 'OFF'}")
        log.info("=" * W)


async def main() -> None:
    """Bootstrap: load credentials, init DB, start BTC feed, run main loop."""
    _init_config()
    load_credentials()
    init_db()
    _load_bv3_corrections()
    _init_claude()

    # Verify Kalshi credentials and log account balance before doing anything
    async with aiohttp.ClientSession() as verify_session:
        await verify_kalshi_connection(verify_session)

    # Start the BTC WebSocket feed as a background task
    asyncio.create_task(btc_feed_task())

    # Wait for the first BTC price — retry indefinitely so a transient network
    # hiccup on startup doesn't kill the process and burn a Railway restart credit.
    log.info("Waiting for BTC price feed...")
    waited = 0
    while get_btc_price() is None:
        await asyncio.sleep(1)
        waited += 1
        if waited % 30 == 0:
            log.warning(f"Still waiting for BTC price feed ({waited}s elapsed)...")
    log.info(f"BTC feed ready after {waited}s. Current price: ${get_btc_price():,.2f}")
    _startup_cfg = read_config()
    await send_telegram(f"🤖 <b>Printer bot started</b>\nBTC: ${get_btc_price():,.2f}\nMode: {_startup_cfg.get('mode','?').upper()}  |  Bot enabled: {_startup_cfg.get('bot_enabled', False)}")

    # Pre-flight check runs once before trading begins.
    # LIVE mode with unresolved issues → sys.exit(1). Paper mode → warn and continue.
    await run_preflight_checks(_startup_cfg)

    await main_loop()


if __name__ == "__main__":
    asyncio.run(main())
