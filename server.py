"""
server.py — Flask backend for the Kalshi BTC bot dashboard.

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
import sqlite3
import time
from datetime import datetime, timezone

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

app = Flask(__name__)


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


_CONFIG_DEFAULT = {"mode": "paper", "trade_amount_dollars": 20, "stop_loss_percent": 35}
_STATE_DEFAULT  = {"btc_price": None, "today_live_pnl": 0.0, "today_paper_pnl": 0.0,
                   "phase": "waiting", "mode": "paper"}

# If BOT_DATA_DIR is set, all shared files live there (e.g. /app/data on Railway).
# Falls back to the app directory for local development.
_DATA_DIR = os.environ.get("BOT_DATA_DIR", "")


def _data_path(filename: str) -> str:
    return os.path.join(_DATA_DIR, filename) if _DATA_DIR else filename


def read_config() -> dict:
    return _safe_json_read(_data_path("config.json"), _CONFIG_DEFAULT.copy())


def write_config(data: dict) -> None:
    try:
        with open(_data_path("config.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except Exception as exc:
        log.error(f"Could not write config.json: {exc}")


def read_state() -> dict:
    return _safe_json_read(_data_path("bot_state.json"), _STATE_DEFAULT.copy())


def _load_strategies() -> list[dict]:
    return _safe_json_read("strategies.json", [])


def _read_strategy_state(state_file: str) -> dict | None:
    return _safe_json_read(_data_path(state_file), None)


def get_db() -> sqlite3.Connection:
    """Open a WAL-mode SQLite connection with Row factory."""
    db_path = os.environ.get("BOT_DB_FILE", _data_path("kalshi_bot.db"))
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
                "SELECT * FROM trades WHERE mode = ? ORDER BY ts DESC LIMIT 100",
                (mode,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY ts DESC LIMIT 100"
            ).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as exc:
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
    errors = []

    def is_positive_number(v):
        return isinstance(v, (int, float)) and v > 0

    validators = {
        "trade_amount_dollars":      is_positive_number,
        "mode":                      lambda v: v in ("live", "paper"),
        "daily_loss_limit_dollars":  is_positive_number,
        "daily_profit_target_dollars": is_positive_number,
        "confidence_threshold":      lambda v: isinstance(v, (int, float)) and 50 <= v <= 100,
        "stop_loss_percent":         lambda v: isinstance(v, (int, float)) and 10 <= v <= 50,
        "cooldown_markets":          lambda v: isinstance(v, (int, float)) and 0 <= v <= 10,
        "bot_enabled":               lambda v: isinstance(v, bool),
        "claude_enabled":            lambda v: isinstance(v, bool),
    }

    for key, value in data.items():
        if key not in validators:
            continue
        if validators[key](value):
            # Coerce cooldown_markets to int
            config[key] = int(value) if key == "cooldown_markets" else value
        else:
            errors.append(f"Invalid value for {key!r}: {value!r}")

    if errors:
        return jsonify({"error": errors}), 400

    write_config(config)
    log.info(f"Config updated: {data}")
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
        log.info(f"Serving dashboard from {path}")
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        log.info(f"dashboard.html read OK ({len(content)} bytes)")
        return content
    except Exception as exc:
        log.error(f"index() failed: {exc}", exc_info=True)
        return f"<h1>Error: {exc}</h1>", 500






@app.route("/health")
def health():
    return '{"status":"ok"}', 200, {"Content-Type": "application/json"}


# ══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    port = int(os.environ.get("PORT", 5000))
    log.info(f"Starting Flask on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)
