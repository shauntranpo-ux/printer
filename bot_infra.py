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
    "db_brain_scorecard", "db_write_market_log", "db_get_today_pnl",
    "_update_wr_bucket", "_get_empirical_wr",
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
        "s1_mode": "paper",
        "s2_mode": "paper",
        "confidence_threshold": 0,
        "daily_loss_limit_dollars": 0,
        "daily_profit_target_dollars": 200,
        "max_consecutive_losses": 5,
        "enable_reversal_signal": False,
        "min_ev_base": 8,
        "kalshi_fee_per_contract_cents": 7,
        "preflight_override": False,
        # Edge-measurement instrumentation (decision_log / maker_log / settlement basis).
        # Default on; set false to disable all measurement if it ever pressures rate limits.
        "measurement_enabled": True,
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
            with open(_be_state) as _f:
                cfg["bot_enabled"] = _f.read().strip() == "1"
        except Exception:
            pass

    if "BOT_MODE" in os.environ:
        cfg["mode"] = os.environ["BOT_MODE"].strip().lower()
    if "BOT_ENABLED" in os.environ:
        cfg["bot_enabled"] = os.environ["BOT_ENABLED"].strip().lower() in ("1", "true", "yes")

    for k, v in defaults.items():
        cfg.setdefault(k, v)

    # Safety caps — enforce on every startup so stale on-disk values can't bypass limits.
    cfg["max_s1_positions"]          = min(int(cfg.get("max_s1_positions", 5)), 5)
    cfg["max_s1_positions_per_asset"] = min(int(cfg.get("max_s1_positions_per_asset", 1)), 1)
    cfg["max_consecutive_losses"]    = min(int(cfg.get("max_consecutive_losses", 5)), 5)
    cfg["min_entry_price_cents"]     = max(float(cfg.get("min_entry_price_cents", 20)), 20.0)
    cfg["max_entry_price_cents"]     = min(float(cfg.get("max_entry_price_cents", 76)), 76.0)
    # Hard per-trade clip cap: $25 max (user directive). Never size a single entry above this.
    cfg["trade_amount_dollars"]      = min(max(float(cfg.get("trade_amount_dollars", 25)), 0.0), 25.0)
    # 0 (default) = NO daily loss cap — the bot never halts itself for the day.
    # A positive value re-enables the cap (clamped to 150 for safety).
    cfg["daily_loss_limit_dollars"]  = min(max(float(cfg.get("daily_loss_limit_dollars", 0)), 0.0), 150.0)

    # confidence_threshold: 72 was the old server.py default and blocks GBM-based trades.
    # GBM win_prob is capped at 0.75 (75%), so 72% threshold allows almost nothing.
    # 0 = disabled (bot_loops.py hardcoded fallback of 65 never applies when config key exists).
    if cfg.get("confidence_threshold", 0) >= 70:
        cfg["confidence_threshold"] = 0

    # Disable BTC and DOGE — only trade ETH, SOL, XRP.
    cfg["enabled_assets"] = [a for a in cfg.get("enabled_assets", ["ETH", "SOL", "XRP"])
                             if a not in ("BTC", "DOGE")]
    if not cfg["enabled_assets"]:
        cfg["enabled_assets"] = ["ETH", "SOL", "XRP"]

    # Restore evening trading — old migration forced quiet_start_et=17 (5pm ET).
    # Default is now 22 (10pm ET). Overwrite stale 17 on first deploy after this change.
    if cfg.get("quiet_start_et", 22) == 17:
        cfg["quiet_start_et"] = 22
    cfg.setdefault("quiet_start_et", 22)
    cfg.setdefault("quiet_end_et", 9)

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
                strategy_variant      TEXT DEFAULT 'strategy2',
                brain                 TEXT
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
            ("brain",              "TEXT"),
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

        c.execute("""
            CREATE TABLE IF NOT EXISTS wr_calibration (
                asset       TEXT NOT NULL,
                dist_bucket INTEGER NOT NULL,
                time_bucket INTEGER NOT NULL,
                strategy    TEXT NOT NULL DEFAULT 's1',
                mode        TEXT NOT NULL DEFAULT 'live',
                win_count   INTEGER NOT NULL DEFAULT 0,
                total_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (asset, dist_bucket, time_bucket, strategy, mode)
            )
        """)

        # decision_log: every brain evaluation (NOT just taken trades) so the edge of the
        # signal can be measured without survivorship bias. outcome is backfilled at
        # settlement. See scripts/edge_report.py.
        c.execute("""
            CREATE TABLE IF NOT EXISTS decision_log (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                ts               TEXT,
                ticker           TEXT,
                asset            TEXT,
                strategy         TEXT,
                mode             TEXT,
                side             TEXT,
                model_p_yes      REAL,
                market_mid_p_yes REAL,
                market_edge      REAL,
                entry_price_cents REAL,
                secs_left        REAL,
                would_trade      INTEGER DEFAULT 0,
                outcome          TEXT DEFAULT 'pending'
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_decision_ticker ON decision_log(ticker)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_decision_outcome ON decision_log(outcome)")

        # maker_log: per settled trade, the maker-vs-taker counterfactual (measurement only).
        # See bot_loops._record_maker_counterfactual + scripts/maker_report.py.
        c.execute("""
            CREATE TABLE IF NOT EXISTS maker_log (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                ts               TEXT,
                ticker           TEXT,
                asset            TEXT,
                strategy         TEXT,
                mode             TEXT,
                side             TEXT,
                entry_ask_cents  REAL,
                maker_price_cents REAL,
                filled           INTEGER,
                outcome          TEXT,
                taker_pnl        REAL,
                maker_pnl        REAL,
                contracts        INTEGER
            )
        """)

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
                    order_id, asset, raw_p_yes, entry_signals, strategy_variant, brain
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                trade.get("strategy_variant", "strategy2"), trade.get("brain"),
            ))
            await db.commit()
            return cur.lastrowid
    except Exception as exc:
        log.error("db_write_trade FAILED — trade NOT recorded: %s | trade=%s", exc, trade)
        return None


_VALID_TRADE_COLS = frozenset({
    "ts", "market_id", "market_title", "mode", "side", "contracts",
    "entry_price_cents", "trade_amount_dollars", "confidence_score",
    "model_prob", "implied_prob", "btc_price_at_entry", "strike",
    "seconds_left_at_entry", "fill_confirmed", "exit_price_cents",
    "exit_reason", "outcome", "pnl_dollars", "profit_percent",
    "order_id", "asset", "raw_p_yes", "entry_signals",
    "strategy_variant", "brain", "signal_name", "strategy_version",
})


async def db_update_trade(trade_id: int, fields: dict) -> None:
    """Update named columns on an existing trade row."""
    if trade_id is None:
        log.error("db_update_trade called with trade_id=None — trade will stay pending in DB")
        return
    bad_cols = set(fields) - _VALID_TRADE_COLS
    if bad_cols:
        log.error("db_update_trade: unknown column(s) %s — skipping update for trade %s", bad_cols, trade_id)
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
        raise


async def db_write_decision(decision: dict) -> None:
    """
    Record one brain evaluation in decision_log (fire-and-forget; never raises into the
    hot loop). Logs ALL decisions, not just taken trades, so edge measurement is free of
    survivorship bias. outcome stays 'pending' until db_backfill_decision_outcome runs.
    """
    try:
        async with aiosqlite.connect(bot_state._DB_FILE) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("""
                INSERT INTO decision_log (
                    ts, ticker, asset, strategy, mode, side,
                    model_p_yes, market_mid_p_yes, market_edge,
                    entry_price_cents, secs_left, would_trade
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                decision.get("ts"), decision.get("ticker"), decision.get("asset"),
                decision.get("strategy"), decision.get("mode"), decision.get("side"),
                decision.get("model_p_yes"), decision.get("market_mid_p_yes"),
                decision.get("market_edge"), decision.get("entry_price_cents"),
                decision.get("secs_left"), int(bool(decision.get("would_trade"))),
            ))
            await db.commit()
    except Exception as exc:
        # Logging must never break trading — swallow and move on.
        log.debug("db_write_decision skipped: %s", exc)


async def db_backfill_decision_outcome(ticker: str, outcome: str) -> None:
    """Stamp the settled YES/NO outcome onto all pending decision_log rows for a ticker."""
    if outcome not in ("yes", "no"):
        return
    try:
        async with aiosqlite.connect(bot_state._DB_FILE) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute(
                "UPDATE decision_log SET outcome = ? WHERE ticker = ? AND outcome = 'pending'",
                (outcome, ticker),
            )
            await db.commit()
    except Exception as exc:
        log.debug("db_backfill_decision_outcome skipped for %s: %s", ticker, exc)


async def db_write_maker_sample(sample: dict) -> None:
    """Record one maker-vs-taker counterfactual sample (fire-and-forget; never raises)."""
    try:
        async with aiosqlite.connect(bot_state._DB_FILE) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("""
                INSERT INTO maker_log (
                    ts, ticker, asset, strategy, mode, side,
                    entry_ask_cents, maker_price_cents, filled, outcome,
                    taker_pnl, maker_pnl, contracts
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                sample.get("ts"), sample.get("ticker"), sample.get("asset"),
                sample.get("strategy"), sample.get("mode"), sample.get("side"),
                sample.get("entry_ask_cents"), sample.get("maker_price_cents"),
                int(bool(sample.get("filled"))), sample.get("outcome"),
                sample.get("taker_pnl"), sample.get("maker_pnl"), sample.get("contracts"),
            ))
            await db.commit()
    except Exception as exc:
        log.debug("db_write_maker_sample skipped: %s", exc)


async def db_pending_decision_tickers(older_than_iso: str, limit: int = 30) -> list:
    """Distinct tickers in decision_log still 'pending', evaluated before older_than_iso
    (so their window has closed). Used by the periodic settlement backfill."""
    try:
        async with aiosqlite.connect(bot_state._DB_FILE) as db:
            cur = await db.execute(
                "SELECT DISTINCT ticker FROM decision_log "
                "WHERE outcome = 'pending' AND ts < ? LIMIT ?",
                (older_than_iso, limit),
            )
            rows = await cur.fetchall()
            return [r[0] for r in rows if r[0]]
    except Exception as exc:
        log.debug("db_pending_decision_tickers skipped: %s", exc)
        return []


async def db_brain_scorecard(today: str) -> dict:
    """Returns daily and all-time per-brain per-asset P&L for S1 and S2."""
    result: dict = {
        "daily":   {"s1": {}, "s2": {}},
        "alltime": {"s1": {}, "s2": {}},
    }
    _query_daily = """
        SELECT brain, asset,
               COUNT(*) AS trades,
               COALESCE(SUM(pnl_dollars), 0) AS pnl,
               SUM(CASE WHEN pnl_dollars > 0 THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN pnl_dollars < 0 THEN 1 ELSE 0 END) AS losses
        FROM trades
        WHERE brain IN ('s1', 's2')
          AND pnl_dollars IS NOT NULL
          AND date(ts) = ?
        GROUP BY brain, asset
    """
    _query_alltime = """
        SELECT brain, asset,
               COUNT(*) AS trades,
               COALESCE(SUM(pnl_dollars), 0) AS pnl,
               SUM(CASE WHEN pnl_dollars > 0 THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN pnl_dollars < 0 THEN 1 ELSE 0 END) AS losses
        FROM trades
        WHERE brain IN ('s1', 's2')
          AND pnl_dollars IS NOT NULL
        GROUP BY brain, asset
    """
    try:
        async with aiosqlite.connect(bot_state._DB_FILE) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            for scope, query, params in (
                ("daily",   _query_daily,   (today,)),
                ("alltime", _query_alltime, ()),
            ):
                async with db.execute(query, params) as cur:
                    async for row in cur:
                        brain, asset, trades, pnl, wins, losses = row
                        if brain in result[scope]:
                            result[scope][brain][asset] = {
                                "trades": trades,
                                "pnl":    round(pnl or 0.0, 2),
                                "wins":   wins or 0,
                                "losses": losses or 0,
                            }
    except Exception as exc:
        log.error("db_brain_scorecard error: %s", exc)
    return result


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
                "WHERE mode = ? AND DATE(ts) = ? AND outcome != 'pending'",
                (mode, today),
            ) as cur:
                row = await cur.fetchone()
        return float(row[0]) if row else 0.0
    except Exception as exc:
        log.error(f"DB get_today_pnl error: {exc}")
        return 0.0


# ---------------------------------------------------------------------------
# Live WR calibration helpers
# ---------------------------------------------------------------------------

def _update_wr_bucket(
    asset: str, abs_pct: float, mins_left: float,
    outcome: str, mode: str, strategy: str = "s1",
) -> None:
    """Increment win/total counters for the matching WR calibration bucket."""
    from bot_strategy import _S1_DIST_BOUNDS, _S1_TIME_BOUNDS
    dist_idx = len(_S1_DIST_BOUNDS)
    for i, b in enumerate(_S1_DIST_BOUNDS):
        if abs_pct < b:
            dist_idx = i
            break
    time_idx = len(_S1_TIME_BOUNDS)
    for i, b in enumerate(_S1_TIME_BOUNDS):
        if mins_left < b:
            time_idx = i
            break
    win_inc = 1 if outcome == "win" else 0
    try:
        conn = sqlite3.connect(bot_state._DB_FILE)
        conn.execute("""
            INSERT INTO wr_calibration (asset, dist_bucket, time_bucket, strategy, mode, win_count, total_count)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(asset, dist_bucket, time_bucket, strategy, mode)
            DO UPDATE SET win_count=win_count+excluded.win_count, total_count=total_count+1
        """, (asset, dist_idx, time_idx, strategy, mode, win_inc))
        conn.commit()
        conn.close()
    except Exception as exc:
        log.warning("_update_wr_bucket error: %s", exc)


def _get_empirical_wr(
    asset: str, abs_pct: float, mins_left: float,
    mode: str, strategy: str = "s1", min_samples: int = 30,
    breakeven_wr: float = 0.38,
) -> "float | None":
    """
    Return empirical WR only when statistically proven to exceed breakeven.

    Uses one-sided 95% Wilson CI lower bound. Returns None when:
      - fewer than min_samples trades in bucket
      - Wilson lower bound <= breakeven_wr (not enough evidence of edge)

    Raising min_samples 20->30 and adding Wilson CI prevents the bot from
    acting on noise during burn-in. 0.38 breakeven matches ~38-40c entry prices.
    """
    import math
    from bot_strategy import _S1_DIST_BOUNDS, _S1_TIME_BOUNDS

    dist_idx = len(_S1_DIST_BOUNDS)
    for i, b in enumerate(_S1_DIST_BOUNDS):
        if abs_pct < b:
            dist_idx = i
            break
    time_idx = len(_S1_TIME_BOUNDS)
    for i, b in enumerate(_S1_TIME_BOUNDS):
        if mins_left < b:
            time_idx = i
            break
    try:
        conn = sqlite3.connect(bot_state._DB_FILE)
        row = conn.execute(
            "SELECT win_count, total_count FROM wr_calibration "
            "WHERE asset=? AND dist_bucket=? AND time_bucket=? AND strategy=? AND mode=?",
            (asset, dist_idx, time_idx, strategy, mode),
        ).fetchone()
        conn.close()
        if not row or row[1] < min_samples:
            return None
        wins, n = row[0], row[1]
        p = wins / n
        z = 1.645
        wlb = (p + z*z/(2*n) - z * math.sqrt((p*(1-p) + z*z/(4*n)) / n)) / (1 + z*z/n)
        if wlb <= breakeven_wr:
            return None
        return p
    except Exception:
        return None


def get_today_pnl(mode: str = "paper") -> float:
    """Sum pnl_dollars for all settled trades today (UTC date)."""
    try:
        import datetime
        today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        conn = sqlite3.connect(bot_state._DB_FILE)
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl_dollars), 0.0) FROM trades "
            "WHERE outcome IN ('win','loss') AND mode=? AND DATE(ts) = ?",
            (mode, today),
        ).fetchone()
        conn.close()
        return float(row[0]) if row else 0.0
    except Exception:
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
    # Fill verification notifications suppressed — daily summary only


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
