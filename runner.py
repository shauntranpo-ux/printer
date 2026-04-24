"""
runner.py — Launches and monitors bot.py strategy instances.

Server is handled by Railway (Procfile: web: python server.py).
This runner manages bot worker processes and all background sidecars.

Sidecars
--------
  paper mode  : price_validator.py     — continuous AMM price accuracy monitor
  live mode   : validate_and_report.py — blocking GO/NO-GO gate before bots start
  always      : collect_kalshi_ladder_history.py — refreshed every 24 h
  always      : weekly_report.py       — refreshed every 7 days

Usage:
    python runner.py
    python runner.py --strategies strategies.json
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STRATEGIES_FILE = os.path.join(BASE_DIR, "strategies.json")
RESTART_BACKOFF      = 10        # seconds before restarting a crashed bot
POLL_INTERVAL        = 5         # seconds between health-check loops
MAX_CRASHES_PER_HOUR = 5         # halt permanently after this many crashes/hour
LADDER_INTERVAL      = 24 * 3600 # re-run ladder collector every 24 h
WEEKLY_INTERVAL      = 7 * 24 * 3600  # re-run weekly report every 7 days


def _send_telegram_sync(text: str) -> None:
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
        print(f"[runner] Telegram error: {exc}")


# ── Process registry ──────────────────────────────────────────────────────────
_procs: list[dict] = []                          # bot strategy processes
_validator_proc: subprocess.Popen | None = None  # price_validator (paper mode)
_ladder_proc:    subprocess.Popen | None = None  # ladder history collector
_weekly_proc:    subprocess.Popen | None = None  # weekly report generator
_last_ladder_run: float = 0.0
_last_weekly_run: float = 0.0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_strategies(path: str) -> list[dict]:
    try:
        with open(path) as fh:
            strategies = json.load(fh)
    except FileNotFoundError:
        print(f"[runner] strategies.json not found at {path}.")
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print(f"[runner] strategies.json parse error: {exc}")
        sys.exit(1)

    required = {"name", "config_file", "db_file", "state_file", "enabled"}
    valid = []
    for i, s in enumerate(strategies):
        missing = required - s.keys()
        if missing:
            print(f"[runner] Strategy #{i} missing fields: {missing} — skipping")
            continue
        if s["enabled"]:
            valid.append(s)
        else:
            print(f"[runner] Strategy '{s['name']}' disabled — skipping")
    return valid


def _build_env(strategy: dict) -> dict:
    env = os.environ.copy()
    env["BOT_CONFIG_FILE"] = strategy["config_file"]
    db_file = strategy["db_file"]
    if "BOT_DB_FILE" not in os.environ or os.path.isabs(db_file):
        env["BOT_DB_FILE"] = db_file
    env["BOT_STATE_FILE"] = strategy["state_file"]
    return env


def _start_bot(strategy: dict) -> subprocess.Popen:
    print(f"[runner] Starting strategy '{strategy['name']}' ...")
    return subprocess.Popen(
        [sys.executable, os.path.join(BASE_DIR, "bot.py")],
        env=_build_env(strategy),
        cwd=BASE_DIR,
    )


def _start_validator() -> subprocess.Popen:
    print("[runner] Starting price_validator.py (paper mode sidecar) ...")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(BASE_DIR, "price_validator.py")],
        cwd=BASE_DIR,
    )
    print(f"[runner]   price_validator PID={proc.pid}")
    return proc


def _run_preflight_validation() -> bool:
    """
    Run validate_and_report.py synchronously before starting bots in live mode.
    Returns True if GO or MARGINAL (safe to proceed), False if NO-GO (halt).
    """
    print("[runner] Live mode — running validate_and_report.py pre-flight check ...")
    result = subprocess.run(
        [sys.executable, os.path.join(BASE_DIR, "validate_and_report.py")],
        cwd=BASE_DIR,
    )
    if result.returncode != 0:
        print("[runner] validate_and_report.py returned NO-GO (exit 1). Halting.")
        _send_telegram_sync(
            "<b>LIVE PRE-FLIGHT FAILED — NO-GO</b>\n"
            "validate_and_report.py rejected the price model.\n"
            "Run price_validator.py in paper mode first to collect 200+ samples,\n"
            "then retry: <code>python runner.py</code>"
        )
        return False
    print("[runner] Pre-flight validation passed (GO / MARGINAL) — starting bots.")
    return True


def _start_ladder_collector() -> subprocess.Popen:
    global _last_ladder_run
    print("[runner] Starting collect_kalshi_ladder_history.py ...")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(BASE_DIR, "collect_kalshi_ladder_history.py")],
        cwd=BASE_DIR,
    )
    _last_ladder_run = time.time()
    print(f"[runner]   ladder collector PID={proc.pid}")
    return proc


def _start_weekly_report() -> subprocess.Popen:
    global _last_weekly_run
    print("[runner] Starting weekly_report.py ...")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(BASE_DIR, "weekly_report.py")],
        cwd=BASE_DIR,
    )
    _last_weekly_run = time.time()
    print(f"[runner]   weekly report PID={proc.pid}")
    return proc


def _shutdown(signum, frame):
    print("\n[runner] Shutting down ...")
    for entry in _procs:
        proc = entry["proc"]
        if proc.poll() is None:
            proc.terminate()
            print(f"[runner]   terminated '{entry['name']}'")
    for name, proc in [
        ("price_validator",  _validator_proc),
        ("ladder_collector", _ladder_proc),
        ("weekly_report",    _weekly_proc),
    ]:
        if proc and proc.poll() is None:
            proc.terminate()
            print(f"[runner]   terminated {name}")
    sys.exit(0)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global _validator_proc, _ladder_proc, _weekly_proc

    parser = argparse.ArgumentParser()
    parser.add_argument("--strategies", default=DEFAULT_STRATEGIES_FILE)
    args = parser.parse_args()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    os.chdir(BASE_DIR)

    # Read mode from config.json
    try:
        with open(os.path.join(BASE_DIR, "config.json")) as fh:
            cfg = json.load(fh)
        mode = cfg.get("mode", "paper")
    except Exception as exc:
        print(f"[runner] Could not read config.json: {exc} — defaulting to paper mode")
        mode = "paper"

    print(f"[runner] Mode: {mode.upper()}")

    # Live mode: blocking pre-flight validation before starting any bots
    if mode == "live":
        ok = _run_preflight_validation()
        if not ok:
            sys.exit(1)

    # Load and start bot strategy processes
    strategies = _load_strategies(args.strategies)
    if not strategies:
        print("[runner] No enabled strategies found. Check strategies.json.")
        sys.exit(1)

    for s in strategies:
        proc = _start_bot(s)
        _procs.append({
            "name":        s["name"],
            "strategy":    s,
            "proc":        proc,
            "last_crash":  0.0,
            "crash_times": [],
            "halted":      False,
        })
        print(f"[runner]   '{s['name']}' PID={proc.pid}  state={s['state_file']}")

    print(f"[runner] {len(strategies)} strategy instance(s) running. Ctrl+C to stop.\n")

    # Paper mode sidecar: continuous price validation
    if mode == "paper":
        _validator_proc = _start_validator()
    else:
        print("[runner] Live mode — price_validator not started.")

    # Always: ladder history collector (runs once now, then every 24 h)
    _ladder_proc = _start_ladder_collector()

    # Always: weekly report (runs once now, then every 7 days)
    _weekly_proc = _start_weekly_report()

    # ── Main monitoring loop ──────────────────────────────────────────────────
    while True:
        try:
            time.sleep(POLL_INTERVAL)
            now = time.time()

            # ── Bot strategy health checks ────────────────────────────────────
            for entry in _procs:
                if entry.get("halted"):
                    continue

                proc = entry["proc"]
                if proc.poll() is None:
                    continue

                code        = proc.returncode
                since_crash = now - entry["last_crash"]

                if code == 2:
                    msg = (f"PRE-FLIGHT FAILED: '{entry['name']}' refused to start (code=2). "
                           f"Resolve pre-flight issues then restart manually.")
                    print(f"[runner] {msg}")
                    _send_telegram_sync(
                        f"<b>PRE-FLIGHT FAILED — {entry['name']}</b>\n"
                        f"Bot refused to start due to unresolved pre-flight checks.\n"
                        f"Resolve issues in config.json / price_validation_log.csv, "
                        f"then restart manually: <code>python runner.py</code>"
                    )
                    entry["halted"] = True
                    continue

                if entry["last_crash"] == 0.0:
                    print(f"[runner] '{entry['name']}' exited (code={code}). "
                          f"Waiting {RESTART_BACKOFF}s before restart ...")
                    entry["last_crash"] = now
                    entry["crash_times"].append(now)
                elif since_crash >= RESTART_BACKOFF:
                    cutoff = now - 3600
                    recent = [t for t in entry["crash_times"] if t > cutoff]
                    entry["crash_times"] = recent

                    if len(recent) >= MAX_CRASHES_PER_HOUR:
                        msg = (
                            f"CRITICAL: '{entry['name']}' crashed {len(recent)}x "
                            f"in the last hour — halting restarts."
                        )
                        print(f"[runner] {msg}")
                        _send_telegram_sync(
                            f"<b>CRASH LOOP HALTED — {entry['name']}</b>\n"
                            f"Crashed {len(recent)}x in the last hour.\n"
                            f"Manual restart required: python runner.py"
                        )
                        entry["halted"] = True
                    else:
                        new_proc = _start_bot(entry["strategy"])
                        entry["proc"]       = new_proc
                        entry["last_crash"] = 0.0
                        print(f"[runner] '{entry['name']}' restarted "
                              f"(PID={new_proc.pid}, crashes_1h={len(recent)}).")

            # ── price_validator: restart if died ─────────────────────────────
            if _validator_proc and _validator_proc.poll() is not None:
                print(f"[runner] price_validator exited (code={_validator_proc.returncode}) — restarting ...")
                _validator_proc = _start_validator()

            # ── ladder collector: restart when done + 24 h elapsed ───────────
            if _ladder_proc and _ladder_proc.poll() is not None:
                if now - _last_ladder_run >= LADDER_INTERVAL:
                    print("[runner] 24 h elapsed — refreshing ladder history ...")
                    _ladder_proc = _start_ladder_collector()

            # ── weekly report: restart when done + 7 days elapsed ────────────
            if _weekly_proc and _weekly_proc.poll() is not None:
                if now - _last_weekly_run >= WEEKLY_INTERVAL:
                    print("[runner] 7 days elapsed — generating weekly report ...")
                    _weekly_proc = _start_weekly_report()

            # ── Status line ───────────────────────────────────────────────────
            running = [e["name"] for e in _procs if e["proc"].poll() is None and not e.get("halted")]
            halted  = [e["name"] for e in _procs if e.get("halted")]
            sidecars = []
            if _validator_proc:
                sidecars.append(f"price_validator:{'on' if _validator_proc.poll() is None else 'restarting'}")
            if _ladder_proc:
                sidecars.append(f"ladder:{'running' if _ladder_proc.poll() is None else 'idle'}")
            if _weekly_proc:
                sidecars.append(f"weekly:{'running' if _weekly_proc.poll() is None else 'idle'}")
            status = f"[runner] OK | bots: {running}"
            if sidecars:
                status += f" | sidecars: {sidecars}"
            if halted:
                status += f" | HALTED: {halted}"
            print(status)

        except Exception as exc:
            print(f"[runner] Loop error (continuing): {exc}")


if __name__ == "__main__":
    main()
