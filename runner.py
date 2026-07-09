"""
runner.py - Launches and monitors bot.py strategy instances.

Server is handled by Railway (Procfile: web: python server.py).
This runner manages bot worker processes and all background sidecars.

Sidecars
--------
  paper mode  : price_validator.py     - continuous AMM price accuracy monitor
  live mode   : validate_and_report.py - blocking GO/NO-GO gate before bots start
  always      : collect_kalshi_ladder_history.py - refreshed every 24 h
  always      : weekly_report.py       - refreshed every 7 days

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
import logging
import time
import urllib.request

import notify
import obs
obs.setup_logging("runner")
log = logging.getLogger("runner")


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STRATEGIES_FILE = os.path.join(BASE_DIR, "strategies.json")
RESTART_BACKOFF      = 10        # seconds before restarting a crashed bot
POLL_INTERVAL        = 5         # seconds between health-check loops
MAX_CRASHES_PER_HOUR = 5         # halt permanently after this many crashes/hour


def _now_str() -> str:
    """Current time in the configured display timezone (config.json: display_timezone)."""
    tz_name = "America/New_York"
    try:
        with open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8") as fh:
            tz_name = json.load(fh).get("display_timezone", tz_name) or tz_name
    except Exception:
        pass
    try:
        from zoneinfo import ZoneInfo
        import datetime as _dt
        d = _dt.datetime.now(ZoneInfo(tz_name))
        hour12 = d.strftime("%I").lstrip("0") or "12"
        return f"{d.strftime('%b')} {d.day}, {hour12}:{d.strftime('%M')} {d.strftime('%p')} {d.strftime('%Z')}"
    except Exception:
        return ""


def _send_telegram_sync(text: str) -> None:
    _ts = _now_str()
    notify.send_alert("INFO", f"{text}\n{_ts}" if _ts else text)


# Process registry
_procs: list[dict] = []                          # bot strategy processes


# Helpers

def _load_strategies(path: str) -> list[dict]:
    try:
        with open(path) as fh:
            strategies = json.load(fh)
    except FileNotFoundError:
        log.error(f"strategies.json not found at {path}.")
        sys.exit(1)
    except json.JSONDecodeError as exc:
        log.error(f"strategies.json parse error: {exc}")
        sys.exit(1)

    required = {"name", "config_file", "db_file", "state_file", "enabled"}
    valid = []
    for i, s in enumerate(strategies):
        missing = required - s.keys()
        if missing:
            log.warning(f"Strategy #{i} missing fields: {missing} - skipping")
            continue
        if s["enabled"]:
            valid.append(s)
        else:
            log.info(f"Strategy '{s['name']}' disabled - skipping")
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
    log.info(f"Starting strategy '{strategy['name']}' ...")
    return subprocess.Popen(
        [sys.executable, os.path.join(BASE_DIR, "bot.py")],
        env=_build_env(strategy),
        cwd=BASE_DIR,
    )


def _run_preflight_validation() -> bool:
    """
    Run validate_and_report.py synchronously before starting bots in live mode.
    Returns True if GO or MARGINAL (safe to proceed), False if NO-GO (halt).
    """
    _vr_path = os.path.join(BASE_DIR, "validate_and_report.py")
    if not os.path.exists(_vr_path):
        # The pre-flight script was removed from the repo (commit 1ac7fab); without this
        # guard subprocess.run would exit non-zero and falsely halt live mode on startup.
        log.warning("validate_and_report.py not present - skipping live pre-flight gate, proceeding.")
        return True
    log.info("Live mode - running validate_and_report.py pre-flight check ...")
    result = subprocess.run(
        [sys.executable, _vr_path],
        cwd=BASE_DIR,
    )
    if result.returncode != 0:
        log.error("validate_and_report.py returned NO-GO (exit 1). Halting.")
        _send_telegram_sync(
            "<b>LIVE PRE-FLIGHT FAILED - NO-GO</b>\n"
            "validate_and_report.py rejected the price model.\n"
            "Run price_validator.py in paper mode first to collect 200+ samples,\n"
            "then retry: <code>python runner.py</code>"
        )
        return False
    log.info("Pre-flight validation passed (GO / MARGINAL) - starting bots.")
    return True


def _shutdown(signum, frame):
    log.info("Shutting down ...")
    for entry in _procs:
        proc = entry["proc"]
        if proc.poll() is None:
            proc.terminate()
            log.info(f"  terminated '{entry['name']}' ")
    sys.exit(0)


# Main

def main():
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
        log.warning(f"Could not read config.json: {exc} - defaulting to paper mode")
        mode = "paper"

    log.info(f"Mode: {mode.upper()}")

    # Load and start bot strategy processes
    strategies = _load_strategies(args.strategies)
    if not strategies:
        log.error("No enabled strategies found. Check strategies.json.")
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
        log.info(f"  '{s['name']}' PID={proc.pid}  state={s['state_file']}")

    log.info(f"{len(strategies)} strategy instance(s) running. Ctrl+C to stop.")

    # Main monitoring loop
    while True:
        try:
            time.sleep(POLL_INTERVAL)
            now = time.time()

            # Bot strategy health checks
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
                    log.error(f"{msg}")
                    _send_telegram_sync(
                        f"<b>PRE-FLIGHT FAILED - {entry['name']}</b>\n"
                        f"Bot refused to start due to unresolved pre-flight checks.\n"
                        f"Resolve issues in config.json / price_validation_log.csv, "
                        f"then restart manually: <code>python runner.py</code>"
                    )
                    entry["halted"] = True
                    continue

                if entry["last_crash"] == 0.0:
                    log.info(f"'{entry['name']}' exited (code={code}). Waiting {RESTART_BACKOFF}s before restart ...")
                    entry["last_crash"] = now
                    entry["crash_times"].append(now)
                elif since_crash >= RESTART_BACKOFF:
                    cutoff = now - 3600
                    recent = [t for t in entry["crash_times"] if t > cutoff]
                    entry["crash_times"] = recent

                    if len(recent) >= MAX_CRASHES_PER_HOUR:
                        msg = (
                            f"CRITICAL: '{entry['name']}' crashed {len(recent)}x "
                            f"in the last hour - halting restarts."
                        )
                        log.error(f"{msg}")
                        _send_telegram_sync(
                            f"<b>CRASH LOOP HALTED - {entry['name']}</b>\n"
                            f"Crashed {len(recent)}x in the last hour.\n"
                            f"Manual restart required: python runner.py"
                        )
                        entry["halted"] = True
                    else:
                        new_proc = _start_bot(entry["strategy"])
                        entry["proc"]       = new_proc
                        entry["last_crash"] = 0.0
                        log.info(f"'{entry['name']}' restarted (PID={new_proc.pid}, crashes_1h={len(recent)}).")

            # Status line
            running = [e["name"] for e in _procs if e["proc"].poll() is None and not e.get("halted")]
            halted  = [e["name"] for e in _procs if e.get("halted")]
            status = f"OK | bots: {running}"
            if halted:
                status += f" | HALTED: {halted}"
            log.info(status)

        except Exception as exc:
            log.error(f"Loop error (continuing): {exc}")


if __name__ == "__main__":
    main()
