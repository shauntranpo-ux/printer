"""bot_db.py â€” SQLite trade log: init, write, update, query."""
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

import aiosqlite

import bot_state

log = logging.getLogger("bot")


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

        # Migrate existing DB â€” add new columns if not present
        for col, typedef in (
            ("order_id",          "TEXT"),
            ("asset",             "TEXT DEFAULT 'BTC'"),  # multi-asset support
            ("raw_p_yes",         "REAL"),                # pre-calibration P(YES wins)
            ("entry_signals",    "TEXT"),                # JSON snapshot of entry signals
            ("calibrated_p_yes",  "REAL"),               # post-calibration p_yes used in EV gate
            ("signal_name",       "TEXT"),               # which signal fired (d3_hybrid / supertrend)
            ("strategy_variant",  "TEXT DEFAULT 'strategy2'"),  # strategy1=original, strategy2=D3 hybrid
            ("strategy_version",   "TEXT"),                       # bumped when logic changes
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
        log.error("Cannot write trades â€” halting to prevent silent data loss.")
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

