"""bot_infra.py — Infrastructure bundle: config, database, and Telegram notifications.

Public interface (see __all__):
  Config:  atomic_write_json, read_config, write_config, get_asset_config, _init_config
  DB:      init_db, test_db_write, db_write_trade, db_update_trade,
           db_write_market_log, db_get_today_pnl
  Notify:  send_telegram, _maybe_fill_verification_notify, _notify_ctx, _phase_for_eth
"""
import asyncio
import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone

import aiohttp
import aiosqlite

import bot_state

log = logging.getLogger("bot")

__all__ = [
    # Config
    "atomic_write_json", "read_config", "write_config", "get_asset_config", "_init_config",
    # DB
    "init_db", "test_db_write", "db_write_trade", "db_update_trade",
    "db_write_market_log", "db_get_today_pnl",
    # Notify
    "send_telegram", "_maybe_fill_verification_notify", "_notify_ctx", "_phase_for_eth",
]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

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


def read_config() -> dict:
    """Read and return the contents of the config file.
    Falls back to bot_state._last_good_config if the file is transiently corrupt
    (e.g. a partial write in progress). Raises only on startup failure
    when no cached config is available yet.
    """
    try:
        with open(bot_state._CONFIG_FILE, "r") as fh:
            cfg = json.load(fh)
        cfg.setdefault("enabled_assets", ["ETH", "SOL", "XRP"])
        bot_state._last_good_config = cfg
        return cfg
    except json.JSONDecodeError as exc:
        log.warning(f"Config JSON decode error: {exc}. Using last known good config.")
        if bot_state._last_good_config is not None:
            return bot_state._last_good_config
        raise
    except Exception as exc:
        log.warning(f"Config read error: {exc}. Using last known good config.")
        if bot_state._last_good_config is not None:
            return bot_state._last_good_config
        raise


def write_config(data: dict) -> None:
    """Write a dict to the config file atomically (temp+replace)."""
    atomic_write_json(data, bot_state._CONFIG_FILE)


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
    Daily loss limits still work — they set bot_state.limit_triggered in memory which
    is checked independently of the mode flag.
    """
    defaults = {
        "bot_enabled": False,
        "trade_amount_dollars": 25,
        "mode": "paper",
        "confidence_threshold": 0,
        "daily_loss_limit_dollars": 50,
        "daily_profit_target_dollars": 200,
        "max_consecutive_losses": 5,
        "enable_reversal_signal": False,
        "min_ev_base": 8,
        "kalshi_fee_per_contract_cents": 7,
        "preflight_override": False,
    }

    if os.path.exists(bot_state._CONFIG_FILE):
        try:
            with open(bot_state._CONFIG_FILE) as fh:
                cfg = json.load(fh)
        except Exception:
            cfg = defaults.copy()
    else:
        cfg = defaults.copy()

    _data_dir = os.path.dirname(os.path.abspath(bot_state._DB_FILE))
    _be_state = os.path.join(_data_dir, "bot_enabled.state")
    if os.path.exists(_be_state):
        try:
            cfg["bot_enabled"] = open(_be_state).read().strip() == "1"
        except Exception:
            pass

    if "BOT_MODE" in os.environ:
        cfg["mode"] = os.environ["BOT_MODE"].strip().lower()
    if "BOT_ENABLED" in os.environ:
        cfg["bot_enabled"] = os.environ["BOT_ENABLED"].strip().lower() in ("1", "true", "yes")

    for k, v in defaults.items():
        cfg.setdefault(k, v)

    write_config(cfg)
    log.info(f"Config ready: mode={cfg['mode']} enabled={cfg['bot_enabled']}")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create the database and all required tables if they do not exist."""
    try:
        conn = sqlite3.connect(bot_state._DB_FILE)
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

        for col, typedef in (
            ("order_id",          "TEXT"),
            ("asset",             "TEXT DEFAULT 'BTC'"),
            ("raw_p_yes",         "REAL"),
            ("entry_signals",    "TEXT"),
            ("calibrated_p_yes",  "REAL"),
            ("signal_name",       "TEXT"),
            ("strategy_variant",  "TEXT DEFAULT 'strategy2'"),
            ("strategy_version",   "TEXT"),
        ):
            try:
                c.execute(f"ALTER TABLE trades ADD COLUMN {col} {typedef}")
            except Exception:
                pass

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
    """Smoke-test the DB pipeline: write a sentinel row, read it back, delete it."""
    try:
        conn = sqlite3.connect(bot_state._DB_FILE)
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
        log.info(f"DB self-test PASSED  path={os.path.abspath(bot_state._DB_FILE)}")
    except Exception as exc:
        log.error(f"DB self-test FAILED: {exc}")
        log.error(f"DB path: {os.path.abspath(bot_state._DB_FILE)}")
        log.error("Cannot write trades — halting to prevent silent data loss.")
        sys.exit(2)


async def db_write_trade(trade: dict) -> int | None:
    """Insert a trade record. Returns the new row id."""
    try:
        async with aiosqlite.connect(bot_state._DB_FILE) as db:
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
    if trade_id is None:
        log.error("db_update_trade called with trade_id=None — trade will stay pending in DB")
        return
    try:
        async with aiosqlite.connect(bot_state._DB_FILE) as db:
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
        async with aiosqlite.connect(bot_state._DB_FILE) as db:
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
        async with aiosqlite.connect(bot_state._DB_FILE) as db:
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


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def _phase_for_eth(asset, elapsed_seconds):
    """Return ETH hourly window-phase label ('Mid'/'Dwell'/'Late') or None."""
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
    """Send a fill-verification Telegram message to spot price-selection bugs in flight."""
    if fill_yes_price is None:
        return
    _target = entry_price_cents
    _ask = market_ask_at_post_c
    _posted = price_this_attempt
    _filled = fill_yes_price
    _target_str = f"{int(round(_target))}c" if _target is not None else "-"
    _ask_str    = f"{int(round(_ask))}c"    if _ask    is not None else "-"
    _posted_str = f"{int(round(_posted))}c" if _posted is not None else "-"
    _filled_str = f"{int(round(_filled))}c"
    if _target is not None:
        _slip_target = int(round(_filled - _target))
        _slip_target_str = f"{_slip_target:+d}c vs target"
        _warn = "WARN " if abs(_slip_target) > 3 else "OK "
    else:
        _slip_target_str = "n/a vs target"
        _warn = "OK "
    _slip_market_str = (
        f"{int(round(_filled - _ask)):+d}c vs market" if _ask is not None else "n/a vs market"
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
    if not bot_state.TELEGRAM_BOT_TOKEN or not bot_state.TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{bot_state.TELEGRAM_BOT_TOKEN}/sendMessage"
    for attempt in range(1, 4):
        try:
            log.info(f"Telegram: sending (attempt {attempt}/3)...")
            async with aiohttp.ClientSession() as tg:
                async with tg.post(
                    url,
                    json={"chat_id": bot_state.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    body = await resp.text()
                    if resp.status == 200:
                        log.info("Telegram: sent OK")
                        return
                    elif resp.status == 429:
                        log.warning(f"Telegram: rate-limited (429) -- attempt {attempt}/3, retrying...")
                    else:
                        log.warning(f"Telegram: HTTP {resp.status} -- {body}")
                        return
        except Exception as exc:
            log.warning(f"Telegram: error on attempt {attempt}/3 -- {exc}")
        if attempt < 3:
            await asyncio.sleep(2)
    log.error("Telegram: failed after 3 attempts -- notification dropped")
