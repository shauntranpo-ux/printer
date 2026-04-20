"""
server.py — Flask backend for the Kalshi BTC bot dashboard.

Reads from kalshi_bot.db and bot_state.json (written by bot.py).
Writes to config.json when the user changes settings.
Never writes to the database directly.

Start via runner.py, not directly.
"""

import csv
import glob
import io
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

try:
    from flask import Flask, jsonify, request
except Exception as _import_err:
    import traceback
    traceback.print_exc()
    raise

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("server")

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
    "bot_enabled": False,
    "mode": "paper",
    "trade_amount_dollars": 25,
    "confidence_threshold": 72,
    "daily_loss_limit_dollars": 50000,
    "daily_profit_target_dollars": 50000,
}
if not os.path.exists("config.json"):
    try:
        with open("config.json", "w", encoding="utf-8") as _f:
            json.dump(_FULL_CONFIG_DEFAULT, _f, indent=2)
        log.info("Created default config.json")
    except Exception as _cfg_err:
        logging.warning(f"Could not create default config.json: {_cfg_err}")

app = Flask(__name__)

# ── Bot subprocess tracking ──
_bot_process: subprocess.Popen | None = None


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════

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


def _telegram_notify(text: str) -> None:
    """Fire-and-forget Telegram notification from the Flask process."""
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID",   "")
    if not token or not chat_id:
        return
    try:
        payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception as exc:
        log.warning(f"Telegram notify error (server): {exc}")


_CONFIG_DEFAULT = {"mode": "paper", "trade_amount_dollars": 25, "confidence_threshold": 72,
                   "daily_loss_limit_dollars": 50000,
                   "daily_profit_target_dollars": 50000}
_STATE_DEFAULT  = {"btc_price": None, "today_live_pnl": 0.0, "today_paper_pnl": 0.0,
                   "phase": "waiting", "mode": "paper"}

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


# ══════════════════════════════════════════════════════════════════════════════
#  API endpoints
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/state")
def api_state():
    """Return the full current bot state from bot_state.json plus config."""
    state = read_state()
    state["config"] = read_config()
    return jsonify(state)


@app.route("/api/status")
def api_status():
    """
    Aggregated status across all enabled strategies defined in strategies.json.

    Returns:
      {
        "strategies": [
          {
            "name": "btc15m",
            "state_file": "bot_state_btc15m.json",
            "alive": true,          # false if state file is missing or stale (>60s)
            "state": { ... }        # full bot_state dict, or {} if unreadable
          },
          ...
        ],
        "summary": {
          "total": 2,
          "alive": 1,
          "today_live_pnl": 12.50,
          "today_paper_pnl": -3.20,
          "total_trades_today": 5
        }
      }
    """
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

    return jsonify({
        "strategies": results,
        "summary": {
            "total":             len(results),
            "alive":             alive_count,
            "today_live_pnl":    round(live_pnl_total,  2),
            "today_paper_pnl":   round(paper_pnl_total, 2),
        },
    })


@app.route("/api/trades")
def api_trades():
    """
    Return the last 100 trades ordered by ts descending.
    Optional query parameter: mode=live|paper.
    """
    mode = request.args.get("mode")
    try:
        conn = get_db()
        if mode:
            rows = conn.execute(
                "SELECT * FROM trades WHERE mode = ? ORDER BY ts DESC LIMIT 500",
                (mode,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY ts DESC LIMIT 500"
            ).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as exc:
        if "no such table" in str(exc).lower():
            return jsonify([])
        return jsonify({"error": str(exc)}), 500


@app.route("/api/market_log")
def api_market_log():
    """Return the last 100 market log entries ordered by ts descending."""
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM market_log ORDER BY ts DESC LIMIT 100"
        ).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as exc:
        if "no such table" in str(exc).lower():
            return jsonify([])
        return jsonify({"error": str(exc)}), 500


@app.route("/api/daily_summary")
def api_daily_summary():
    """Return the last 30 days of daily_summary rows for both modes."""
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM daily_summary ORDER BY date DESC LIMIT 30"
        ).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as exc:
        if "no such table" in str(exc).lower():
            return jsonify([])
        return jsonify({"error": str(exc)}), 500


@app.route("/api/stress_test")
def api_stress_test():
    """Return all stress_test_results rows ordered by run_ts descending."""
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM stress_test_results ORDER BY run_ts DESC"
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
        "daily_loss_limit_dollars":    is_positive_number,
        "daily_profit_target_dollars": is_positive_number,
        "confidence_threshold":        lambda v: isinstance(v, (int, float)) and 50 <= v <= 100,
        "bot_enabled":                 lambda v: isinstance(v, bool),
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
        enabled = config.setdefault("enabled_assets", ["ETH", "SOL", "XRP", "DOGE"])
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

    now_str = datetime.now(timezone(timedelta(hours=-7))).strftime("%b %d %I:%M %p PST")
    new_enabled = config.get("bot_enabled", False)
    new_mode    = config.get("mode", "paper")

    if "bot_enabled" in data and new_enabled != prev_enabled:
        icon = "▶️" if new_enabled else "⏹"
        _telegram_notify(f"{icon} <b>Bot {'ENABLED' if new_enabled else 'DISABLED'}</b>  —  {now_str}\nMode: {new_mode.upper()}")

    if "mode" in data and new_mode != prev_mode:
        icon = "💵" if new_mode == "live" else "📄"
        _telegram_notify(f"{icon} <b>Mode switched to {new_mode.upper()}</b>  —  {now_str}")

    return jsonify(config)


@app.route("/api/reset_pnl", methods=["POST"])
def api_reset_pnl():
    """
    Delete all trade and daily_summary records for a given mode.
    Body: {"mode": "live"} or {"mode": "paper"}
    """
    data = request.get_json(silent=True)
    if not data or data.get("mode") not in ("live", "paper", "all"):
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
        path = os.path.join(_BASE_DIR, "dashboard.html")
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
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY ts DESC LIMIT 2000"
        ).fetchall()
        conn.close()

        all_trades = [dict(r) for r in rows]

        def _pnl(trades):
            resolved = [t for t in trades if t.get("outcome") not in ("pending", None) and t.get("pnl_dollars") is not None]
            total = round(sum(t["pnl_dollars"] for t in resolved), 2)
            wins  = sum(1 for t in resolved if t.get("outcome") == "win")
            count = len(resolved)
            win_rate = round(wins / count * 100, 1) if count else 0.0
            return {"pnl": total, "trades": count, "wins": wins, "win_rate": win_rate}

        today_trades = [t for t in all_trades if (t.get("ts") or "").startswith(today)]

        # Per-asset today
        cfg = read_config()
        enabled_assets = cfg.get("enabled_assets", ["BTC", "ETH", "SOL", "XRP", "DOGE"])
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

        return jsonify({
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
        })
    except Exception as exc:
        if "no such table" in str(exc).lower():
            empty = {"pnl": 0.0, "trades": 0, "wins": 0, "win_rate": 0.0}
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            return jsonify({
                "today":   {"live": empty, "paper": empty, "demo": empty, "by_asset": {}, "date": today},
                "alltime": {"live": empty, "paper": empty, "demo": empty},
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
        enabled_assets = cfg.get("enabled_assets", ["BTC", "ETH", "SOL", "XRP", "DOGE"])

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
        enabled_assets = cfg.get("enabled_assets", ["BTC", "ETH", "SOL", "XRP", "DOGE"])
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
            result[asset] = {**a_state, "daily": daily.get(asset, {"wins": 0, "losses": 0, "pnl": 0.0})}

        return jsonify({"assets": result, "ts": state.get("ts"), "bot_enabled": cfg.get("bot_enabled", False)})
    except Exception as exc:
        log.error(f"api_market_state error: {exc}", exc_info=True)
        # Return OFFLINE placeholder for all known assets so the JS grid always renders
        _default_assets = {a: {"phase": "OFFLINE", "price": None, "daily": {"wins": 0, "losses": 0, "pnl": 0.0}}
                           for a in ["BTC", "ETH", "SOL", "XRP", "DOGE"]}
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
            pv_path = "price_validation_log.csv"
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


@app.route("/api/bot/start", methods=["POST"])
def api_bot_start():
    """
    Start bot.py as a subprocess.
    Returns the PID of the new process.
    Uses 'py -3' on Windows, 'python3' elsewhere.
    """
    global _bot_process
    try:
        # Clean up finished process if any
        if _bot_process is not None:
            ret = _bot_process.poll()
            if ret is None:
                return jsonify({"error": "Bot is already running", "pid": _bot_process.pid}), 409
            else:
                _bot_process = None

        bot_script = os.path.join(_BASE_DIR, "bot.py")
        if not os.path.exists(bot_script):
            return jsonify({"error": f"bot.py not found at {bot_script}"}), 404

        python_cmd = "py" if sys.platform == "win32" else "python3"
        args = [python_cmd, "-3", bot_script] if sys.platform == "win32" else [python_cmd, bot_script]

        _bot_process = subprocess.Popen(
            args,
            cwd=_BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )

        log.info(f"Bot started with PID {_bot_process.pid}")
        return jsonify({"ok": True, "pid": _bot_process.pid})
    except Exception as exc:
        log.error(f"api_bot_start error: {exc}", exc_info=True)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/bot/stop", methods=["POST"])
def api_bot_stop():
    """
    Stop the bot subprocess started by /api/bot/start.
    Sends SIGTERM (or CTRL_BREAK on Windows), then waits up to 5 seconds.
    """
    global _bot_process
    try:
        if _bot_process is None:
            return jsonify({"ok": True, "msg": "No bot process tracked (already stopped)"})

        pid = _bot_process.pid
        ret = _bot_process.poll()
        if ret is not None:
            _bot_process = None
            return jsonify({"ok": True, "msg": f"Bot process {pid} had already exited (code {ret})"})

        try:
            if sys.platform == "win32":
                _bot_process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                _bot_process.terminate()
            _bot_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning(f"Bot process {pid} did not stop in 5s — killing")
            _bot_process.kill()
            _bot_process.wait(timeout=3)
        except Exception as kill_exc:
            log.warning(f"Error stopping bot: {kill_exc}")

        _bot_process = None
        log.info(f"Bot process {pid} stopped")
        return jsonify({"ok": True, "msg": f"Bot process {pid} stopped"})
    except Exception as exc:
        log.error(f"api_bot_stop error: {exc}", exc_info=True)
        return jsonify({"error": str(exc)}), 500


@app.route("/health")
def health():
    return '{"status":"ok"}', 200, {"Content-Type": "application/json"}


# ══════════════════════════════════════════════════════════════════════════════
#  AI Analysis endpoints
# ══════════════════════════════════════════════════════════════════════════════

def _latest_analysis_file() -> str | None:
    files = sorted(glob.glob("daily_analysis/*.json"))
    return files[-1] if files else None


def _latest_weekly_file() -> str | None:
    files = sorted(glob.glob("weekly_reports/*.json"))
    return files[-1] if files else None


@app.route("/api/analysis")
def api_analysis():
    """Return the latest daily_analysis JSON, or {exists:false} if none."""
    try:
        path = _latest_analysis_file()
        if not path:
            return jsonify({"exists": False})
        data = json.loads(open(path, encoding="utf-8").read())
        data["exists"]   = True
        data["filename"] = os.path.basename(path)
        return jsonify(data)
    except Exception as exc:
        log.error(f"api_analysis error: {exc}", exc_info=True)
        return jsonify({"error": str(exc)}), 500



@app.route("/api/signal-stats")
def api_signal_stats():
    """
    Per-signal win rates from settled trades.
    Returns win/loss/wr for each signal bucket: mom_label, accel_label,
    lag_signal, vel_signal.
    """
    try:
        db_path = os.path.join(os.path.dirname(__file__), "trades.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT claude_signals, outcome FROM trades "
            "WHERE outcome IN ('win','loss') AND claude_signals IS NOT NULL "
            "ORDER BY ts DESC LIMIT 500"
        ).fetchall()
        conn.close()

        buckets: dict = {}
        for row in rows:
            try:
                sigs = json.loads(row["claude_signals"])
            except Exception:
                continue
            outcome = row["outcome"]
            for key in ("mom_label", "accel_label", "lag_signal", "vel_signal"):
                val = sigs.get(key, "unknown")
                bucket_key = f"{key}:{val}"
                if bucket_key not in buckets:
                    buckets[bucket_key] = {"signal": key, "value": val, "wins": 0, "losses": 0}
                if outcome == "win":
                    buckets[bucket_key]["wins"] += 1
                else:
                    buckets[bucket_key]["losses"] += 1

        result = []
        for b in buckets.values():
            total = b["wins"] + b["losses"]
            result.append({
                "signal": b["signal"],
                "value":  b["value"],
                "wins":   b["wins"],
                "losses": b["losses"],
                "total":  total,
                "wr":     round(b["wins"] / total, 3) if total > 0 else None,
            })
        result.sort(key=lambda x: (x["signal"], x["value"]))
        return jsonify({"stats": result, "total_trades": len(rows)})
    except Exception as exc:
        log.error(f"api_signal_stats error: {exc}", exc_info=True)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/weekly")
def api_weekly():
    """Return the latest weekly_reports JSON, or {exists:false} if none."""
    try:
        path = _latest_weekly_file()
        if not path:
            return jsonify({"exists": False})
        data = json.loads(open(path, encoding="utf-8").read())
        data["exists"]   = True
        data["filename"] = os.path.basename(path)
        return jsonify(data)
    except Exception as exc:
        log.error(f"api_weekly error: {exc}", exc_info=True)
        return jsonify({"error": str(exc)}), 500




# ══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    port = int(os.environ.get("PORT", 5000))
    log.info(f"Starting Flask on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)
