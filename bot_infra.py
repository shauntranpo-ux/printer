"""bot_infra.py - Infrastructure bundle: config, database, and Telegram notifications.

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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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
    "display_tz", "fmt_ts", "et_day_bounds_utc",
]


# Config

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


_mode_clamp_warned: set = set()


def strategy_mode(config: dict, strategy: str) -> str:
    """
    Resolve s1_mode/s2_mode with the safety rule: per-strategy modes may only go
    SAFER than the global mode. Order routing follows these keys while every safety
    rail (daily limits, preflight, startup reconcile, restore verification) keys off
    the global mode, so "live" is honored only when the global mode is live.
    Resolved at READ time rather than by mutating the config dict: the bot's own
    read-modify-write cycles (midnight_reset, the daily-limit trip) persist that
    dict back to config.json, and a mutating clamp permanently erased the operator's
    stored live setting.
    """
    mode = config.get("mode", "paper")
    smode = config.get(f"{strategy}_mode", mode)
    if smode == "live" and mode != "live":
        key = f"{strategy}_mode"
        if key not in _mode_clamp_warned:
            _mode_clamp_warned.add(key)
            log.warning("%s=live ignored - global mode is %s (safety rails key off it)",
                        key, mode)
        return mode
    return smode


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
    """Get config value - asset override if present, else global value, else default."""
    overrides = config.get("asset_overrides", {}).get(asset, {})
    if field in overrides:
        return overrides[field]
    return config.get(field, default)


def _init_config() -> None:
    """
    Create config.json on startup if missing, and apply Railway env var overrides.

    Set BOT_MODE=live and BOT_ENABLED=true in Railway environment variables
    so live mode survives every redeploy without manual editing.
    Daily loss limits still work - they set bot_state.limit_triggered in memory which
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
        "quiet_hours_enabled": True,
        # Notifications. Every timestamp in a Telegram message uses display_timezone
        # (an IANA name) with its real abbreviation (EDT/EST/...), never a fixed offset.
        "display_timezone": "America/New_York",
        # Per-trade settle alerts on by default; entry alerts are opt-in (2x volume).
        "notify_on_settle": True,
        "notify_on_entry": False,
        # End-of-day summary: sent once per day at/after this ET hour and it always
        # covers the PREVIOUS full ET calendar day (0 = just after midnight ET), so
        # no evening trade is ever missing from its day's report.
        "daily_summary_hour_et": 0,
        # Edge-measurement instrumentation (decision_log / maker_log / settlement basis).
        # Default on; set false to disable all measurement if it ever pressures rate limits.
        "measurement_enabled": True,
        # Manual time-of-day / day-of-week filter (bot_strategy._session_allowed). Default off
        # = no behavior change. Turn specific ET sessions off once the Edge dashboard shows they
        # lose. Valid sessions: us_open, us_midday, us_close, us_evening, overnight (see sessions.py).
        "blocked_sessions": [],
        "block_weekends": False,
        # Model self-calibration (scripts/calibration.py, fitted from decision_log every
        # 30 min): prob_scale shrinks/expands the fair value around 0.5. Off -> scale 1.0.
        "calibration_enabled": True,
        # Kalshi settles 15-min crypto on a ~60s average, not the point spot; the brains
        # price against effective time secs_left - settlement_avg_seconds/2.
        "settlement_avg_seconds": 60,
        # Auto-gate: block any ET session or (strategy, asset) bucket whose Wilson-LB
        # net-$/contract is not positive once it has 150+ settled picks. Inert until a
        # bucket reaches that sample size; blocked entries show in /api/edge.
        "auto_gate_enabled": True,
        # Best-strike ladder: in READY, evaluate up to this many candidate strikes /
        # windows per asset (every 30s) and enter the highest-EV one. 1 = off.
        "ladder_max_strikes": 3,
        # Paper maker execution (S2, paper mode only, default OFF): settle each trade
        # as the resting maker order the counterfactual tracked - filled trades get
        # maker pricing + maker fee, unfilled trades are voided at $0. Turn on only
        # after the Edge tab's maker-vs-taker delta is positive.
        "maker_execution_enabled": False,
        # Fair-value gate thresholds (both brains). Previously inline fallbacks only;
        # surfaced here so they can be tuned from the dashboard without a deploy.
        "min_ev_anchored": 0.025,
        "min_market_edge": 0.04,
        "max_model_edge": 0.08,
        # Too-good-to-be-true: REJECT (not clamp) any trade whose raw model-vs-market
        # gap exceeds this. Win rate fell monotonically with the gap in settled data.
        "max_model_market_gap": 0.15,
        # Entry band + tail ban. Sub-20c sides are the longshot tail (1W-28L on record;
        # the Kalshi-wide favorite-longshot study agrees); mids are de-vigged fractions.
        "fv_min_entry_price_cents": 20.0,
        "fv_max_entry_price_cents": 85.0,
        "s2_min_side_price_cents": 20.0,
        "s1_min_side_price_cents": 25.0,
        # Staleness gate: trade only when the spot moved toward the side bought within
        # the window while the tracked contract mid stayed put (a lagging book).
        "staleness_gate_enabled": True,
        "staleness_window_secs": 60.0,
        "staleness_min_spot_sigma": 0.35,
        "staleness_max_mid_move_cents": 3.0,
        # Sigma engine: market-implied EWMA anchor blended with live realized vol.
        "sigma_implied_weight": 0.6,
        "sigma_live_weight": 0.4,
        "sigma_implied_halflife_secs": 2700,
        "sigma_implied_max_age_secs": 900,
        "sigma_clamp_lo": 0.6,
        "sigma_clamp_hi": 1.7,
        # S1 lead-signal gates: BTC must genuinely move; live lead-beta accepted only
        # within this band around the static file beta (then shrunk halfway toward it).
        "s1_min_btc_ret": 0.0010,
        "s1_beta_clamp_lo": 0.5,
        "s1_beta_clamp_hi": 1.5,
        # Quarter-Kelly stake sizing: scales stakes DOWN from trade_amount_dollars on
        # thin edges (never up past the clip). kelly_cap = the quarter-Kelly fraction
        # that earns the full clip.
        "kelly_sizing_enabled": True,
        "kelly_cap": 0.05,
        "min_stake_dollars": 5.0,
        # Shadow favorite-bias candidate: logs would-buy-the-favorite decisions to
        # decision_log (zero capital). Now that S2 trades this thesis live, the shadow
        # runs at a slightly wider band and stays purely for the untraded-extension read.
        "shadow_fav_enabled": True,
        # Strategy duel (paper): both brains evaluate and trade every market; opposing
        # positions on the same ticker are allowed so per-strategy P&L is a clean A/B.
        # False restores the old one-way "S1 blocks S2" dedup.
        "strategy_duel_mode": True,
        # S1 MOMENTUM gates. A move counts only when it is real (>= min_sigma window
        # sigmas) and still underway; the continuation fair value projects the spot
        # forward by drift_lambda * move. Entry band keeps room to run.
        "s1_momentum_lookback_secs": 75,
        "s1_momentum_min_sigma": 1.0,
        "s1_momentum_drift_lambda": 0.5,
        "s1_confirm_ticks": 2,
        "s1_time_min": 3.0,
        "s1_time_max": 10.0,
        "s1_min_entry_cents": 30,
        "s1_max_entry_cents": 75,
        "s1_min_edge": 0.03,
        # BTC-lead is a logged confirming input; flip on to hard-require agreement.
        "s1_require_btc_confirm": False,
        "s1_btc_enabled": False,
        "s1_eth_enabled": False,
        # S2 FAVORITE-BIAS gates. Fire late on a proven favorite (|z| >= min_z) whose
        # de-vigged mid is in the premium band; buy it. No model-edge requirement - the
        # premium is realized win-rate > price; max_model_shortfall only vetoes traps.
        "s2_fav_min_z": 0.8,
        "s2_fav_mid_lo": 0.70,
        "s2_fav_mid_hi": 0.88,
        "s2_fav_confirm_ticks": 2,
        "s2_fav_time_min": 2.5,
        "s2_fav_time_max": 6.0,
        "s2_fav_min_entry_cents": 65,
        "s2_fav_max_entry_cents": 90,
        "s2_fav_max_model_shortfall": 0.08,
        # S2's ETH kill switch (the brain hard-skips ETH unless true) - mirror of the
        # s1_*_enabled pair above; previously read with an inline default only, making
        # it invisible in config.json.
        "s2_eth_enabled": False,
        # S1 flow-control knobs, previously inline-default only (invisible/untunable):
        # per-asset hourly entry cap, cross-asset burst window, and the consecutive-
        # loss cooldown that benches an asset after N straight losses.
        "max_s1_per_asset_per_hour": 2,
        "s1_cross_asset_window_seconds": 300.0,
        "s1_consec_loss_cooldown_count": 3,
        "s1_consec_loss_cooldown_secs": 900,
        # Test-slot lab strategies (S3-S6): paper-only regardless of global mode, all
        # tunable from the dashboard. See bot_strategies.STRATEGY_REGISTRY.
        # S3 structural arb: buy BOTH sides when the combined asks leave a fee-proof profit.
        "s3_arb_enabled": True,
        "s3_arb_max_combined_cents": 93,
        # S4 mean-reversion: fade a >=2-sigma run once the last third shows it stalling.
        "s4_revert_enabled": True,
        "s4_min_sigma": 2.0,
        "s4_lookback_secs": 120,
        "s4_revert_lambda": 0.5,
        "s4_time_min": 3.0,
        "s4_time_max": 10.0,
        "s4_min_entry_cents": 25,
        "s4_max_entry_cents": 70,
        "s4_min_edge": 0.03,
        # S5 maker spread-capture: passive quote 1c inside the favorite-side ask; settle
        # via the held-book fill model (unfilled -> $0 no-trade).
        "s5_maker_enabled": True,
        "s5_mid_lo": 0.60,
        "s5_mid_hi": 0.90,
        "s5_improve_cents": 1,
        "s5_time_min": 3.0,
        "s5_time_max": 9.0,
        # S6 window-fade: first 2 minutes of a window, FADE the previous window's
        # resolved direction at near-coin-flip prices. Gates tuned on 25k historical
        # settlement pairs (scripts/tune_fade.py): move >= 15bp AND streak >= 2 fades
        # 56.4% (Wilson-LB 0.552, +4.6c/ct at 50c) vs 53.4% unconditional.
        "s6_carry_enabled": True,
        "s6_window_secs": 120,
        "s6_min_prev_move": 0.0015,
        "s6_min_streak": 2,
        "s6_min_entry_cents": 40,
        "s6_max_entry_cents": 60,
        "s6_fade_premium": 0.064,
        "s6_min_edge": 0.01,
        # S7/S8: the volatility-regime mirror pair (every other strategy is
        # regime-blind). S7 trades breakouts only when live vol spikes vs its anchor;
        # S8 buys favorites only when vol has collapsed. See _vol_regime.
        "s7_volspike_enabled": True,
        "s7_spike_ratio": 1.6,
        "s7_lookback_secs": 60,
        "s7_time_min": 4.0,
        "s7_time_max": 10.0,
        "s7_min_entry_cents": 30,
        "s7_max_entry_cents": 70,
        "s7_min_edge": 0.03,
        "s8_calm_enabled": True,
        "s8_calm_ratio": 0.6,
        "s8_mid_lo": 0.55,
        "s8_mid_hi": 0.85,
        "s8_min_z": 0.4,
        "s8_time_min": 4.0,
        "s8_time_max": 10.0,
        "s8_min_entry_cents": 50,
        "s8_max_entry_cents": 88,
        "s8_min_edge": 0.03,
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

    # Safety caps - enforce on every startup so stale on-disk values can't bypass limits.
    cfg["max_s1_positions"]          = min(int(cfg.get("max_s1_positions", 5)), 5)
    cfg["max_s1_positions_per_asset"] = min(int(cfg.get("max_s1_positions_per_asset", 1)), 1)
    cfg["max_consecutive_losses"]    = min(int(cfg.get("max_consecutive_losses", 5)), 5)
    cfg["min_entry_price_cents"]     = max(float(cfg.get("min_entry_price_cents", 20)), 20.0)
    cfg["max_entry_price_cents"]     = min(float(cfg.get("max_entry_price_cents", 76)), 76.0)
    # Hard per-trade clip cap: $25 max (user directive). Never size a single entry above this.
    cfg["trade_amount_dollars"]      = min(max(float(cfg.get("trade_amount_dollars", 25)), 0.0), 25.0)
    # 0 (default) = NO daily loss cap - the bot never halts itself for the day.
    # A positive value re-enables the cap (clamped to 150 for safety).
    cfg["daily_loss_limit_dollars"]  = min(max(float(cfg.get("daily_loss_limit_dollars", 0)), 0.0), 150.0)

    # confidence_threshold: 72 was the old server.py default and blocks GBM-based trades.
    # GBM win_prob is capped at 0.75 (75%), so 72% threshold allows almost nothing.
    # 0 = disabled (bot_loops.py hardcoded fallback of 65 never applies when config key exists).
    if cfg.get("confidence_threshold", 0) >= 70:
        cfg["confidence_threshold"] = 0

    # Disable BTC and DOGE - only trade ETH, SOL, XRP.
    cfg["enabled_assets"] = [a for a in cfg.get("enabled_assets", ["ETH", "SOL", "XRP"])
                             if a not in ("BTC", "DOGE")]
    if not cfg["enabled_assets"]:
        cfg["enabled_assets"] = ["ETH", "SOL", "XRP"]

    # Restore evening trading - old migration forced quiet_start_et=17 (5pm ET).
    # Default is now 22 (10pm ET). Overwrite stale 17 on first deploy after this change.
    if cfg.get("quiet_start_et", 22) == 17:
        cfg["quiet_start_et"] = 22
    cfg.setdefault("quiet_start_et", 22)
    cfg.setdefault("quiet_end_et", 9)

    write_config(cfg)
    log.info(f"Config ready: mode={cfg['mode']} enabled={cfg['bot_enabled']}")


# Database

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

        # Sigma observability columns (nullable; ADD COLUMN is metadata-only in SQLite).
        # spot/strike/sigma_eff/z let the recalibration job refit the vol scale offline
        # from the survivorship-free decision population.
        for col in ("spot", "strike", "sigma_eff", "z"):
            try:
                c.execute(f"ALTER TABLE decision_log ADD COLUMN {col} REAL")
            except Exception:
                pass

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

        # settlement_basis: our Coinbase spot-vs-strike implied side vs Kalshi's official
        # result at each settle. Persists what bot_state._settlement_basis holds in memory
        # so the per-asset basis offset can be fitted across restarts.
        c.execute("""
            CREATE TABLE IF NOT EXISTS settlement_basis (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT,
                ticker      TEXT,
                asset       TEXT,
                strike      REAL,
                our_spot    REAL,
                kalshi      TEXT,
                ours        TEXT,
                agree       INTEGER,
                signed_dist REAL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_basis_asset ON settlement_basis(asset)")

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
        log.error("Cannot write trades - halting to prevent silent data loss.")
        sys.exit(2)


async def db_write_trade(trade: dict) -> int | None:
    """Insert a trade record. Returns the new row id."""
    try:
        async with aiosqlite.connect(bot_state._DB_FILE, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            cur = await db.execute("""
                INSERT INTO trades (
                    ts, market_id, market_title, mode, side, contracts,
                    entry_price_cents, trade_amount_dollars, confidence_score,
                    model_prob, implied_prob, btc_price_at_entry, strike,
                    seconds_left_at_entry, fill_confirmed,
                    exit_price_cents, exit_reason, outcome, pnl_dollars, profit_percent,
                    order_id, asset, raw_p_yes, entry_signals, strategy_variant, brain,
                    strategy_version
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                trade.get("strategy_version"),
            ))
            await db.commit()
            return cur.lastrowid
    except Exception as exc:
        log.error("db_write_trade FAILED - trade NOT recorded: %s | trade=%s", exc, trade)
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


async def db_update_trade(trade_id: int, fields: dict, only_if_pending: bool = False) -> None:
    """Update named columns on an existing trade row.

    only_if_pending=True adds `AND outcome='pending'` - the orphan sweeps use it so a
    row the live settle path finished between the sweep's snapshot and this write is
    never overwritten (the S5 maker void would replace a real win/loss with $0).
    """
    if trade_id is None:
        log.error("db_update_trade called with trade_id=None - trade will stay pending in DB")
        return
    bad_cols = set(fields) - _VALID_TRADE_COLS
    if bad_cols:
        log.error("db_update_trade: unknown column(s) %s - skipping update for trade %s", bad_cols, trade_id)
        return
    try:
        async with aiosqlite.connect(bot_state._DB_FILE, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            set_clause = ", ".join(f"{k} = ?" for k in fields)
            guard = " AND outcome = 'pending'" if only_if_pending else ""
            await db.execute(
                f"UPDATE trades SET {set_clause} WHERE id = ?{guard}",
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
        async with aiosqlite.connect(bot_state._DB_FILE, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("""
                INSERT INTO decision_log (
                    ts, ticker, asset, strategy, mode, side,
                    model_p_yes, market_mid_p_yes, market_edge,
                    entry_price_cents, secs_left, would_trade,
                    spot, strike, sigma_eff, z
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                decision.get("ts"), decision.get("ticker"), decision.get("asset"),
                decision.get("strategy"), decision.get("mode"), decision.get("side"),
                decision.get("model_p_yes"), decision.get("market_mid_p_yes"),
                decision.get("market_edge"), decision.get("entry_price_cents"),
                decision.get("secs_left"), int(bool(decision.get("would_trade"))),
                decision.get("spot"), decision.get("strike"),
                decision.get("sigma_eff"), decision.get("z"),
            ))
            await db.commit()
    except Exception as exc:
        # Logging must never break trading - swallow and move on.
        log.debug("db_write_decision skipped: %s", exc)


async def db_backfill_decision_outcome(ticker: str, outcome: str) -> None:
    """Stamp the settled YES/NO outcome onto all pending decision_log rows for a ticker."""
    if outcome not in ("yes", "no"):
        return
    try:
        async with aiosqlite.connect(bot_state._DB_FILE, timeout=30) as db:
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
        async with aiosqlite.connect(bot_state._DB_FILE, timeout=30) as db:
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


async def db_write_settlement_basis(sample: dict) -> None:
    """Persist one settlement-basis sample (fire-and-forget; never raises)."""
    try:
        async with aiosqlite.connect(bot_state._DB_FILE, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("""
                INSERT INTO settlement_basis (
                    ts, ticker, asset, strike, our_spot, kalshi, ours, agree, signed_dist
                ) VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                sample.get("ts"), sample.get("ticker"), sample.get("asset"),
                sample.get("strike"), sample.get("our_spot"), sample.get("kalshi"),
                sample.get("ours"), int(bool(sample.get("agree"))), sample.get("signed_dist"),
            ))
            await db.commit()
    except Exception as exc:
        log.debug("db_write_settlement_basis skipped: %s", exc)


async def db_settled_decision_probs(strategy: str, limit: int = 5000) -> list:
    """(model_p_yes, outcome) pairs for a strategy's settled decisions, newest first.
    Input to the prob_scale calibration fit."""
    try:
        async with aiosqlite.connect(bot_state._DB_FILE, timeout=30) as db:
            cur = await db.execute(
                "SELECT model_p_yes, outcome FROM decision_log "
                "WHERE strategy = ? AND outcome IN ('yes','no') AND model_p_yes IS NOT NULL "
                "ORDER BY id DESC LIMIT ?",
                (strategy, limit),
            )
            return list(await cur.fetchall())
    except Exception as exc:
        log.debug("db_settled_decision_probs skipped: %s", exc)
        return []


async def db_settled_decision_zs(strategy: str = "strategy2", limit: int = 20000) -> list:
    """(asset, z, outcome) for one strategy's settled decision_log rows with a recorded z.
    Input to the per-asset sigma_scale fit (strategy2 by default: its z comes from the
    actual spot, not a predicted one, and carries no shadow-strategy selection bias)."""
    try:
        async with aiosqlite.connect(bot_state._DB_FILE, timeout=30) as db:
            cur = await db.execute(
                "SELECT asset, z, outcome FROM decision_log "
                "WHERE strategy = ? AND outcome IN ('yes','no') AND z IS NOT NULL "
                "ORDER BY id DESC LIMIT ?",
                (strategy, limit),
            )
            return list(await cur.fetchall())
    except Exception as exc:
        log.debug("db_settled_decision_zs skipped: %s", exc)
        return []


async def db_settled_picks(limit: int = 10000) -> list:
    """Settled PICKS rows (dicts) from decision_log for the auto-gate computation."""
    try:
        async with aiosqlite.connect(bot_state._DB_FILE, timeout=30) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT ts, strategy, asset, side, outcome, entry_price_cents "
                "FROM decision_log WHERE outcome IN ('yes','no') AND would_trade = 1 "
                "AND side IS NOT NULL AND entry_price_cents IS NOT NULL "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [dict(r) for r in await cur.fetchall()]
    except Exception as exc:
        log.debug("db_settled_picks skipped: %s", exc)
        return []


async def db_basis_rows(limit: int = 5000) -> list:
    """(asset, signed_dist, kalshi) rows from settlement_basis, newest first.
    Input to the per-asset basis-offset fit."""
    try:
        async with aiosqlite.connect(bot_state._DB_FILE, timeout=30) as db:
            cur = await db.execute(
                "SELECT asset, signed_dist, kalshi FROM settlement_basis "
                "WHERE kalshi IN ('yes','no') AND signed_dist IS NOT NULL "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return list(await cur.fetchall())
    except Exception as exc:
        log.debug("db_basis_rows skipped: %s", exc)
        return []


async def db_pending_decision_tickers(older_than_iso: str, limit: int = 30) -> list:
    """Distinct tickers in decision_log still 'pending', evaluated before older_than_iso
    (so their window has closed). Used by the periodic settlement backfill."""
    try:
        async with aiosqlite.connect(bot_state._DB_FILE, timeout=30) as db:
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
        async with aiosqlite.connect(bot_state._DB_FILE, timeout=30) as db:
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
        async with aiosqlite.connect(bot_state._DB_FILE, timeout=30) as db:
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


async def db_get_today_pnl(mode: str, variants: "tuple | None" = None) -> float:
    """
    Sum pnl_dollars for completed trades in the given mode today (ET calendar day).
    `variants` optionally restricts to specific strategy_variant values - the daily
    limit check passes the main-line pair so the lab slots' paper P&L can't trip the
    profit target / loss cap on S1/S2's behalf. None = all strategies (notifications).
    """
    try:
        start, end = et_day_bounds_utc(datetime.now(_ET).date())
        q = ("SELECT COALESCE(SUM(pnl_dollars), 0) FROM trades "
             "WHERE mode = ? AND ts >= ? AND ts < ? AND outcome != 'pending'")
        params: list = [mode, start, end]
        if variants:
            q += f" AND COALESCE(strategy_variant, 'strategy2') IN ({','.join('?' * len(variants))})"
            params.extend(variants)
        async with aiosqlite.connect(bot_state._DB_FILE, timeout=30) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            async with db.execute(q, params) as cur:
                row = await cur.fetchone()
        return float(row[0]) if row else 0.0
    except Exception as exc:
        log.error(f"DB get_today_pnl error: {exc}")
        return 0.0


# Live WR calibration helpers

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


# Notifications

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


_ET = ZoneInfo("America/New_York")


def display_tz(config: dict | None = None) -> ZoneInfo:
    """Timezone every notification timestamp is rendered in (config: display_timezone)."""
    try:
        cfg = config if config is not None else read_config()
        return ZoneInfo(str(cfg.get("display_timezone", "America/New_York")))
    except Exception:
        return ZoneInfo("America/New_York")


def fmt_ts(dt: datetime | None = None, config: dict | None = None) -> str:
    """'Jul 5, 2:14 PM EDT' in the display timezone. Naive input is treated as UTC.

    Built without %-d/%-I - those are glibc extensions that raise on Windows.
    """
    d = dt or datetime.now(timezone.utc)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    d = d.astimezone(display_tz(config))
    hour12 = d.strftime("%I").lstrip("0") or "12"
    return f"{d.strftime('%b')} {d.day}, {hour12}:{d.strftime('%M')} {d.strftime('%p')} {d.strftime('%Z')}"


def et_day_bounds_utc(day) -> tuple[str, str]:
    """UTC ISO bounds [start, end) of the given ET calendar day.

    The trading "day" everywhere in this bot is the ET calendar day (sessions,
    quiet hours, daily reports). Trade rows carry UTC timestamps, so day queries
    must use these bounds - DATE(ts) buckets by UTC date and misclassifies every
    trade after 8pm ET.
    """
    start = datetime(day.year, day.month, day.day, tzinfo=_ET).astimezone(timezone.utc)
    nxt = day + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=_ET).astimezone(timezone.utc)
    return start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S")


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
    # Fill verification notifications suppressed - daily summary only


async def send_telegram(text: str) -> None:
    """Send a Telegram notification with retries. 429s honor the response's
    retry_after (typically 5-30s at a settle burst across 5 assets) - a fixed 2s
    backoff landed every retry inside the same rate-limit window and dropped the
    message."""
    if not bot_state.TELEGRAM_BOT_TOKEN or not bot_state.TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{bot_state.TELEGRAM_BOT_TOKEN}/sendMessage"
    delay = 2.0
    for attempt in range(1, 5):
        try:
            log.info(f"Telegram: sending (attempt {attempt}/4)...")
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
                        try:
                            delay = min(35.0, float(
                                json.loads(body).get("parameters", {}).get("retry_after", 5)) + 1.0)
                        except Exception:
                            delay = 5.0
                        log.warning(f"Telegram: rate-limited (429), retry in {delay:.0f}s "
                                    f"-- attempt {attempt}/4")
                    else:
                        log.warning(f"Telegram: HTTP {resp.status} -- {body}")
                        return
        except Exception as exc:
            log.warning(f"Telegram: error on attempt {attempt}/4 -- {exc}")
        if attempt < 4:
            await asyncio.sleep(delay)
    log.error("Telegram: failed after 4 attempts -- notification dropped")
