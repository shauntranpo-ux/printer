"""
bot.py — Core trading logic for the Kalshi BTC 15-minute prediction market bot.

Connects to Coinbase WebSocket for live BTC prices, polls Kalshi for the
soonest-expiring BTC 15-minute market, evaluates a four-component confidence
score, places paper or live orders, monitors stop loss, and enforces daily
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
import time
from base64 import b64encode
from collections import deque
from datetime import datetime, timezone

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
WATCH_PHASE_SECONDS = 0   # evaluate immediately when a session starts

# ── Strategy constants — hardcoded so Railway deploys never revert them ───────
CONFIDENCE_THRESHOLD = 67   # minimum win probability % to enter a trade
STOP_LOSS_PERCENT    = 45   # exit if contract bid drops this % from entry price

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

# Market cache
_market_cache: dict | None = None
_market_cache_ts: float = 0.0

# Daily-limit tracking
limit_triggered: bool = False
limit_reason: str = ""
pre_limit_mode: str | None = None
daily_reset_date = None

# Cooldown
cooldown_counter: int = 0

# Last evaluation info (written to state file)
last_confidence_score: int = 0
last_confidence_breakdown: dict = {}
last_action: str = ""
last_skip_reason: str = ""

# Contract price history — tracks YES ask over time per ticker for velocity signal
_contract_price_history: dict = {}   # ticker → deque[(ts, price)]

# Claude AI client
_claude_client = None       # AsyncAnthropic instance, set in main()
_claude_cache: dict = {}    # cache_key → {ts, result}
CLAUDE_CACHE_TTL = 25       # seconds between Claude calls for same market+side

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
    "min_edge_override": None,  # if set, overrides the 6% default
    "confidence_bonus":  0,     # added to confidence score as a reward
    "reward_tier":       0,     # 0=none 1=good(>50%) 2=great(>75%) 3=max(>85%)
    "overall_wr":        0.0,   # tracked for dashboard display
    # condition win rates: key = "dist_time_mom" → [wins, total]
    "condition_wr":      {},
    # momentum direction performance
    "bullish_wr":        0.5,
    "bearish_wr":        0.5,
}


# ══════════════════════════════════════════════════════════════════════════════
#  Config helpers
# ══════════════════════════════════════════════════════════════════════════════

def read_config() -> dict:
    """Read and return the contents of the config file."""
    with open(_CONFIG_FILE, "r") as fh:
        return json.load(fh)


def write_config(data: dict) -> None:
    """Write a dict to the config file atomically."""
    with open(_CONFIG_FILE, "w") as fh:
        json.dump(data, fh, indent=2)


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
        "bot_enabled": True,
        "trade_amount_dollars": 10,
        "mode": "live",
        "daily_loss_limit_dollars": 50,
        "daily_profit_target_dollars": 9999999,
        "claude_enabled": False,
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
                stop_loss_price_cents INTEGER,
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

        # Migrate existing DB — add Claude columns if not present
        for col, typedef in (("claude_confidence", "INTEGER"), ("claude_signals", "TEXT")):
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


def _db_conn() -> sqlite3.Connection:
    """Open a WAL-mode SQLite connection."""
    conn = sqlite3.connect(_DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def db_write_trade(trade: dict) -> int | None:
    """Insert a trade record. Returns the new row id."""
    try:
        conn = _db_conn()
        c = conn.cursor()
        c.execute("""
            INSERT INTO trades (
                ts, market_id, market_title, mode, side, contracts,
                entry_price_cents, trade_amount_dollars, confidence_score,
                model_prob, implied_prob, btc_price_at_entry, strike,
                seconds_left_at_entry, fill_confirmed, stop_loss_price_cents,
                exit_price_cents, exit_reason, outcome, pnl_dollars, profit_percent,
                claude_confidence, claude_signals
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            trade.get("ts"), trade.get("market_id"), trade.get("market_title"),
            trade.get("mode"), trade.get("side"), trade.get("contracts"),
            trade.get("entry_price_cents"), trade.get("trade_amount_dollars"),
            trade.get("confidence_score"), trade.get("model_prob"),
            trade.get("implied_prob"), trade.get("btc_price_at_entry"),
            trade.get("strike"), trade.get("seconds_left_at_entry"),
            trade.get("fill_confirmed"), trade.get("stop_loss_price_cents"),
            trade.get("exit_price_cents"), trade.get("exit_reason"),
            trade.get("outcome", "pending"), trade.get("pnl_dollars"),
            trade.get("profit_percent"),
            trade.get("claude_confidence"), trade.get("claude_signals"),
        ))
        row_id = c.lastrowid
        conn.commit()
        conn.close()
        return row_id
    except Exception as exc:
        log.error(f"DB write_trade error: {exc}")
        return None


def db_update_trade(trade_id: int, fields: dict) -> None:
    """Update named columns on an existing trade row."""
    try:
        conn = _db_conn()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE trades SET {set_clause} WHERE id = ?",
            list(fields.values()) + [trade_id],
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        log.error(f"DB update_trade error: {exc}")


def db_write_market_log(entry: dict) -> None:
    """Append one row to market_log."""
    try:
        conn = _db_conn()
        conn.execute("""
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
        conn.commit()
        conn.close()
    except Exception as exc:
        log.error(f"DB write_market_log error: {exc}")


def db_get_today_pnl(mode: str) -> float:
    """Sum pnl_dollars for completed trades in the given mode today (UTC)."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        conn = _db_conn()
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl_dollars), 0) FROM trades "
            "WHERE mode = ? AND ts LIKE ? AND outcome != 'pending'",
            (mode, f"{today}%"),
        ).fetchone()
        conn.close()
        return float(row[0]) if row else 0.0
    except Exception as exc:
        log.error(f"DB get_today_pnl error: {exc}")
        return 0.0


# ══════════════════════════════════════════════════════════════════════════════
#  Kalshi auth
# ══════════════════════════════════════════════════════════════════════════════

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
        private_key.sign(msg, padding.PKCS1v15(), hashes.SHA256())
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
                                btc_prices.append((time.time(), price))
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

async def fetch_current_market(session: aiohttp.ClientSession) -> dict | None:
    """
    Fetch the soonest-expiring open BTC 15-minute market from Kalshi.
    Results are cached for MARKET_CACHE_TTL seconds to avoid hammering the API.

    Returns:
        Market dict, or None if no markets are available.
    """
    global _market_cache, _market_cache_ts

    now = time.time()
    if _market_cache and (now - _market_cache_ts) < MARKET_CACHE_TTL:
        return _market_cache

    path = "/markets"
    # Try known Kalshi BTC series tickers; fall back to no filter
    all_markets = []
    for series in ("KXBTC15M", "BTCD", "KXBTC", "BTC"):
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
            if batch:
                log.info(f"Series {series!r} returned {len(batch)} markets: "
                         + ", ".join(m.get("ticker", "?") for m in batch[:5]))
                all_markets.extend(batch)
                break   # found markets — stop trying other series
        except Exception as exc:
            log.error(f"Market fetch error (series={series}): {exc}")

    if not all_markets:
        log.warning("All series tickers returned no markets.")
        return _market_cache

    # Log all markets with their close times so we can see what's available
    now_utc = datetime.now(timezone.utc)
    for m in all_markets:
        try:
            close_dt = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
            mins_left = (close_dt - now_utc).total_seconds() / 60
        except Exception:
            mins_left = -1
        log.info(f"  Market: {m.get('ticker')} | closes in {mins_left:.1f}m | {m.get('title','')[:60]}")

    # Try to identify 15-minute markets by open→close duration
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

    fifteen = [m for m in all_markets if market_duration_minutes(m) is not None
               and 13 <= market_duration_minutes(m) <= 17]

    if fifteen:
        log.info(f"Found {len(fifteen)} 15-min market(s) by duration.")
        pool = fifteen
    else:
        # Fall back: markets closing within 20 minutes (soonest active window)
        soon = [
            m for m in all_markets
            if (lambda c: 0 < c <= 20)(
                (datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")) - now_utc).total_seconds() / 60
                if m.get("close_time") else -1
            )
        ]
        if soon:
            log.info(f"No 15-min duration match — using {len(soon)} markets closing within 20 min.")
            pool = soon
        else:
            log.warning(f"No short-window markets found. Using soonest-expiring of {len(all_markets)} total.")
            pool = all_markets

    pool.sort(key=lambda m: m.get("close_time", ""))
    _market_cache = pool[0]
    _market_cache_ts = now
    log.info(f"Active market: {_market_cache.get('ticker')} | {_market_cache.get('title')} | closes {_market_cache.get('close_time')}")
    return _market_cache


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
    """Estimate seconds since market open (assumes 15-minute duration)."""
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
    if not (1 <= best_yes_ask <= 99 and 1 <= best_no_ask <= 99):
        log.error(
            f"Orderbook sanity FAIL for {ticker}: "
            f"yes_ask={best_yes_ask}c no_ask={best_no_ask}c — out of [1,99], skipping"
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

    return {
        "best_yes_ask": best_yes_ask,
        "best_no_ask":  best_no_ask,
        "best_yes_bid": best_yes_bid,  # may be None — only used for SL monitoring
        "yes_liquidity": yes_liquidity,
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

def calibrate_from_history() -> None:
    """
    Analyse completed paper + live trades to learn what conditions win.
    Updates _adaptive weights. Runs automatically every 20 completed trades.
    """
    global _adaptive
    try:
        conn = _db_conn()
        rows = conn.execute("""
            SELECT entry_price_cents, seconds_left_at_entry, outcome
            FROM trades WHERE outcome IN ('win','loss')
            ORDER BY ts DESC LIMIT 100
        """).fetchall()
        conn.close()

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


def calibrate_brain() -> None:
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
        conn = _db_conn()
        rows = conn.execute("""
            SELECT side, entry_price_cents, seconds_left_at_entry,
                   btc_price_at_entry, strike, outcome, model_prob
            FROM trades
            WHERE outcome IN ('win','loss')
            ORDER BY ts DESC LIMIT 200
        """).fetchall()
        conn.close()

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
                # Floor at 0.85 — the table is built on 7.4M rows; trust it
                _brain_cal["prob_scale"] = max(0.85, min(1.5, _brain_cal["prob_scale"]))

        # ── 2. Win-rate reward tiers (priority = win rate, not profit) ───────────
        #   Tier 3 (≥85%) — MAX reward: lowest edge bar, biggest confidence bonus
        #   Tier 2 (≥75%) — HUGE reward: very low bar, large bonus
        #   Tier 1 (≥50%) — reward: lower bar, small bonus
        #   Below 50%     — tighten up, no bonus
        _brain_cal["overall_wr"] = overall_wr
        if overall_wr >= 0.85:
            _brain_cal["reward_tier"]       = 3
            _brain_cal["min_edge_override"] = 0.10   # proven accurate — lower bar early
            _brain_cal["confidence_bonus"]  = 25
            tier_label = "TIER 3 MAX REWARD"
        elif overall_wr >= 0.75:
            _brain_cal["reward_tier"]       = 2
            _brain_cal["min_edge_override"] = 0.12
            _brain_cal["confidence_bonus"]  = 15
            tier_label = "TIER 2 HUGE REWARD"
        elif overall_wr >= 0.50:
            _brain_cal["reward_tier"]       = 1
            _brain_cal["min_edge_override"] = 0.15   # same as default
            _brain_cal["confidence_bonus"]  = 5
            tier_label = "TIER 1 REWARD"
        elif overall_wr >= 0.40:
            _brain_cal["reward_tier"]       = 0
            _brain_cal["min_edge_override"] = 0.18   # tighten slightly
            _brain_cal["confidence_bonus"]  = 0
            tier_label = "no reward (learning)"
        else:
            _brain_cal["reward_tier"]       = 0
            _brain_cal["min_edge_override"] = 0.20   # losing — require real edge early
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
        conn2 = _db_conn()
        claude_rows = conn2.execute("""
            SELECT claude_confidence, outcome FROM trades
            WHERE outcome IN ('win','loss') AND claude_confidence IS NOT NULL
            ORDER BY ts DESC LIMIT 50
        """).fetchall()
        conn2.close()

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
    log.info("Claude AI client ready (claude-opus-4-6 with adaptive thinking).")


def _recent_trades_for_claude() -> list:
    """Return the last 15 completed trades as plain dicts for the Claude prompt."""
    try:
        conn = _db_conn()
        rows = conn.execute("""
            SELECT ts, side, entry_price_cents, seconds_left_at_entry,
                   btc_price_at_entry, strike, outcome, pnl_dollars, confidence_score
            FROM trades WHERE outcome IN ('win','loss')
            ORDER BY ts DESC LIMIT 15
        """).fetchall()
        conn.close()
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


async def claude_analysis(
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
    recent_trades = _recent_trades_for_claude()

    system_prompt = (
        "You are an expert quantitative trader specialising in short-duration binary prediction markets on Kalshi. "
        "A YES contract pays $1 if BTC closes ABOVE the strike. A NO contract pays $1 if BTC closes AT OR BELOW the strike. "
        "You receive the cost of each side and decide: which side (if any) has genuine edge right now? "
        "Be aggressive — only skip when there is truly no edge. If a contract is mispriced, take it. "
        "Respond with valid JSON only."
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
  3-min momentum: {mom_label} ({mom_pct*100:+.3f}%)

PRINTER BRAIN STATE (your decisions will be stored and feed back into this):
  Overall win rate:     {_brain_cal['overall_wr']:.0%}
  Reward tier:          {_brain_cal['reward_tier']} / 3  (tier 3 = ≥85% WR = max reward)
  Min EV required:      {(_brain_cal['min_edge_override'] or 0.15):.0%} early / 8% last 3min
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
            model="claude-opus-4-6",
            max_tokens=1024,
            thinking={"type": "adaptive"},
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
#  Printer Brain v3 — Empirically Calibrated from 7.4M rows of BTC 1-min data
# ══════════════════════════════════════════════════════════════════════════════

# Empirical win-probability table: P(BTC stays on same side at window close)
# Derived from backtest of 7.4M rows btcusd_1-min_data.csv (2020-2026 regime)
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

    return row[t_low] + (row[t_high] - row[t_low]) * frac


def printer_brain(
    btc_price: float,
    strike: float,
    yes_ask: float,
    no_ask: float,
    elapsed_seconds: float,
    secs_left: float,
    ticker: str,
) -> dict:
    """
    Printer Brain v3 — empirically calibrated from 7.4M rows BTC 1-min data.

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

    # ── 6. Expected value vs actual contract price ────────────────────────────
    # yes_ask / no_ask are in cents (0-100). $1 payout.
    yes_ev = prob_yes - (yes_ask / 100)
    no_ev  = prob_no  - (no_ask  / 100)

    # Directional track record: penalise if a direction has been losing badly
    if _brain_cal["bullish_wr"] < 0.35: yes_ev -= 0.04
    if _brain_cal["bearish_wr"] < 0.35: no_ev  -= 0.04

    # ── 7. Pick side — always bet WITH BTC's current position ────────────────
    # The entire strategy is "BTC stays where it is." Never bet against it
    # just because the opposite contract happens to be cheap — those are
    # low-probability lottery tickets that flip the loss rate.
    if above:
        side, best_ev, entry_c, true_p = "yes", yes_ev, yes_ask, prob_yes
    else:
        side, best_ev, entry_c, true_p = "no",  no_ev,  no_ask,  prob_no

    # ── 8. EV filter — dynamic threshold based on time remaining ─────────────
    # Early/mid session (>3 min left): require 15% EV — only strong edges.
    # Late session (≤3 min left): accept 8% EV — near expiry, win probability
    # is near-certain but markets reprice slowly, so edge exists even at lower EV.
    # This lets the bot find a trade in almost every session's final minutes
    # while staying selective early when outcomes are still uncertain.
    base_ev = _brain_cal["min_edge_override"] if _brain_cal["min_edge_override"] is not None else 0.15
    if secs_left < 3 * 60:
        min_ev = min(base_ev, 0.08)   # last 3 min: lower bar, near-certain outcomes
    else:
        min_ev = base_ev

    skip_reason = ""
    if best_ev < min_ev:
        skip_reason = (
            f"EV {best_ev:+.1%} below {min_ev:.0%} minimum | "
            f"{side.upper()} at {entry_c:.0f}c, win prob {true_p:.1%}"
        )

    action = "skip" if skip_reason else "trade"

    # ── 9. Confidence = win probability 0-100 ────────────────────────────────
    confidence = min(99, max(0, int(true_p * 100) + _brain_cal["confidence_bonus"]))

    if action == "trade":
        reasoning = (
            f"BTC is {abs_pct*100:.2f}% {'above' if above else 'below'} strike "
            f"with {mins_left:.1f} min left. Empirical win prob: {true_p:.1%} "
            f"(raw {win_prob_raw:.1%} + mom {mom_adj:+.1%}). "
            f"Contract {entry_c:.0f}c -> EV {best_ev:+.1%}. "
            f"Momentum: {mom_label} ({mom_pct*100:+.2f}%)."
        )
    else:
        reasoning = skip_reason or f"No EV edge. Best: {best_ev:+.1%} (need {min_ev:.0%})"

    key_signals = [
        f"BTC {pct_above*100:+.2f}% from strike | {mins_left:.1f} min left",
        f"Win prob: YES={prob_yes:.1%}  NO={prob_no:.1%}  (raw={win_prob_raw:.1%})",
        f"EV: YES={yes_ev:+.1%}  NO={no_ev:+.1%}  (min {min_ev:.0%})",
        f"Momentum: {mom_label} ({mom_pct*100:+.2f}%) | Velocity: {vel_signal}",
    ]

    brain_log.info(
        f"{action.upper():5} {side.upper():3} conf={confidence:3} | "
        f"dist={abs_pct*100:.3f}% {'UP' if above else 'DN'} | "
        f"ev={best_ev:+.1%} | prob={true_p:.1%} raw={win_prob_raw:.1%} mom={mom_adj:+.1%} | "
        f"contract={entry_c:.0f}c | {mom_label} {mins_left:.1f}min"
    )
    return {"action": action, "side": side, "confidence": confidence,
            "reasoning": reasoning, "key_signals": key_signals,
            "win_prob": float(true_p),
            "mom_label": mom_label, "vel_signal": vel_signal,
            "mins_left": mins_left, "abs_pct": abs_pct}



# ══════════════════════════════════════════════════════════════════════════════
#  Position sizing
# ══════════════════════════════════════════════════════════════════════════════

def calculate_contracts(
    trade_amount_dollars: float,
    entry_price_cents: int,
    liquidity: int,
) -> int:
    """
    Calculate contract count for a trade.

    Returns:
        Number of contracts (0 if trade amount is too small for even one contract).
    """
    trade_cents = trade_amount_dollars * 100
    contracts = int(trade_cents / entry_price_cents)  # floor division
    contracts = min(contracts, liquidity)             # cap at available liquidity
    return contracts


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
) -> dict:
    """
    Place a fill-or-kill limit order on Kalshi.
    Retries up to 3 times, bumping price by 1c each attempt to ensure fill.
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

    for attempt in range(3):
        price_this_attempt = min(99, entry_price_cents + attempt)
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
            "time_in_force": "fill_or_kill",
        }

        if attempt > 0:
            log.info(f"Order retry {attempt}/2 at {price_this_attempt}c...")
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
            continue

        if http_status not in (200, 201):
            log.error(f"Order HTTP {http_status}: {data}")
            continue

        order_id = (data.get("order") or {}).get("order_id") or data.get("order_id")
        if not order_id:
            log.error(f"No order_id in response: {data}")
            continue

        # Confirm fill status
        check_path = f"/portfolio/orders/{order_id}"
        try:
            async with session.get(
                KALSHI_BASE_URL + check_path,
                headers=kalshi_headers("GET", check_path),
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as resp:
                order_data = await resp.json()
        except Exception as exc:
            log.error(f"Order status check error: {exc}")
            return {"fill_confirmed": False, "fill_price_cents": None, "order_id": order_id}

        order = order_data.get("order", order_data)
        status = order.get("status", "")

        if status == "filled":
            fill_price = order.get("yes_price", price_this_attempt)
            log.info(f"Order FILLED: {order_id} @ {fill_price}c (attempt {attempt})")
            return {"fill_confirmed": True, "fill_price_cents": fill_price, "order_id": order_id}

        log.warning(f"Order {order_id} status={status!r} — not filled on attempt {attempt}")
        # Cancel before retry
        cancel_path = f"/portfolio/orders/{order_id}"
        try:
            async with session.delete(
                KALSHI_BASE_URL + cancel_path,
                headers=kalshi_headers("DELETE", cancel_path),
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as resp:
                await resp.read()
        except Exception as exc:
            log.error(f"Order cancel error: {exc}")

    log.error(f"Order not filled after 3 attempts for {ticker} {side}@{entry_price_cents}c")
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

def check_daily_limits(config: dict) -> tuple[bool, str]:
    """
    Check daily loss limit and profit target for live mode.
    Switches mode to 'paper' in config.json and memory when triggered.

    Returns:
        (triggered: bool, reason: str)
    """
    global limit_triggered, limit_reason, pre_limit_mode

    if config.get("mode") == "paper":
        return False, ""

    live_pnl = db_get_today_pnl("live")

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

def write_state_file(
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
        "mode": config.get("mode", "paper"),
        "today_live_pnl": db_get_today_pnl("live"),
        "today_paper_pnl": db_get_today_pnl("paper"),
        "config": config,
        "cooldown_counter": cooldown_counter,
        "limit_triggered": limit_triggered,
        "limit_reason": limit_reason,
        "open_position": current_position,
    }
    try:
        with open(_STATE_FILE, "w") as fh:
            json.dump(state, fh)
    except Exception as exc:
        log.error(f"State file write error: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
#  Phase handlers
# ══════════════════════════════════════════════════════════════════════════════

def _log_entry(
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
    db_write_market_log({
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
    global last_action, last_skip_reason, cooldown_counter

    mode = config.get("mode", "paper")

    # Hard expiry gate — truly nothing to do in the last 90 seconds
    if secs_left < 90:
        log.info(f"{ticker}: < 90s remaining. Moving to DONE.")
        current_phase = "DONE"
        return

    # Orderbook — retry next cycle if temporarily unavailable
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

    # Track YES price for velocity signal
    track_contract_price(ticker, yes_ask)

    # ── Printer Brain — primary decision engine (always runs, no API needed) ──
    brain = printer_brain(btc_price, strike, yes_ask, no_ask, elapsed, secs_left, ticker)
    side     = brain["side"]
    score    = brain["confidence"]
    do_trade = brain["action"] == "trade"
    skip_reason_ai = brain["reasoning"]

    # ── Claude — optional override (only if enabled in config) ───────────────
    _active_claude_result = None
    if config.get("claude_enabled", False):
        claude_result = await claude_analysis(
            btc_price, strike, yes_ask, no_ask, elapsed, secs_left, ticker
        )
        if claude_result is not None:
            _active_claude_result = claude_result
            side     = claude_result["side"]
            score    = claude_result["confidence"]
            do_trade = claude_result["action"] == "trade"
            skip_reason_ai = claude_result.get("reasoning", "")

    entry_price_cents = yes_ask if side == "yes" else no_ask

    # Dashboard breakdown from Brain v3 components
    win_p_raw = _empirical_win_prob(abs((btc_price - strike) / strike), secs_left / 60)
    breakdown = {
        "win_prob_raw":  round(win_p_raw * 100, 1),
        "win_prob_final": round(brain.get("win_prob", win_p_raw) * 100, 1),
        "ev":            round((brain.get("win_prob", 0.5) - entry_price_cents / 100) * 100, 1),
        "contract_c":    round(entry_price_cents, 1),
        "momentum":      brain.get("mom_label", "neutral"),
        "velocity":      brain.get("vel_signal", "neutral"),
        "time":          round(brain.get("mins_left", secs_left / 60), 1),
        "distance":      round(brain.get("abs_pct", abs((btc_price - strike) / strike)) * 100, 3),
    }

    last_confidence_score = score
    last_confidence_breakdown = breakdown

    # Confidence threshold gate (matches backtest min_confidence param)
    min_score = CONFIDENCE_THRESHOLD
    if do_trade and score < min_score:
        skip_reason_ai = f"confidence {score} < threshold {min_score}"
        do_trade = False

    if not do_trade:
        log.info(f"{ticker}: watching — {skip_reason_ai}")
        _log_entry(market, "READY", secs_left, btc_price, strike,
                   int(entry_price_cents), score, "skip", skip_reason_ai, mode)
        last_action, last_skip_reason = "watching", skip_reason_ai
        return

    # Daily limits — may flip mode to paper
    limit_hit, _ = check_daily_limits(config)
    if limit_hit:
        config = read_config()
        mode = config.get("mode", "paper")

    # Cooldown disabled — trade every session regardless of prior outcome

    # Position sizing
    trade_amount = config.get("trade_amount_dollars", 20)
    contracts = calculate_contracts(trade_amount, int(entry_price_cents), ob["yes_liquidity"])
    if contracts == 0:
        reason = "trade amount too small for current contract price"
        log.info(f"{ticker}: {reason}")
        _log_entry(market, "READY", secs_left, btc_price, strike,
                   int(entry_price_cents), score, "skip", reason, mode)
        last_action, last_skip_reason = "skip", reason
        return

    # Place order
    log.info(f"{ticker}: TRADE {side} {contracts}x @ {int(entry_price_cents)}c (score={score}, mode={mode})")
    result = await place_order(session, ticker, side, contracts, int(entry_price_cents), mode)

    fill_confirmed = result["fill_confirmed"]
    fill_price = result.get("fill_price_cents") or int(entry_price_cents)
    order_id = result.get("order_id")

    stop_pct = STOP_LOSS_PERCENT
    sl_price = int(fill_price * (1 - stop_pct / 100))
    trade_ts = datetime.now(timezone.utc).isoformat()

    trade_data = {
        "ts": trade_ts,
        "market_id": ticker,
        "market_title": market.get("title", ""),
        "mode": mode,
        "side": side,
        "contracts": contracts,
        "entry_price_cents": fill_price,
        "trade_amount_dollars": trade_amount,
        "confidence_score": score,
        "model_prob": brain.get("win_prob", 0.5),
        "implied_prob": implied_prob(entry_price_cents),
        "btc_price_at_entry": btc_price,
        "strike": strike,
        "seconds_left_at_entry": int(secs_left),
        "fill_confirmed": 1 if fill_confirmed else 0,
        "stop_loss_price_cents": sl_price,
        "exit_price_cents": None,
        "exit_reason": None,
        "outcome": "pending",
        "pnl_dollars": None,
        "profit_percent": None,
        # Store Claude's view at time of trade so brain can learn from it later
        "claude_confidence": _active_claude_result["confidence"] if _active_claude_result else None,
        "claude_signals":    json.dumps(_active_claude_result["key_signals"]) if _active_claude_result else None,
    }
    trade_id = db_write_trade(trade_data)

    _log_entry(
        market, "READY", secs_left, btc_price, strike, int(entry_price_cents),
        score, "trade" if fill_confirmed else "skip",
        "" if fill_confirmed else "order not filled",
        mode,
    )

    if fill_confirmed:
        current_position = {
            "trade_id": trade_id,
            "ticker": ticker,
            "side": side,
            "contracts": contracts,
            "entry_price_cents": fill_price,
            "stop_loss_price_cents": sl_price,
            "mode": mode,
            "strike": strike,
        }
        current_phase = "LOCKED"
        last_action, last_skip_reason = "trade", ""
        log.info(f"{ticker}: LOCKED. SL={sl_price}c")
    else:
        current_phase = "DONE"
        last_action, last_skip_reason = "skip", "order not filled"
        log.info(f"{ticker}: order not filled. Moving to DONE.")


async def handle_locked_phase(
    session: aiohttp.ClientSession,
    config: dict,
    market: dict,
    ticker: str,
    btc_price: float,
    secs_left: float,
    strike: float,
) -> None:
    """
    Monitor an open position for stop loss or expiry every 15 seconds.
    Two consecutive stop-loss checks below the trigger price are required
    before exiting to avoid false triggers in thin markets.
    """
    global current_phase, current_position, cooldown_counter

    if current_position is None:
        log.warning("LOCKED phase with no position. Moving to DONE.")
        current_phase = "DONE"
        return

    pos = current_position
    now = time.time()

    # Expiry check
    if secs_left <= 0:
        outcome = "win" if (
            (pos["side"] == "yes" and btc_price > pos["strike"]) or
            (pos["side"] == "no"  and btc_price <= pos["strike"])
        ) else "loss"
        exit_price = 100 if outcome == "win" else 0
        pnl = (exit_price - pos["entry_price_cents"]) * pos["contracts"] / 100
        profit_pct = (exit_price - pos["entry_price_cents"]) / pos["entry_price_cents"] * 100 \
                     if pos["entry_price_cents"] else 0

        log.info(f"{ticker} expired. Outcome={outcome}, P&L=${pnl:.2f}")
        db_update_trade(pos["trade_id"], {
            "exit_price_cents": exit_price,
            "exit_reason": "expiry",
            "outcome": outcome,
            "pnl_dollars": round(pnl, 2),
            "profit_percent": round(profit_pct, 2),
        })

        # Cooldown disabled — no sit-out after losses

        current_position = None
        current_phase = "DONE"
        return

    # Fetch orderbook for stop-loss price check
    try:
        ob = await fetch_orderbook(session, ticker, market)
    except Exception as exc:
        log.error(f"Orderbook error in LOCKED: {exc}")
        return

    if ob is None:
        return

    if pos["side"] == "yes":
        # YES bid: prefer yes_bid directly; fall back to 100 - no_ask
        current_bid = ob["best_yes_bid"] if ob.get("best_yes_bid") is not None else (100 - ob["best_no_ask"])
    else:
        # NO bid: what we'd receive selling NO = 100 - yes_ask
        current_bid = 100 - ob["best_yes_ask"]
    unrealized = (current_bid - pos["entry_price_cents"]) * pos["contracts"] / 100
    log.info(
        f"[LOCKED] {ticker} | BTC={btc_price:.0f} | bid={current_bid}c | "
        f"unrealized=${unrealized:.2f} | SL={pos['stop_loss_price_cents']}c"
    )

    # Determine exit reason — standard SL OR late-stage bail-out
    exit_reason = None
    if current_bid <= pos["stop_loss_price_cents"]:
        exit_reason = "stop_loss"
        log.warning(
            f"Stop loss triggered for {ticker}: "
            f"bid={current_bid}c <= SL={pos['stop_loss_price_cents']}c"
        )
    elif secs_left <= 120 and current_bid < pos["entry_price_cents"] * 0.6:
        # Last 2 minutes and down >40% from entry — get out, don't ride to zero
        exit_reason = "late_bail"
        log.warning(
            f"Late bail for {ticker}: {secs_left:.0f}s left, "
            f"bid={current_bid}c < 60% of entry {pos['entry_price_cents']}c"
        )

    if exit_reason:
        exit_price = await sell_position(
            session, ticker, pos["side"], pos["contracts"], pos["mode"], current_bid
        )
        pnl = (exit_price - pos["entry_price_cents"]) * pos["contracts"] / 100
        profit_pct = (exit_price - pos["entry_price_cents"]) / pos["entry_price_cents"] * 100 \
                     if pos["entry_price_cents"] else 0

        db_update_trade(pos["trade_id"], {
            "exit_price_cents": exit_price,
            "exit_reason": exit_reason,
            "outcome": "loss",
            "pnl_dollars": round(pnl, 2),
            "profit_percent": round(profit_pct, 2),
        })

        # Cooldown disabled — re-enter immediately after stop loss or exit

        current_position = None
        current_phase = "DONE"


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

    prev_ticker: str | None = None

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                midnight_reset()

                # Re-calibrate every 5 completed trades
                try:
                    conn = _db_conn()
                    completed = conn.execute(
                        "SELECT COUNT(*) FROM trades WHERE outcome IN ('win','loss')"
                    ).fetchone()[0]
                    conn.close()
                    if completed >= _adaptive["last_calibrated_count"] + 20:
                        calibrate_from_history()
                    if completed >= _brain_cal["last_count"] + 5:
                        calibrate_brain()
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
                    write_state_file(config, current_market, "PAUSED", 0,
                                     get_btc_price(), last_confidence_score,
                                     last_confidence_breakdown, last_action, last_skip_reason)
                    await asyncio.sleep(10)
                    continue

                btc_price = get_btc_price()
                if btc_price is None:
                    log.warning("Waiting for BTC price...")
                    write_state_file(config, current_market, current_phase, 0,
                                     None, last_confidence_score,
                                     last_confidence_breakdown, last_action, last_skip_reason)
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
                    write_state_file(config, None, "DONE", 0, btc_price,
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
                    log.info(f"New market: {ticker} (was {prev_ticker}). Resetting to WATCH.")
                    current_phase = "WATCH"
                    current_position = None
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
                        _log_entry(market, "WATCH", secs_left, btc_price, strike,
                                   None, 0, "skip", f"WATCH phase, {elapsed:.0f}s elapsed",
                                   config.get("mode", "paper"))
                        write_state_file(config, market, current_phase, secs_left,
                                         btc_price, 0, {}, "watch", "")
                        await asyncio.sleep(10)
                        continue

                # ── LOCKED ─────────────────────────────────────────────────
                if current_phase == "LOCKED":
                    try:
                        await handle_locked_phase(
                            session, config, market, ticker, btc_price, secs_left, strike
                        )
                    except Exception as exc:
                        log.error(f"LOCKED phase error: {exc}", exc_info=True)
                    write_state_file(config, market, current_phase, secs_left, btc_price,
                                     last_confidence_score, last_confidence_breakdown,
                                     last_action, last_skip_reason)
                    await asyncio.sleep(10)
                    continue

                # ── DONE ───────────────────────────────────────────────────
                if current_phase == "DONE":
                    # Re-enter READY if this market still has meaningful time left
                    # (e.g. stop loss triggered at minute 8, still 7 min remaining)
                    if secs_left > 3 * 60:
                        log.info(
                            f"DONE → READY re-entry: {ticker} has {secs_left:.0f}s left."
                        )
                        current_phase = "READY"
                        # Fall through to READY handler below
                    else:
                        log.info(f"DONE phase. {secs_left:.0f}s left — waiting for next market.")
                        write_state_file(config, market, current_phase, secs_left, btc_price,
                                         last_confidence_score, last_confidence_breakdown,
                                         last_action, last_skip_reason)
                        await asyncio.sleep(10)
                        continue

                # ── READY ──────────────────────────────────────────────────
                if current_phase == "READY":
                    try:
                        await handle_ready_phase(
                            session, config, market, ticker,
                            btc_price, secs_left, strike, elapsed
                        )
                    except Exception as exc:
                        log.error(f"READY phase error: {exc}", exc_info=True)

                write_state_file(config, market, current_phase, secs_left, btc_price,
                                 last_confidence_score, last_confidence_breakdown,
                                 last_action, last_skip_reason)

            except Exception as exc:
                log.error(f"Main loop unhandled error: {exc}", exc_info=True)

            await asyncio.sleep(10)


# ══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════════

async def verify_kalshi_connection(session: aiohttp.ClientSession) -> None:
    """Verify Kalshi credentials work by fetching active markets. Exits on auth failure."""
    path = "/markets"
    params = {"status": "open", "series_ticker": "KXBTC15M", "limit": 1}
    try:
        async with session.get(
            KALSHI_BASE_URL + path,
            headers=kalshi_headers("GET", path),
            params=params,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json()
            if resp.status == 401:
                log.error("KALSHI AUTH FAILED (401) — check KALSHI_API_KEY and KALSHI_PRIVATE_KEY")
                sys.exit(1)
            if resp.status != 200:
                log.error(f"Kalshi connection check failed: HTTP {resp.status} — {data}")
                sys.exit(1)
            markets = data.get("markets", [])
            log.info(f"Kalshi connected. Active KXBTC15M markets visible: {len(markets)}")
    except SystemExit:
        raise
    except Exception as exc:
        log.error(f"Kalshi connection check failed: {exc}")
        sys.exit(1)


async def main() -> None:
    """Bootstrap: load credentials, init DB, start BTC feed, run main loop."""
    _init_config()
    load_credentials()
    init_db()
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

    await main_loop()


if __name__ == "__main__":
    asyncio.run(main())
