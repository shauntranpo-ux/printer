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
import signal
import sqlite3
import subprocess
import tempfile
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
    "confidence_threshold": 72,
    "daily_loss_limit_dollars": 50,
    "daily_profit_target_dollars": 200,
    "quiet_hours_enabled": True,
    "quiet_start_et": 22,
    "quiet_end_et": 7,
    "enabled_assets": ["BTC", "ETH", "SOL", "XRP", "DOGE"],
}
if not os.path.exists("config.json"):
    try:
        with open("config.json", "w", encoding="utf-8") as _f:
            json.dump(_FULL_CONFIG_DEFAULT, _f, indent=2)
        log.info("Created default config.json")
    except Exception as _cfg_err:
        logging.warning(f"Could not create default config.json: {_cfg_err}")

# On startup: if mode was live, reset to paper (safety — never auto-start real trades on redeploy).
# bot_enabled is intentionally preserved so the bot resumes running in paper mode after redeploy.
try:
    if os.path.exists("config.json"):
        with open("config.json", "r", encoding="utf-8") as _f:
            _cfg = json.load(_f)
        if _cfg.get("mode", "paper") == "live":
            _cfg["mode"] = "paper"
            with open("config.json", "w", encoding="utf-8") as _f:
                json.dump(_cfg, _f, indent=2)
            log.info("Startup safety reset: live → paper mode")
except Exception as _rst_err:
    logging.warning(f"Could not apply startup safety reset: {_rst_err}")

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
    notify.send_alert("INFO", text)


_CONFIG_DEFAULT = {"mode": "paper", "trade_amount_dollars": 25, "confidence_threshold": 72,
                   "daily_loss_limit_dollars": 50,
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
        "confidence_threshold":        lambda v: isinstance(v, (int, float)) and 50 <= v <= 100,
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
        enabled = config.setdefault("enabled_assets", ["BTC", "ETH", "SOL", "XRP", "DOGE"])
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


@app.route("/api/test-telegram", methods=["POST"])
def api_test_telegram():
    """Send a test Telegram message to verify notification config."""
    now_str = datetime.now(timezone(timedelta(hours=-7))).strftime("%b %d %I:%M %p PST")
    try:
        notify.send_alert("INFO", f"\U0001f514 <b>Telegram test</b>  —  {now_str}\nBot notifications are working.")
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
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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
            expires = "—"

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
                time_str = t[11:16] if len(t) >= 16 else "—"
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
            points = [0.0] + points  # ensure length ≥ 2 so dashboard renders the curve

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

        loss_limit  = cfg.get("daily_loss_limit_dollars", 50.0)
        profit_target = cfg.get("daily_profit_target_dollars", 200.0)
        ev_floor    = cfg.get("min_ev_base", 7.0)
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
