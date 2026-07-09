"""
server.py - Flask backend for the Kalshi BTC bot dashboard.

Reads from kalshi_bot.db and bot_state.json (written by bot.py).
Writes to config.json when the user changes settings.
Never writes to the database directly.

Start via runner.py, not directly.
"""

import csv
import io
import json
import logging
import os
import signal
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

try:
    from flask import Flask, jsonify, request
except Exception as _import_err:
    import traceback
    traceback.print_exc()
    raise

import notify
import obs
obs.setup_logging("server")
log = logging.getLogger("server")
_PROCESS_START = time.time()

# Always run from the directory containing this file so relative paths work
# under gunicorn (which doesn't execute __main__)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(_BASE_DIR)

# Ensure the data directory exists (Railway volume at /app/data)
try:
    _db_path = os.environ.get("BOT_DB_FILE", "")
    if _db_path:
        _db_dir = os.path.dirname(_db_path)
        if _db_dir:
            os.makedirs(_db_dir, exist_ok=True)
            log.info(f"Data directory ready: {_db_dir}")
except Exception as _dir_err:
    logging.warning(f"Could not create data directory: {_dir_err}")

# Write default config.json if it doesn't exist (e.g. fresh Railway deploy)
_FULL_CONFIG_DEFAULT = {
    "bot_enabled": True,
    "mode": "paper",
    "trade_amount_dollars": 25,
    "confidence_threshold": 0,
    "daily_loss_limit_dollars": 0,
    "daily_profit_target_dollars": 200,
    "quiet_hours_enabled": True,
    "quiet_start_et": 22,
    "quiet_end_et": 9,
    "display_timezone": "America/New_York",
    "notify_on_settle": True,
    "notify_on_entry": False,
    "daily_summary_hour_et": 0,
    "enabled_assets": ["ETH", "SOL", "XRP"],
}
if not os.path.exists("config.json"):
    try:
        with open("config.json", "w", encoding="utf-8") as _f:
            json.dump(_FULL_CONFIG_DEFAULT, _f, indent=2)
        log.info("Created default config.json")
    except Exception as _cfg_err:
        logging.warning(f"Could not create default config.json: {_cfg_err}")

# On startup: if mode was live, reset to paper (safety - never auto-start real trades on redeploy).
# bot_enabled is intentionally preserved so the bot resumes running in paper mode after redeploy.
try:
    if os.path.exists("config.json"):
        with open("config.json", "r", encoding="utf-8") as _f:
            _cfg = json.load(_f)
        if _cfg.get("mode", "paper") == "live":
            _cfg["mode"] = "paper"
            with open("config.json", "w", encoding="utf-8") as _f:
                json.dump(_cfg, _f, indent=2)
            log.info("Startup safety reset: live -> paper mode")
except Exception as _rst_err:
    logging.warning(f"Could not apply startup safety reset: {_rst_err}")

app = Flask(__name__)

# Bot subprocess tracking
_bot_process: subprocess.Popen | None = None


#  Helpers

def _safe_json_read(path: str, default):
    """Read a JSON file and return default on any error (missing, corrupt, etc)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        log.warning(f"JSON decode error in {path}: {exc}")
        return default
    except Exception as exc:
        log.warning(f"Could not read {path}: {exc}")
        return default


def _display_now_str() -> str:
    """Current time in the configured display timezone with its real abbreviation.

    Replaces a hard-coded UTC-7 labeled "PST" - wrong label, and wrong offset
    for half the year.
    """
    tz_name = "America/New_York"
    try:
        with open("config.json", "r", encoding="utf-8") as fh:
            tz_name = json.load(fh).get("display_timezone", tz_name) or tz_name
    except Exception:
        pass
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/New_York")
    d = datetime.now(tz)
    hour12 = d.strftime("%I").lstrip("0") or "12"
    return f"{d.strftime('%b')} {d.day}, {hour12}:{d.strftime('%M')} {d.strftime('%p')} {d.strftime('%Z')}"


def _telegram_notify(text: str) -> None:
    """Fire-and-forget Telegram notification from the Flask process."""
    notify.send_alert("INFO", text)


_CONFIG_DEFAULT = {"mode": "paper", "trade_amount_dollars": 25, "confidence_threshold": 0,
                   "daily_loss_limit_dollars": 0,
                   "daily_profit_target_dollars": 200}
_STATE_DEFAULT  = {"btc_price": None, "today_live_pnl": 0.0, "today_paper_pnl": 0.0,
                   "today_demo_pnl": 0.0, "phase": "waiting", "mode": "paper"}

def read_config() -> dict:
    return _safe_json_read("config.json", _CONFIG_DEFAULT.copy())


def _atomic_write_json(data: dict, path: str) -> None:
    """Atomic JSON write: write to temp file then os.replace."""
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


def write_config(data: dict) -> None:
    _atomic_write_json(data, "config.json")


def read_state() -> dict:
    return _safe_json_read("bot_state.json", _STATE_DEFAULT.copy())


def _load_strategies() -> list[dict]:
    return _safe_json_read("strategies.json", [])


def _read_strategy_state(state_file: str) -> dict | None:
    return _safe_json_read(state_file, None)


def get_db() -> sqlite3.Connection:
    """Open a WAL-mode SQLite connection with Row factory."""
    db_path = os.environ.get("BOT_DB_FILE", "kalshi_bot.db")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


#  API endpoints

@app.route("/api/state")
def api_state():
    """Return the full current bot state from bot_state.json plus config."""
    state = read_state()
    state["config"] = read_config()
    return jsonify(state)


@app.route("/api/status")
def api_status():
    """Aggregated status across the strategies in strategies.json: a per-strategy list
    (name, state_file, alive flag, state dict) plus a summary of counts and PnL totals.
    A strategy is 'alive' when its state file exists and its timestamp is under 60s old."""
    strategies = _load_strategies()
    now = time.time()
    results = []

    for s in strategies:
        name       = s.get("name", "unknown")
        state_file = s.get("state_file", f"bot_state_{name}.json")
        state      = _read_strategy_state(state_file)

        if state is None:
            alive = False
        else:
            # Consider stale if the timestamp in state is older than 60 seconds
            try:
                ts = datetime.fromisoformat(state.get("ts", "")).timestamp()
                alive = (now - ts) < 60
            except Exception:
                alive = False

        results.append({
            "name":       name,
            "state_file": state_file,
            "enabled":    s.get("enabled", False),
            "alive":      alive,
            "state":      state or {},
        })

    alive_count     = sum(1 for r in results if r["alive"])
    live_pnl_total  = sum(r["state"].get("today_live_pnl",  0.0) for r in results)
    paper_pnl_total = sum(r["state"].get("today_paper_pnl", 0.0) for r in results)
    demo_pnl_total  = sum(r["state"].get("today_demo_pnl",  0.0) for r in results)

    cfg = read_config()
    return jsonify({
        "strategies": results,
        "summary": {
            "total":             len(results),
            "alive":             alive_count,
            "today_live_pnl":    round(live_pnl_total,  2),
            "today_paper_pnl":   round(paper_pnl_total, 2),
            "today_demo_pnl":    round(demo_pnl_total,  2),
        },
        "running": cfg.get("bot_enabled", True),
        "mode":    cfg.get("mode", "paper"),
    })


@app.route("/api/trades")
def api_trades():
    """
    Return the last 500 trades ordered by ts descending.
    Optional query parameters: mode=live|paper, asset=BTC|ETH|..., strategy=1|2.
    """
    mode  = request.args.get("mode")
    asset    = request.args.get("asset", "").upper() or None
    strategy = request.args.get("strategy", "")
    strategy_variant = {"1": "strategy1", "2": "strategy2"}.get(strategy)
    try:
        conn = get_db()
        clauses, params = [], []
        if mode:
            clauses.append("mode=?"); params.append(mode)
        if asset:
            clauses.append("asset=?"); params.append(asset)
        if strategy_variant:
            clauses.append("strategy_variant=?"); params.append(strategy_variant)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM trades {where} ORDER BY ts DESC LIMIT 500",
            params,
        ).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as exc:
        if "no such table" in str(exc).lower():
            return jsonify([])
        return jsonify({"error": str(exc)}), 500


@app.route("/api/config", methods=["POST"])
def api_config():
    """
    Accept a JSON body with any subset of config fields, validate, save, and return
    the full updated config. Starts or stops the stress test thread as needed.
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No JSON body provided"}), 400

    config = read_config()
    prev_enabled = config.get("bot_enabled", False)
    prev_mode    = config.get("mode", "paper")
    errors = []

    def is_positive_number(v):
        return isinstance(v, (int, float)) and v > 0

    validators = {
        "trade_amount_dollars":        is_positive_number,
        "mode":                        lambda v: v in ("live", "paper", "demo"),
        "s1_mode":                     lambda v: v in ("live", "paper"),
        "s2_mode":                     lambda v: v in ("live", "paper"),
        "daily_loss_limit_dollars":    is_positive_number,
        "daily_profit_target_dollars": is_positive_number,
        "confidence_threshold":        lambda v: isinstance(v, (int, float)) and 0 <= v <= 100,
        "bot_enabled":                 lambda v: isinstance(v, bool),
        "quiet_hours_enabled":         lambda v: isinstance(v, bool),
        "quiet_start_et":              lambda v: isinstance(v, int) and 0 <= v <= 23,
        "quiet_end_et":                lambda v: isinstance(v, int) and 0 <= v <= 23,
        "min_ev_base":                 lambda v: isinstance(v, (int, float)) and 0 <= v <= 20,
        "vol_gate_thresh":             lambda v: isinstance(v, (int, float)) and 0.5 <= v <= 10,
    }

    # Action-based updates (enable_asset, disable_asset, set_asset_ev)
    action = data.get("action")
    if action in ("enable_asset", "disable_asset", "set_asset_ev"):
        asset = data.get("asset", "").upper()
        valid_assets = {"BTC", "ETH", "SOL", "XRP", "DOGE"}
        if asset not in valid_assets:
            return jsonify({"error": f"Unknown asset {asset!r}"}), 400
        enabled = config.setdefault("enabled_assets", ["ETH", "SOL", "XRP"])
        if action == "enable_asset":
            if asset not in enabled:
                enabled.append(asset)
        elif action == "disable_asset":
            config["enabled_assets"] = [a for a in enabled if a != asset]
        elif action == "set_asset_ev":
            value = data.get("value")
            if not isinstance(value, (int, float)) or not (0 <= value <= 20):
                return jsonify({"error": "value must be 0-20"}), 400
            config.setdefault("asset_overrides", {}).setdefault(asset, {})["min_ev_base"] = value
        try:
            write_config(config)
        except Exception as exc:
            return jsonify({"error": f"Could not save config: {exc}"}), 500
        log.info(f"Asset config action={action} asset={asset}")
        return jsonify(config)

    for key, value in data.items():
        if key not in validators:
            continue
        if validators[key](value):
            config[key] = value
        else:
            errors.append(f"Invalid value for {key!r}: {value!r}")

    if errors:
        return jsonify({"error": errors}), 400

    try:
        write_config(config)
    except Exception as exc:
        log.error(f"Config write failed: {exc}")
        return jsonify({"error": f"Could not save config: {exc}"}), 500

    # Persist bot_enabled to Railway volume so it survives redeploys
    if "bot_enabled" in data:
        _vol_db = os.environ.get("BOT_DB_FILE", "")
        if _vol_db:
            try:
                _vol_dir = os.path.dirname(os.path.abspath(_vol_db))
                with open(os.path.join(_vol_dir, "bot_enabled.state"), "w") as _sf:
                    _sf.write("1" if config["bot_enabled"] else "0")
            except Exception as _pe:
                log.warning(f"Could not persist bot_enabled to volume: {_pe}")

    log.info(f"Config updated: {data}")

    now_str = _display_now_str()
    new_enabled = config.get("bot_enabled", False)
    new_mode    = config.get("mode", "paper")

    if "bot_enabled" in data and new_enabled != prev_enabled:
        _telegram_notify(f"<b>Bot {'ENABLED' if new_enabled else 'DISABLED'}</b>  -  {now_str}\nMode: {new_mode.upper()}")

    if "mode" in data and new_mode != prev_mode:
        _telegram_notify(f"<b>Mode switched to {new_mode.upper()}</b>  -  {now_str}")

    return jsonify(config)


@app.route("/api/test-telegram", methods=["POST"])
def api_test_telegram():
    """Send a test Telegram message to verify notification config."""
    now_str = _display_now_str()
    try:
        notify.send_alert("INFO", f"\U0001f514 <b>Telegram test</b>  -  {now_str}\nBot notifications are working.")
        return jsonify({"ok": True, "message": "Test message sent"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/reset_pnl", methods=["POST"])
def api_reset_pnl():
    """
    Delete trades for a given mode or a specific asset.
    Body: {"mode": "live"|"paper"|"all"} OR {"asset": "BTC"|"ETH"|...}
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    # Per-asset reset (dashboard Quick Actions button)
    asset = (data.get("asset") or "").upper()
    if asset:
        if asset not in {"BTC", "ETH", "SOL", "XRP", "DOGE"}:
            return jsonify({"error": f"Unknown asset {asset!r}"}), 400
        try:
            conn = get_db()
            deleted = conn.execute("DELETE FROM trades WHERE asset = ?", (asset,)).rowcount
            conn.commit()
            conn.close()
            log.info(f"P&L reset for asset={asset}: {deleted} trades deleted")
            return jsonify({"ok": True, "asset": asset, "deleted_trades": deleted})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    if data.get("mode") not in ("live", "paper", "all"):
        return jsonify({"error": "mode must be 'live', 'paper', or 'all'"}), 400
    mode = data["mode"]
    try:
        conn = get_db()
        if mode == "all":
            deleted = conn.execute("DELETE FROM trades").rowcount
            conn.execute("DELETE FROM daily_summary")
            conn.execute("DELETE FROM market_log")
        else:
            deleted = conn.execute(
                "DELETE FROM trades WHERE mode = ?", (mode,)
            ).rowcount
            conn.execute(
                "DELETE FROM daily_summary WHERE mode = ?", (mode,)
            )
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "mode": mode, "deleted_trades": deleted})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/export/trades")
def api_export_trades():
    """Download the full trades table as a CSV file."""
    try:
        conn = get_db()
        rows = conn.execute("SELECT * FROM trades ORDER BY ts DESC").fetchall()
        conn.close()

        output = io.StringIO()
        if rows:
            writer = csv.DictWriter(output, fieldnames=rows[0].keys())
            writer.writeheader()
            for row in rows:
                writer.writerow(dict(row))

        output.seek(0)
        return app.response_class(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment;filename=kalshi_trades_export.csv"},
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/")
def index():
    """Serve the dashboard HTML."""
    try:
        path = os.path.join(_BASE_DIR, "handoff", "Money Printer.html")
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        from flask import Response
        return Response(content, mimetype="text/html", headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        })
    except Exception as exc:
        log.error(f"index() failed: {exc}", exc_info=True)
        return f"<h1>Error: {exc}</h1>", 500


@app.route("/api/config", methods=["GET"])
def api_config_get():
    """Return the current config."""
    return jsonify(read_config())






@app.route("/api/pnl")
def api_pnl():
    """
    Return PnL summary:
      - today: today's PnL broken down by asset and mode
      - alltime: all-time totals
      - win_rate: overall win rate (resolved trades only)
    """
    strategy = request.args.get("strategy", "")
    strategy_variant = {"1": "strategy1", "2": "strategy2"}.get(strategy)
    try:
        # "Today" is the ET trading day - the same window the bot's daily P&L,
        # limits and Telegram summary use. Bucketing by UTC date put every
        # post-8pm-ET trade in tomorrow's tile.
        _et_now = datetime.now(ZoneInfo("America/New_York"))
        today = _et_now.strftime("%Y-%m-%d")
        _day_start = datetime(_et_now.year, _et_now.month, _et_now.day,
                              tzinfo=ZoneInfo("America/New_York")
                              ).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY ts DESC LIMIT 2000"
        ).fetchall()
        conn.close()

        all_raw    = [dict(r) for r in rows]
        all_trades = (
            [t for t in all_raw if t.get("strategy_variant", "strategy2") == strategy_variant]
            if strategy_variant else all_raw
        )

        def _pnl(trades):
            resolved = [t for t in trades if t.get("outcome") not in ("pending", None) and t.get("pnl_dollars") is not None]
            total = round(sum(t["pnl_dollars"] for t in resolved), 2)
            wins  = sum(1 for t in resolved if t["pnl_dollars"] > 0)
            count = len(resolved)
            win_rate = round(wins / count * 100, 1) if count else 0.0
            return {"pnl": total, "trades": count, "wins": wins, "win_rate": win_rate}

        today_trades = [t for t in all_trades if (t.get("ts") or "") >= _day_start]

        # Per-asset today
        cfg = read_config()
        enabled_assets = cfg.get("enabled_assets", ["ETH", "SOL", "XRP"])
        today_by_asset = {}
        for asset in enabled_assets:
            a_trades = [t for t in today_trades if t.get("asset") == asset]
            today_by_asset[asset] = _pnl(a_trades)

        # Per-mode today
        today_live  = _pnl([t for t in today_trades if t.get("mode") == "live"])
        today_paper = _pnl([t for t in today_trades if t.get("mode") == "paper"])
        today_demo  = _pnl([t for t in today_trades if t.get("mode") == "demo"])

        # All-time
        alltime_live  = _pnl([t for t in all_trades if t.get("mode") == "live"])
        alltime_paper = _pnl([t for t in all_trades if t.get("mode") == "paper"])
        alltime_demo  = _pnl([t for t in all_trades if t.get("mode") == "demo"])

        response = {
            "today": {
                "live":     today_live,
                "paper":    today_paper,
                "demo":     today_demo,
                "by_asset": today_by_asset,
                "date":     today,
            },
            "alltime": {
                "live":  alltime_live,
                "paper": alltime_paper,
                "demo":  alltime_demo,
            },
        }

        # When no strategy filter, include per-strategy breakdown for dashboard
        if not strategy_variant:
            by_strategy = {}
            for sv in ("strategy1", "strategy2"):
                sv_trades = [t for t in all_raw if t.get("strategy_variant", "strategy2") == sv]
                sv_today  = [t for t in sv_trades if (t.get("ts") or "").startswith(today)]
                by_strategy[sv] = {
                    "today":   _pnl(sv_today),
                    "alltime": _pnl(sv_trades),
                }
            response["by_strategy"] = by_strategy

        return jsonify(response)
    except Exception as exc:
        if "no such table" in str(exc).lower():
            empty = {"pnl": 0.0, "trades": 0, "wins": 0, "win_rate": 0.0}
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            return jsonify({
                "today":   {"live": empty, "paper": empty, "demo": empty, "by_asset": {}, "date": today},
                "alltime": {"live": empty, "paper": empty, "demo": empty},
                "by_strategy": {
                    "strategy1": {"today": empty, "alltime": empty},
                    "strategy2": {"today": empty, "alltime": empty},
                },
            })
        log.error(f"api_pnl error: {exc}", exc_info=True)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/markets")
def api_markets():
    """
    Return per-asset market state.
    Reads bot_state.json's 'assets' key if present.
    Falls back to enabled_assets from config with OFFLINE status.
    """
    try:
        state = read_state()
        cfg   = read_config()
        enabled_assets = cfg.get("enabled_assets", ["ETH", "SOL", "XRP"])

        assets = state.get("assets", {})

        # Ensure all enabled assets are represented (fill with OFFLINE if missing)
        result = {}
        for asset in enabled_assets:
            if asset in assets:
                result[asset] = assets[asset]
            else:
                result[asset] = {
                    "phase":     "OFFLINE",
                    "price":     None,
                    "ticker":    None,
                    "market_title": None,
                    "secs_left": 0,
                    "price_age": None,
                }

        return jsonify(result)
    except Exception as exc:
        log.error(f"api_markets error: {exc}", exc_info=True)
        return jsonify({"error": str(exc)}), 500


def _frontend_signals(a: dict) -> dict:
    """
    Map the bot's per-asset eval snapshot onto the schema the dashboard's
    Decision Signals panel (renderSignals) expects. Projects side-relative
    probabilities (win_prob, market mid) onto a consistent P(YES) basis.
    """
    sig = a.get("signals") or {}
    direction = (a.get("direction") or "").upper()
    up = direction == "UP"

    def _yes(p_side):
        """Project a side-relative probability onto P(YES)."""
        if p_side is None:
            return None
        return float(p_side) if up else 1.0 - float(p_side)

    ev = a.get("ev")  # chosen-side EV in percent
    win_prob = a.get("win_prob")  # chosen-side prob in percent
    s1g = a.get("s1_gates") or {}
    s2g = a.get("s2_gates") or {}
    votes = min(5, int(s1g.get("passed", 0) or 0) + int(s2g.get("passed", 0) or 0))
    status = (a.get("status") or "").upper()

    p_ev = _yes((win_prob / 100.0) if win_prob is not None else sig.get("win_prob"))
    market_prob = _yes(sig.get("mkt_p"))
    raw_p_yes = sig.get("model_raw_p_yes")

    s1_dir = (a.get("s1_dir") or "").lower()
    s2_dir = (a.get("s2_dir") or "").lower()
    supertrend = 1 if "up" in s1_dir or s1_dir == "yes" else (-1 if "down" in s1_dir or s1_dir == "no" else 0)
    velocity = "rising" if "up" in s2_dir or s2_dir == "yes" else ("falling" if "down" in s2_dir or s2_dir == "no" else "flat")

    return {
        "final_decision": "trade" if status == "TRADING" else "skip",
        "vote_count": votes,
        "ev_pass": bool(ev is not None and ev > 0),
        "skip_reason": a.get("skip_reason") or None,
        "decision_mode": "S1+S2",
        "raw_p_yes": raw_p_yes if raw_p_yes is not None else 0.5,
        "p_ev": p_ev if p_ev is not None else 0.5,
        "market_prob": market_prob if market_prob is not None else 0.5,
        # Bot computes EV for the chosen side only; show it on that side, 0 on the other
        # (numeric, never None - the frontend calls .toFixed on both).
        "yes_ev": float(ev) if (ev is not None and up) else 0.0,
        "no_ev": float(ev) if (ev is not None and not up) else 0.0,
        "supertrend": supertrend,
        "velocity": velocity,
    }


@app.route("/api/market-state")
def api_market_state():
    """
    Rich per-asset state for dashboard market cards.
    Includes eval metrics (ev, win_prob, direction, yes/no ask, etc.)
    and today's per-asset PnL from the DB.
    """
    try:
        state = read_state()
        cfg   = read_config()
        enabled_assets = cfg.get("enabled_assets", ["ETH", "SOL", "XRP"])
        assets = state.get("assets", {})

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily: dict = {}
        try:
            conn = get_db()
            rows = conn.execute(
                "SELECT asset, outcome, pnl_dollars FROM trades WHERE ts LIKE ?",
                (today + "%",)
            ).fetchall()
            conn.close()
            for row in rows:
                a = row["asset"] or "BTC"
                if a not in daily:
                    daily[a] = {"wins": 0, "losses": 0, "pnl": 0.0}
                if row["outcome"] == "win":
                    daily[a]["wins"] += 1
                elif row["outcome"] == "loss":
                    daily[a]["losses"] += 1
                if row["pnl_dollars"] is not None:
                    daily[a]["pnl"] = round(daily[a]["pnl"] + row["pnl_dollars"], 2)
        except Exception:
            pass

        result = {}
        for asset in enabled_assets:
            a_state = assets.get(asset, {"phase": "OFFLINE", "price": None})
            _merged_sig = {**(a_state.get("signals") or {}), **_frontend_signals(a_state)}
            result[asset] = {**a_state, "signals": _merged_sig,
                             "daily": daily.get(asset, {"wins": 0, "losses": 0, "pnl": 0.0})}

        return jsonify({"assets": result, "ts": state.get("ts"), "bot_enabled": cfg.get("bot_enabled", False)})
    except Exception as exc:
        log.error(f"api_market_state error: {exc}", exc_info=True)
        # Return OFFLINE placeholder for all known assets so the JS grid always renders
        _default_assets = {a: {"phase": "OFFLINE", "price": None, "daily": {"wins": 0, "losses": 0, "pnl": 0.0}}
                           for a in ["BTC", "ETH", "SOL", "XRP"]}
        return jsonify({"assets": _default_assets, "ts": None, "bot_enabled": False})


@app.route("/api/market-pulse")
def api_market_pulse():
    """Skip counter, last 5 resolved trades, price validation stats."""
    try:
        state  = read_state()
        result = {
            "last_action":      state.get("last_action", ""),
            "last_skip_reason": state.get("last_skip_reason", ""),
            "consecutive_losses": state.get("consecutive_losses", 0),
            "recent_trades":    [],
            "price_validation": None,
            "ts":               state.get("ts"),
        }
        try:
            conn = get_db()
            rows = conn.execute(
                "SELECT ts, asset, side, pnl_dollars, outcome, entry_price_cents, mode "
                "FROM trades WHERE outcome IN ('win','loss') ORDER BY ts DESC LIMIT 5"
            ).fetchall()
            conn.close()
            result["recent_trades"] = [dict(r) for r in rows]
        except Exception:
            pass
        try:
            _db_dir = os.path.dirname(os.path.abspath(os.environ.get("BOT_DB_FILE", "kalshi_bot.db")))
            pv_path = os.path.join(_db_dir, "price_validation_log.csv")
            if os.path.exists(pv_path):
                import csv as _csv
                with open(pv_path) as _f:
                    _rdr = _csv.DictReader(_f)
                    _rows = list(_rdr)
                if _rows:
                    _gaps = [float(r["price_gap_cents"]) for r in _rows
                             if r.get("price_gap_cents") not in ("", None)]
                    avg_gap = round(sum(_gaps) / len(_gaps), 2) if _gaps else 0
                    if avg_gap < 3:
                        verdict = "VALIDATED"
                    elif avg_gap <= 5:
                        verdict = "MARGINAL"
                    else:
                        verdict = "UNRELIABLE"
                    result["price_validation"] = {
                        "count": len(_rows),
                        "avg_gap": avg_gap,
                        "verdict": verdict,
                    }
        except Exception:
            pass
        return jsonify(result)
    except Exception as exc:
        log.error(f"api_market_pulse error: {exc}", exc_info=True)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/market/<sym>")
def api_market_sym(sym):
    """
    Per-asset detail: sessions, stats, recent log, last 60 outcomes for heatmap.
    Ladder returns empty (requires live Kalshi orderbook feed).
    """
    sym = sym.upper()
    try:
        state  = read_state()
        a      = state.get("assets", {}).get(sym, {})
        today  = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        secs = a.get("secs_left", 0) or 0
        if secs > 0:
            exp_dt  = datetime.now(timezone.utc) + timedelta(seconds=secs)
            expires = exp_dt.strftime("%H:%M:%S UTC")
        else:
            expires = "-"

        sessions = [{
            "type":       a.get("session_type", "15m"),
            "active":     a.get("phase") == "LOCKED",
            "expires":    expires,
            "strike":     a.get("strike"),
            "dist":       a.get("distance_pct"),
            "ev":         a.get("ev"),
            "wp":         a.get("win_prob"),
            "yesAsk":     a.get("yes_ask"),
            "noAsk":      a.get("no_ask"),
            "qty":        0,
            "vol":        None,
            "score":      None,
            "skipReason": a.get("skip_reason"),
        }]

        conn = get_db()

        # Stats
        try:
            all_rows = conn.execute(
                "SELECT outcome, pnl_dollars, model_prob FROM trades "
                "WHERE asset=? AND outcome IN ('win','loss') AND pnl_dollars IS NOT NULL",
                (sym,)
            ).fetchall()
            today_rows = conn.execute(
                "SELECT pnl_dollars FROM trades "
                "WHERE asset=? AND ts LIKE ? AND outcome IN ('win','loss') AND pnl_dollars IS NOT NULL",
                (sym, today + "%")
            ).fetchall()
            wins    = sum(1 for r in all_rows if r["outcome"] == "win")
            total   = len(all_rows)
            losses  = total - wins
            wr      = round(wins / total, 3) if total else 0.0
            today_p = round(sum(r["pnl_dollars"] for r in today_rows), 2)
            avg_ev  = round(sum((r["model_prob"] or 0) * 100 for r in all_rows) / total, 1) if total else 0.0
            best    = round(max((r["pnl_dollars"] for r in all_rows), default=0.0), 2)
            worst   = round(min((r["pnl_dollars"] for r in all_rows), default=0.0), 2)
            stats   = {
                "wins": wins, "losses": losses, "wr": wr,
                "todayPnl": today_p, "avgEV": avg_ev,
                "bestExit": f"+${best:.2f}" if best >= 0 else f"-${abs(best):.2f}",
                "worstDD": worst,
            }
        except Exception:
            stats = {"wins": 0, "losses": 0, "wr": 0.0, "todayPnl": 0.0,
                     "avgEV": 0.0, "bestExit": "+$0.00", "worstDD": 0.0}

        # Recent log
        try:
            log_rows = conn.execute(
                "SELECT ts, phase, action, skip_reason, confidence_score FROM market_log "
                "WHERE market_id LIKE ? ORDER BY ts DESC LIMIT 8",
                (f"%{sym}%",)
            ).fetchall()
            log_entries = []
            for r in log_rows:
                t = (r["ts"] or "")
                time_str = t[11:16] if len(t) >= 16 else "-"
                if r["action"] in ("trade", "entry"):
                    tag = "entry"
                elif r["action"] in ("skip", "watch"):
                    tag = "skip"
                else:
                    tag = "signal"
                msg = r["skip_reason"] or r["action"] or r["phase"] or ""
                log_entries.append([time_str, tag, msg])
        except Exception:
            log_entries = []

        # Last 60 outcomes for heatmap (newest first)
        try:
            heat_rows = conn.execute(
                "SELECT outcome FROM trades WHERE asset=? ORDER BY ts DESC LIMIT 60",
                (sym,)
            ).fetchall()
            outcomes = [r["outcome"] for r in heat_rows]
        except Exception:
            outcomes = []

        # Brain P&L breakdown for this asset
        brain_alltime = {}
        brain_today_q = {}
        try:
            at_rows = conn.execute(
                "SELECT brain, COUNT(*) AS trades, COALESCE(SUM(pnl_dollars),0) AS pnl, "
                "SUM(CASE WHEN pnl_dollars > 0 THEN 1 ELSE 0 END) AS wins, "
                "SUM(CASE WHEN pnl_dollars < 0 THEN 1 ELSE 0 END) AS losses "
                "FROM trades WHERE asset=? AND brain IN ('s1','s2') "
                "AND pnl_dollars IS NOT NULL GROUP BY brain",
                (sym,)
            ).fetchall()
            td_rows = conn.execute(
                "SELECT brain, COUNT(*) AS trades, COALESCE(SUM(pnl_dollars),0) AS pnl, "
                "SUM(CASE WHEN pnl_dollars > 0 THEN 1 ELSE 0 END) AS wins, "
                "SUM(CASE WHEN pnl_dollars < 0 THEN 1 ELSE 0 END) AS losses "
                "FROM trades WHERE asset=? AND brain IN ('s1','s2') "
                "AND pnl_dollars IS NOT NULL AND date(ts) = ? GROUP BY brain",
                (sym, today)
            ).fetchall()
            for r in at_rows:
                brain_alltime[r["brain"]] = {"trades": r["trades"], "pnl": round(r["pnl"], 2), "wins": r["wins"], "losses": r["losses"]}
            for r in td_rows:
                brain_today_q[r["brain"]] = {"trades": r["trades"], "pnl": round(r["pnl"], 2), "wins": r["wins"], "losses": r["losses"]}
        except Exception:
            pass

        conn.close()

        return jsonify({
            "sym":      sym,
            "sessions": sessions,
            "ladder":   {"asks": [], "bids": []},
            "log":      log_entries,
            "stats":    stats,
            "outcomes": outcomes,
            "brain_stats": {
                "s1": {
                    "alltime": brain_alltime.get("s1", {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0}),
                    "today":   brain_today_q.get("s1", {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0}),
                },
                "s2": {
                    "alltime": brain_alltime.get("s2", {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0}),
                    "today":   brain_today_q.get("s2", {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0}),
                },
            },
        })
    except Exception as exc:
        log.error(f"api_market_sym({sym}) error: {exc}", exc_info=True)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/equity")
def api_equity():
    """
    Cumulative P&L curve for the equity chart.
    Query param: range = 1d | 1w | 1m | all
    Returns: { range, points: [float, ...], x_labels: [str, ...] }
    """
    range_ = request.args.get("range", "1d")
    try:
        now = datetime.now(timezone.utc)
        if range_ == "1d":
            since = now - timedelta(hours=24)
            bucket_sql = "strftime('%H:00', ts)"
            n_buckets = 24
            labels = [f"{h:02d}:00" for h in range(0, 24, 4)]
        elif range_ == "1w":
            since = now - timedelta(days=7)
            bucket_sql = "strftime('%Y-%m-%d', ts)"
            n_buckets = 7
            labels = [f"{(now-timedelta(days=i)).day} {(now-timedelta(days=i)).strftime('%b')}" for i in range(6, -1, -1)]
        elif range_ == "1m":
            since = now - timedelta(days=30)
            bucket_sql = "strftime('%Y-%m-%d', ts)"
            n_buckets = 30
            labels = [f"{(now-timedelta(days=i*7)).day} {(now-timedelta(days=i*7)).strftime('%b')}" for i in range(4, -1, -1)]
        else:  # all
            since = datetime(2024, 1, 1, tzinfo=timezone.utc)
            bucket_sql = "strftime('%Y-%W', ts)"
            n_buckets = None
            labels = []

        since_str = since.strftime("%Y-%m-%dT%H:%M:%S")
        conn = get_db()
        rows = conn.execute(
            f"SELECT {bucket_sql} AS bucket, SUM(pnl_dollars) AS bucket_pnl "
            "FROM trades "
            "WHERE ts >= ? AND outcome IN ('win','loss') AND pnl_dollars IS NOT NULL "
            "GROUP BY bucket ORDER BY bucket",
            (since_str,),
        ).fetchall()
        conn.close()

        # Build cumulative series
        points = []
        cumulative = 0.0
        for row in rows:
            cumulative = round(cumulative + (row["bucket_pnl"] or 0.0), 2)
            points.append(cumulative)

        if not points:
            points = [0.0]
        else:
            points = [0.0] + points  # ensure length >= 2 so dashboard renders the curve

        return jsonify({"range": range_, "points": points, "x_labels": labels})
    except Exception as exc:
        log.error(f"api_equity error: {exc}", exc_info=True)
        return jsonify({"range": range_, "points": [0.0], "x_labels": []}), 200


@app.route("/api/risk")
def api_risk():
    """
    Risk status for the Overview Risk Status card.
    Returns daily loss/profit vs limits, EV floor, vol gate config, and win/loss streak.
    """
    try:
        cfg = read_config()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        loss_limit  = cfg.get("daily_loss_limit_dollars", 0.0)
        profit_target = cfg.get("daily_profit_target_dollars", 200.0)
        ev_floor    = cfg.get("min_ev_base", 8)
        vol_thresh  = cfg.get("vol_gate_thresh", 1.80)

        today_pnl   = 0.0
        today_loss  = 0.0
        streak_type = "W"
        streak_count = 0

        try:
            conn = get_db()
            rows = conn.execute(
                "SELECT outcome, pnl_dollars FROM trades "
                "WHERE ts LIKE ? AND outcome IN ('win','loss') AND pnl_dollars IS NOT NULL "
                "ORDER BY ts DESC LIMIT 200",
                (today + "%",),
            ).fetchall()
            recent = conn.execute(
                "SELECT outcome FROM trades WHERE outcome IN ('win','loss') "
                "ORDER BY ts DESC LIMIT 50"
            ).fetchall()
            conn.close()

            for r in rows:
                today_pnl = round(today_pnl + r["pnl_dollars"], 2)
                if r["pnl_dollars"] < 0:
                    today_loss = round(today_loss + abs(r["pnl_dollars"]), 2)

            if recent:
                first = recent[0]["outcome"]
                streak_type = "W" if first == "win" else "L"
                for r in recent:
                    if r["outcome"] == ("win" if streak_type == "W" else "loss"):
                        streak_count += 1
                    else:
                        break
        except Exception:
            pass

        state = read_state()
        assets = state.get("assets", {})
        vol_asset = "BTC"
        vol_current = None
        for sym, a in assets.items():
            v = (a or {}).get("vol_ratio") or (a or {}).get("vol")
            if v is not None:
                vol_current = round(float(v), 2)
                vol_asset = sym
                break

        return jsonify({
            "daily_loss_limit":    {"current": today_loss,  "max": loss_limit},
            "daily_profit_target": {"current": max(today_pnl, 0.0), "max": profit_target},
            "vol_gate":            {"current": vol_current, "threshold": vol_thresh, "asset": vol_asset},
            "ev_floor":            {"current": ev_floor,    "pct": min(100, int(ev_floor / 10 * 100))},
            "streak":              {"type": streak_type,    "count": streak_count},
        })
    except Exception as exc:
        log.error(f"api_risk error: {exc}", exc_info=True)
        return jsonify({"error": str(exc)}), 500


@app.route("/metrics")
def metrics():
    """Lightweight JSON metrics for uptime, trading activity, config, and recent errors."""
    try:
        cfg = read_config()
        state_file = os.environ.get("BOT_STATE_FILE", "bot_state.json")
        db_path = os.environ.get("BOT_DB_FILE", "kalshi_bot.db")

        last_trade_ts = None
        trade_count_24h = 0
        fill_confirmed_rate_24h = None
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT MAX(ts) AS mx FROM trades").fetchone()
            if row:
                last_trade_ts = row["mx"]
            row24 = conn.execute(
                "SELECT COUNT(*) AS cnt, SUM(fill_confirmed) AS fc "
                "FROM trades WHERE ts > datetime('now', '-24 hours')"
            ).fetchone()
            if row24 and row24["cnt"]:
                trade_count_24h = row24["cnt"]
                fc = row24["fc"] or 0
                fill_confirmed_rate_24h = round(fc / trade_count_24h, 4)
            conn.close()
        except Exception:
            pass

        bot_state_age = None
        try:
            bot_state_age = round(time.time() - os.path.getmtime(state_file), 1)
        except OSError:
            pass

        err = obs.get_last_error()
        return jsonify({
            "uptime_seconds":          round(time.time() - _PROCESS_START, 1),
            "last_trade_ts":           last_trade_ts,
            "trade_count_24h":         trade_count_24h,
            "fill_confirmed_rate_24h": fill_confirmed_rate_24h,
            "bot_enabled":             cfg.get("bot_enabled", False),
            "mode":                    cfg.get("mode", "paper"),
            "bot_state_age_seconds":   bot_state_age,
            "last_error_ts":           err["ts"]  if err else None,
            "last_error_msg":          err["msg"] if err else None,
        })
    except Exception as exc:
        log.error(f"metrics error: {exc}", exc_info=True)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/debug/gates")
def api_debug_gates():
    """
    Real-time gate status per asset and strategy.
    Useful for diagnosing why trades are not firing.
    Uses price=strike so abs_pct=0; shows which gate fires first.
    """
    try:
        import bot_state
        from bot_strategy import strategy_brain_s1, strategy_brain_s2
        from bot_market import get_btc_price
        import asset_manager

        cfg = read_config()
        enabled = cfg.get("enabled_assets", ["ETH", "SOL", "XRP"])
        btc_price = get_btc_price() or 100000.0
        result = {}

        for asset in enabled:
            price = (btc_price if asset == "BTC"
                     else (asset_manager.get_price(asset) or 1.0))
            strike = price  # dist=0 so only non-dist gates can pass

            try:
                s1 = strategy_brain_s1(
                    price, strike, 55.0, 45.0,
                    30.0, 360.0, f"DBG-{asset}", asset=asset,
                )
            except Exception as exc:
                s1 = {"action": "error", "reasoning": str(exc), "abs_pct": 0}

            try:
                s2 = strategy_brain_s2(
                    price, strike, 55.0, 45.0,
                    30.0, 360.0, f"DBG-{asset}", asset=asset,
                )
            except Exception as exc:
                s2 = {"action": "error", "reasoning": str(exc), "abs_pct": 0}

            result[asset] = {
                "price":  round(float(price), 4),
                "s1": {"action": s1.get("action"), "gate": s1.get("reasoning", "")},
                "s2": {"action": s2.get("action"), "gate": s2.get("reasoning", "")},
            }

        return jsonify({
            "assets": result,
            "config": {
                "bot_enabled":         cfg.get("bot_enabled", False),
                "mode":                cfg.get("mode", "paper"),
                "quiet_hours_enabled": cfg.get("quiet_hours_enabled", True),
                "quiet_hours_active":  False,
            },
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/trade-stats")
def api_trade_stats():
    """Return 24h win/loss/WR/PnL and top-5 skip reasons from brain.log tail."""
    import re as _re
    config = read_config()
    mode = config.get("mode", "paper")

    # DB stats: wins/losses/PnL last 24h
    wins = losses = 0
    pnl_24h = 0.0
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = get_db()
        rows = conn.execute(
            "SELECT outcome, COUNT(*), COALESCE(SUM(pnl_dollars), 0) "
            "FROM trades WHERE mode=? AND ts > ? AND outcome != 'pending' "
            "GROUP BY outcome",
            (mode, since),
        ).fetchall()
        conn.close()
        for outcome, count, pnl in rows:
            if outcome == "win":
                wins, pnl_24h = count, pnl_24h + pnl
            elif outcome == "loss":
                losses, pnl_24h = count, pnl_24h + pnl
    except Exception as exc:
        log.warning("api_trade_stats DB error: %s", exc)

    total = wins + losses
    wr = round(wins / total, 3) if total else None

    # Brain log: skip reasons from last 500 lines
    skip_counts: dict = {}
    try:
        import bot_strategy as _bs
        brain_log_path = getattr(_bs, "_brain_log_path", "brain.log")
        with open(brain_log_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-500:]
        for line in lines:
            if "TRADE" in line or "SETTLE" in line:
                continue
            m = _re.search(r"\b(s[12]_\w+)", line)
            if m:
                key = m.group(1).split(":")[0]
                skip_counts[key] = skip_counts.get(key, 0) + 1
    except Exception:
        pass

    top_skips = sorted(skip_counts.items(), key=lambda x: -x[1])[:5]

    payload = {
        "mode": mode,
        "period_hours": 24,
        "wins": wins,
        "losses": losses,
        "total": total,
        "win_rate": wr,
        "pnl_dollars": round(pnl_24h, 2),
        "top_skip_reasons": [{"reason": r, "count": c} for r, c in top_skips],
    }
    return jsonify(payload)


def _safe(x, nd=4):
    """Round, mapping NaN/None -> None so the JSON is valid (no NaN tokens)."""
    if x is None or (isinstance(x, float) and x != x):
        return None
    return round(x, nd)


@app.route("/api/edge")
def api_edge():
    """
    Surface the edge-measurement harness for the dashboard GATE-1 panel:
    decision_log calibration + net-of-fee edge (Wilson LB) and the maker_log counterfactual.
    Reuses the exact math from scripts/edge_report.py and scripts/maker_report.py.
    Always returns HTTP 200 with an 'insufficient data' shape when tables are empty/missing.
    """
    _empty = {
        "decisions": {"by_strategy": {}, "by_session": {}, "by_daytype": {},
                      "overall": None, "verdict": "insufficient data"},
        "maker": {"by_strategy": {}, "overall": None, "verdict": "insufficient data"},
        "shadow": {"s_fav": None, "verdict": "insufficient data"},
        "sigma": {},
        "calibration": {},
        "basis": {},
        "counts": {"logged": 0, "settled": 0, "pending": 0},
    }
    try:
        from scripts.edge_report import wilson_lower, _win, _p_side, _kalshi_fee, MIN_BUCKET_N
        import sessions
    except Exception as exc:
        # scripts/ isn't part of the "15 live flat files" contract - degrade to 200, not 500.
        log.warning("api_edge: edge_report import unavailable: %s", exc)
        return jsonify(_empty)
    try:
        from scripts.maker_report import _stats as _maker_stats
    except Exception:
        _maker_stats = None

    def _decision_group(rows):
        n = len(rows)
        if n == 0:
            return None
        wins = sum(_win(r["side"], r["outcome"]) for r in rows)
        mean_model = sum(_p_side(r["model_p_yes"], r["side"]) for r in rows) / n
        mkt = [r for r in rows if r["market_mid_p_yes"] is not None]
        mean_mkt = (sum(_p_side(r["market_mid_p_yes"], r["side"]) for r in mkt) / len(mkt)) if mkt else None
        brier_m = sum((_p_side(r["model_p_yes"], r["side"]) - _win(r["side"], r["outcome"])) ** 2 for r in rows) / n
        brier_k = (sum((_p_side(r["market_mid_p_yes"], r["side"]) - _win(r["side"], r["outcome"])) ** 2 for r in mkt) / len(mkt)) if mkt else None
        pnls, entries = [], []
        for r in rows:
            if r["entry_price_cents"] is None:
                continue
            e = r["entry_price_cents"] / 100.0
            won = _win(r["side"], r["outcome"])
            pnls.append((1.0 - e if won else -e) - _kalshi_fee(e))
            entries.append(e)
        mean_pnl = sum(pnls) / len(pnls) if pnls else None
        mean_entry = sum(entries) / len(entries) if entries else None
        wlb = wilson_lower(wins, n)
        pnl_wlb = (wlb * (1 - mean_entry) - (1 - wlb) * mean_entry - _kalshi_fee(mean_entry)) if mean_entry is not None else None
        return {
            "n": n, "win_rate": _safe(wins / n, 3),
            "mean_model_p": _safe(mean_model, 3), "mean_market_p": _safe(mean_mkt, 3),
            "brier_model": _safe(brier_m), "brier_market": _safe(brier_k),
            "net_pnl_per_contract": _safe(mean_pnl), "wilson_lb_pnl": _safe(pnl_wlb),
        }

    def _round_maker(st):
        return {
            "n": st["n"], "fill_rate": _safe(st["fill_rate"], 3),
            "taker_mean": _safe(st["taker_mean"]), "maker_strategy_mean": _safe(st["ms_mean"]),
            "delta": _safe(st["delta"]), "delta_se": _safe(st["delta_se"]),
            "maker_filled_mean": _safe(st["maker_filled_mean"]),
        }

    result = {
        "decisions": {"by_strategy": {}, "by_session": {}, "by_daytype": {},
                      "overall": None, "verdict": "insufficient data"},
        "maker": {"by_strategy": {}, "overall": None, "verdict": "insufficient data"},
        "shadow": {"s_fav": None, "verdict": "insufficient data"},
        "sigma": {},
        "calibration": {},
        "basis": {},
        "counts": {"logged": 0, "settled": 0, "pending": 0},
    }
    try:
        conn = get_db()
        try:
            result["counts"]["logged"] = conn.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0]
            result["counts"]["settled"] = conn.execute(
                "SELECT COUNT(*) FROM decision_log WHERE outcome IN ('yes','no')").fetchone()[0]
            result["counts"]["pending"] = conn.execute(
                "SELECT COUNT(*) FROM decision_log WHERE outcome='pending'").fetchone()[0]
            picks = conn.execute(
                "SELECT ts, strategy, side, model_p_yes, market_mid_p_yes, entry_price_cents, outcome "
                "FROM decision_log WHERE outcome IN ('yes','no') AND would_trade=1 "
                "AND model_p_yes IS NOT NULL AND side IS NOT NULL "
                "AND entry_price_cents IS NOT NULL").fetchall()
        except sqlite3.OperationalError:
            picks = []
        groups = {}
        for r in picks:
            groups.setdefault(r["strategy"] or "?", []).append(r)
        for strat, rs in groups.items():
            g = _decision_group(rs)
            if g:
                result["decisions"]["by_strategy"][strat] = g
        overall = _decision_group(picks)
        result["decisions"]["overall"] = overall
        if overall:
            n, net, lb = overall["n"], overall["net_pnl_per_contract"], overall["wilson_lb_pnl"]
            if n < 200:
                result["decisions"]["verdict"] = f"insufficient data ({n}/200 picks)"
            elif lb is not None and lb > 0:
                result["decisions"]["verdict"] = "edge: net positive (Wilson LB > 0)"
            elif net is not None and net > 0:
                result["decisions"]["verdict"] = "marginal: positive but LB <= 0"
            else:
                result["decisions"]["verdict"] = "no edge: net <= 0"

        # Per-time breakdowns - which ET sessions / day-types actually pay (the "better
        # market times" view). Each bucket carries an 'insufficient' flag below MIN_BUCKET_N.
        def _bucketed(bucketer):
            b = {}
            for r in picks:
                key = bucketer(r["ts"])
                if key is None:
                    continue
                b.setdefault(key, []).append(r)
            out = {}
            for key, rs in b.items():
                g = _decision_group(rs)
                if g:
                    g["insufficient"] = g["n"] < MIN_BUCKET_N
                    out[key] = g
            return out
        result["decisions"]["by_session"] = _bucketed(sessions.session_for_iso)
        result["decisions"]["by_daytype"] = _bucketed(sessions.day_type_for_iso)

        # Shadow strategy scoreboard: s_fav rows are logged with would_trade=0 (zero
        # capital), so the picks-based stats above never see them. This block is the
        # promotion criterion for the buy-the-favorite play.
        try:
            srows = conn.execute(
                "SELECT ts, strategy, side, model_p_yes, market_mid_p_yes, "
                "entry_price_cents, outcome FROM decision_log "
                "WHERE strategy='s_fav' AND outcome IN ('yes','no') "
                "AND side IS NOT NULL AND entry_price_cents IS NOT NULL").fetchall()
        except sqlite3.OperationalError:
            srows = []
        sg = _decision_group(srows)
        if sg:
            # The bias premium: realized win rate minus the market's own price of the side.
            if sg["win_rate"] is not None and sg["mean_market_p"] is not None:
                sg["premium"] = _safe(sg["win_rate"] - sg["mean_market_p"], 3)
            result["shadow"]["s_fav"] = sg
            n, lb = sg["n"], sg["wilson_lb_pnl"]
            if n < 200:
                result["shadow"]["verdict"] = f"collecting ({n}/200 settled)"
            elif lb is not None and lb > 0:
                result["shadow"]["verdict"] = "promotion candidate: Wilson LB > 0 at n>=200"
            else:
                result["shadow"]["verdict"] = "no premium: Wilson LB <= 0"

        # Fitted model calibration (written by the bot's recalibration job).
        try:
            from scripts.calibration import load_calibration
            result["calibration"] = load_calibration()
        except Exception:
            pass

        # Sigma engine state per asset: fitted scale + market-implied EWMA vs the
        # static cold-start base. Server and bot are separate processes, so the
        # persisted calibration.json is the channel for the live values.
        try:
            from bot_strategy import _ASSET_VOL_15M as _static_vol
        except Exception:
            _static_vol = {}
        cal = result["calibration"] or {}
        _scales = cal.get("sigma_scale") or {}
        _imp = cal.get("implied_sigma") or {}
        for asset in sorted(set(_scales) | set(_imp) | set(_static_vol)):
            entry = {"static": _safe(_static_vol.get(asset), 5)}
            if asset in _scales:
                entry["sigma_scale"] = _safe(_scales.get(asset), 3)
            ie = _imp.get(asset)
            if isinstance(ie, dict) and ie.get("sigma"):
                entry["implied"] = _safe(ie.get("sigma"), 5)
                entry["implied_n"] = ie.get("n")
                entry["implied_ts"] = ie.get("ts")
            result["sigma"][asset] = entry

        # Settlement basis: per-asset agreement between our spot-implied side and
        # Kalshi's official result, from the settlement_basis table.
        try:
            brows = conn.execute(
                "SELECT asset, agree, signed_dist FROM settlement_basis "
                "WHERE kalshi IN ('yes','no')").fetchall()
            bstats: dict = {}
            for r in brows:
                st = bstats.setdefault(r["asset"], {"n": 0, "agree": 0, "disagree_dists": []})
                st["n"] += 1
                if r["agree"]:
                    st["agree"] += 1
                elif r["signed_dist"] is not None:
                    st["disagree_dists"].append(r["signed_dist"])
            for asset, st in bstats.items():
                dd = st.pop("disagree_dists")
                st["agree_rate"] = _safe(st["agree"] / st["n"], 3) if st["n"] else None
                st["mean_disagree_dist"] = _safe(sum(dd) / len(dd), 5) if dd else None
            result["basis"] = bstats
        except sqlite3.OperationalError:
            pass

        try:
            mrows = conn.execute("SELECT * FROM maker_log").fetchall()
        except sqlite3.OperationalError:
            mrows = []
        conn.close()
        if mrows and _maker_stats:
            mg = {}
            for r in mrows:
                mg.setdefault(r["strategy"] or "?", []).append(r)
            for strat, rs in mg.items():
                st = _maker_stats(rs)
                if st:
                    result["maker"]["by_strategy"][strat] = _round_maker(st)
            ov = _maker_stats(list(mrows))
            result["maker"]["overall"] = _round_maker(ov) if ov else None
            if ov:
                if ov["n"] < 200:
                    result["maker"]["verdict"] = f"insufficient data ({ov['n']}/200)"
                elif ov["delta_se"] == ov["delta_se"] and ov["delta"] > 2 * ov["delta_se"] and ov["delta"] > 0:
                    result["maker"]["verdict"] = "maker clearly better - build 3B"
                elif ov["delta"] <= 0:
                    result["maker"]["verdict"] = "maker not better - stay taker"
                else:
                    result["maker"]["verdict"] = "inconclusive - collect more"
    except Exception as exc:
        log.warning("api_edge error: %s", exc)
    return jsonify(result)


@app.route("/healthz")
def healthz():
    """Railway health check: 200 if bot_state.json updated recently, else 503."""
    state_file = os.environ.get("BOT_STATE_FILE", "bot_state.json")
    try:
        age = time.time() - os.path.getmtime(state_file)
        if age < 120:
            return jsonify({"status": "ok"}), 200
        return jsonify({"status": "stale", "age": round(age, 1)}), 503
    except OSError:
        return jsonify({"status": "stale", "age": None}), 503
