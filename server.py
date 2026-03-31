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
import math
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, jsonify, request

log = logging.getLogger("server")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

app = Flask(__name__)

# Stress-test thread management
_stress_thread: threading.Thread | None = None
_stress_stop: threading.Event = threading.Event()


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════

def read_config() -> dict:
    """Read config.json."""
    try:
        with open("config.json", "r") as fh:
            return json.load(fh)
    except Exception:
        return {"mode": "paper", "trade_amount_dollars": 20, "stop_loss_percent": 35}


def write_config(data: dict) -> None:
    """Write config.json."""
    with open("config.json", "w") as fh:
        json.dump(data, fh, indent=2)


def read_state() -> dict:
    """Read bot_state.json (legacy single-strategy path)."""
    try:
        with open("bot_state.json", "r") as fh:
            return json.load(fh)
    except Exception:
        return {"btc_price": None, "today_live_pnl": 0.0, "today_paper_pnl": 0.0,
                "phase": "waiting", "mode": "paper"}


def _load_strategies() -> list[dict]:
    """Read strategies.json; return empty list if missing."""
    try:
        with open("strategies.json") as fh:
            return json.load(fh)
    except Exception:
        return []


def _read_strategy_state(state_file: str) -> dict | None:
    """Read a strategy's state file; return None on failure."""
    try:
        with open(state_file) as fh:
            return json.load(fh)
    except Exception:
        return None


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


@app.route("/api/stress_test/trades")
def api_stress_test_trades():
    """Return the equity curve and trade list from the most recent stress test run."""
    try:
        with open("stress_test_trades.json", "r") as fh:
            return jsonify(json.load(fh))
    except Exception:
        return jsonify({"trades": [], "equity_curve": []})


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
        "claude_enabled":            lambda v: isinstance(v, bool),
        "stress_test_active":        lambda v: isinstance(v, bool),
        "stress_test_start_date":    lambda v: isinstance(v, str) and len(v) == 10,
        "stress_test_end_date":      lambda v: isinstance(v, str) and len(v) == 10,
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

    # Stress test toggle
    if "stress_test_active" in data:
        if data["stress_test_active"]:
            _start_stress_test(config)
        else:
            _stop_stress_test()

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
        with open("dashboard.html", "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return "<h1>dashboard.html not found.</h1>", 500


# ══════════════════════════════════════════════════════════════════════════════
#  Stress test
# ══════════════════════════════════════════════════════════════════════════════

def _start_stress_test(config: dict) -> None:
    """Spawn the stress test background thread if not already running."""
    global _stress_thread
    if _stress_thread and _stress_thread.is_alive():
        log.info("Stress test already running.")
        return
    _stress_stop.clear()
    _stress_thread = threading.Thread(
        target=_run_stress_test, args=(config.copy(),), daemon=True
    )
    _stress_thread.start()
    log.info("Stress test thread started.")


def _stop_stress_test() -> None:
    """Signal the stress test thread to stop."""
    _stress_stop.set()
    log.info("Stress test stop requested.")


def _normal_cdf(x: float) -> float:
    """Standard normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def _run_stress_test(config: dict) -> None:
    """
    Fetch 1-minute OHLCV candles from Binance for the configured date range,
    simulate the bot's trading logic on 15-minute windows, and store
    aggregated results in stress_test_results plus detailed trades in
    stress_test_trades.json.
    """
    start_date = config.get("stress_test_start_date", "2024-10-01")
    end_date = config.get("stress_test_end_date", "2024-12-31")
    confidence_threshold = config.get("confidence_threshold", 70)
    stop_loss_pct = config.get("stop_loss_percent", 35)
    trade_amount = config.get("trade_amount_dollars", 20)

    log.info(f"Stress test: {start_date} → {end_date}")

    # Parse date range
    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = (
            datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            + timedelta(days=1)
        )
    except ValueError as exc:
        log.error(f"Date parse error: {exc}")
        return

    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    # ── Fetch candles from Binance ────────────────────────────────────────
    candles = []
    chunk_start = start_ms

    while chunk_start < end_ms and not _stress_stop.is_set():
        try:
            resp = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={
                    "symbol": "BTCUSDT",
                    "interval": "1m",
                    "startTime": chunk_start,
                    "endTime": min(chunk_start + 1_000 * 60 * 1_000, end_ms),
                    "limit": 1000,
                },
                timeout=10,
            )
            batch = resp.json()
            if not batch:
                break
            candles.extend(batch)
            chunk_start = batch[-1][0] + 60_000
            time.sleep(0.2)
        except Exception as exc:
            log.error(f"Binance fetch error: {exc}")
            time.sleep(1)

    if not candles or _stress_stop.is_set():
        log.info("Stress test aborted or no candle data.")
        return

    log.info(f"Fetched {len(candles)} 1-min candles.")

    # Convert raw candle arrays to dicts
    parsed = [
        {
            "ts_ms": c[0],
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
        }
        for c in candles
    ]

    # ── Simulate ──────────────────────────────────────────────────────────
    sim_trades = []
    total_markets = 0
    equity = 0.0
    equity_curve = [0.0]
    daily_pnl: dict[str, float] = {}

    i = 0
    while i + 15 <= len(parsed):
        if _stress_stop.is_set():
            break

        window = parsed[i : i + 15]
        total_markets += 1

        open_price = window[0]["close"]
        final_price = window[-1]["close"]
        strike = round(open_price / 500) * 500

        # Annualised vol from last 60 candles
        if i >= 60:
            hist = parsed[i - 60 : i]
            returns = [
                hist[j]["close"] / hist[j - 1]["close"] - 1 for j in range(1, len(hist))
            ]
            var = sum(r ** 2 for r in returns) / len(returns)
            vol_annual = math.sqrt(var) * math.sqrt(525_600)
        else:
            vol_annual = 0.80

        # Bot checks in at minute 5 (5 min elapsed, 10 min remaining)
        check = window[5]
        btc_at_check = check["close"]
        seconds_left = 10 * 60
        elapsed = 5 * 60

        # Log-normal probability
        T = seconds_left / (365 * 24 * 3600)
        if T > 0 and vol_annual > 0:
            d = math.log(btc_at_check / strike) / (vol_annual * math.sqrt(T))
            prob = _normal_cdf(d)
        else:
            prob = 0.5

        mid_prob = prob
        spread = max(2, int((1 - abs(mid_prob - 0.5) * 2) * 6))

        pct_diff = (btc_at_check - strike) / strike
        if abs(pct_diff) <= 0.010:
            i += 15
            continue  # at_strike — skip

        if pct_diff > 0:
            side = "yes"
            contract_price = mid_prob * 100 + spread / 2 + 1.0  # ask + slippage
        else:
            side = "no"
            contract_price = (1 - mid_prob) * 100 + spread / 2 + 1.0

        contract_price = max(0.0, min(100.0, contract_price))

        if contract_price < 60 or contract_price > 77:
            i += 15
            continue

        # Four-component confidence score (simplified for backtest)
        abs_pct = abs(pct_diff)
        dist_pts = 30 if abs_pct > 0.02 else (20 if abs_pct > 0.01 else 10)
        price_pts = (
            25 if 70 <= contract_price <= 77
            else (15 if 65 <= contract_price <= 69 else 5)
        )
        mom_pts = 10    # neutral (no real momentum history)
        time_pts = 20   # > 5 min elapsed
        score = dist_pts + price_pts + mom_pts + time_pts

        if score < confidence_threshold:
            i += 15
            continue

        entry_price = int(contract_price)
        contracts = int(trade_amount * 100 / entry_price)
        if contracts == 0:
            i += 15
            continue

        sl_price = int(entry_price * (1 - stop_loss_pct / 100))

        # Simulate remaining minutes (6–14) for stop loss
        exit_price = None
        exit_reason = "expiry"
        sl_below_count = 0

        for j in range(6, 15):
            if _stress_stop.is_set():
                break
            c_j = window[j]
            rem = (15 - j) * 60
            T_j = rem / (365 * 24 * 3600)
            if T_j > 0 and vol_annual > 0:
                d_j = math.log(c_j["close"] / strike) / (vol_annual * math.sqrt(T_j))
                prob_j = _normal_cdf(d_j)
            else:
                prob_j = 0.5

            bid = (
                prob_j * 100 - spread / 2 if side == "yes"
                else (1 - prob_j) * 100 - spread / 2
            )
            bid = max(0.0, min(100.0, bid))

            if bid <= sl_price:
                sl_below_count += 1
                if sl_below_count >= 2:
                    exit_price = int(bid)
                    exit_reason = "stop_loss"
                    break
            else:
                sl_below_count = 0

        if exit_price is None:
            # Expiry outcome
            if side == "yes":
                outcome = "win" if final_price > strike else "loss"
            else:
                outcome = "win" if final_price < strike else "loss"
            exit_price = 100 if outcome == "win" else 0
        else:
            outcome = "loss"

        pnl = (exit_price - entry_price) * contracts / 100
        profit_pct = (
            (exit_price - entry_price) / entry_price * 100 if entry_price else 0
        )

        equity += pnl
        equity_curve.append(round(equity, 2))

        day_str = datetime.fromtimestamp(
            window[0]["ts_ms"] / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d")
        daily_pnl[day_str] = daily_pnl.get(day_str, 0.0) + pnl

        sim_trades.append({
            "ts": datetime.fromtimestamp(
                window[0]["ts_ms"] / 1000, tz=timezone.utc
            ).isoformat(),
            "side": side,
            "contracts": contracts,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "outcome": outcome,
            "pnl": round(pnl, 2),
            "profit_pct": round(profit_pct, 2),
            "confidence": score,
            "strike": strike,
            "btc_price": btc_at_check,
        })

        i += 15

    if not sim_trades:
        log.info("Stress test: no trades taken — try widening the date range.")
        return

    # ── Aggregate metrics ─────────────────────────────────────────────────
    wins = sum(1 for t in sim_trades if t["outcome"] == "win")
    win_rate = wins / len(sim_trades) * 100
    total_pnl = sum(t["pnl"] for t in sim_trades)
    avg_conf = sum(t["confidence"] for t in sim_trades) / len(sim_trades)
    winning = [t for t in sim_trades if t["outcome"] == "win"]
    avg_profit_pct = (
        sum(t["profit_pct"] for t in winning) / len(winning) if winning else 0.0
    )

    # Max drawdown
    peak = 0.0
    running = 0.0
    max_dd = 0.0
    for t in sim_trades:
        running += t["pnl"]
        if running > peak:
            peak = running
        dd = (peak - running) / abs(peak) * 100 if peak != 0 else 0
        if dd > max_dd:
            max_dd = dd

    # Sharpe ratio (annualised, daily P&L)
    daily_vals = list(daily_pnl.values())
    if len(daily_vals) > 1:
        mean_d = sum(daily_vals) / len(daily_vals)
        std_d = math.sqrt(
            sum((x - mean_d) ** 2 for x in daily_vals) / (len(daily_vals) - 1)
        )
        sharpe = (mean_d / std_d * math.sqrt(252)) if std_d > 0 else 0.0
    else:
        sharpe = 0.0

    # Max consecutive losses
    max_consec = consec = 0
    for t in sim_trades:
        if t["outcome"] == "loss":
            consec += 1
            max_consec = max(max_consec, consec)
        else:
            consec = 0

    # ── Persist results ───────────────────────────────────────────────────
    try:
        conn = sqlite3.connect(os.environ.get("BOT_DB_FILE", "kalshi_bot.db"))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            INSERT INTO stress_test_results (
                run_ts, start_date, end_date, total_markets, total_trades,
                win_rate, total_pnl_dollars, max_drawdown_percent,
                avg_confidence, avg_profit_percent, sharpe_ratio,
                max_consecutive_losses
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            start_date, end_date,
            total_markets, len(sim_trades),
            round(win_rate, 2), round(total_pnl, 2),
            round(max_dd, 2), round(avg_conf, 1),
            round(avg_profit_pct, 2), round(sharpe, 3),
            max_consec,
        ))
        conn.commit()
        conn.close()
        log.info(
            f"Stress test complete: {len(sim_trades)} trades | "
            f"WR={win_rate:.1f}% | P&L=${total_pnl:.2f} | Sharpe={sharpe:.2f}"
        )
    except Exception as exc:
        log.error(f"Stress test DB write error: {exc}")

    try:
        with open("stress_test_trades.json", "w") as fh:
            json.dump({"trades": sim_trades, "equity_curve": equity_curve}, fh)
    except Exception as exc:
        log.error(f"Stress test trades file error: {exc}")

    # Auto-disable the flag in config
    try:
        cfg = read_config()
        cfg["stress_test_active"] = False
        write_config(cfg)
    except Exception:
        pass


@app.route("/health")
def health():
    state = read_state()
    return jsonify({
        "status": "ok",
        "btc_price": state.get("btc_price"),
        "today_live_pnl": state.get("today_live_pnl", 0.0),
        "today_paper_pnl": state.get("today_paper_pnl", 0.0),
        "phase": state.get("phase", "unknown"),
        "mode": state.get("mode", "unknown"),
    })


# ══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    # Auto-start stress test if it was active when last stopped
    try:
        cfg = read_config()
        if cfg.get("stress_test_active"):
            _start_stress_test(cfg)
    except Exception as exc:
        log.warning(f"Could not auto-start stress test on startup: {exc}")

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
