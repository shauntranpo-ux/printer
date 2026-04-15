"""
runner.py — Launches and monitors bot.py strategy instances.

Server is handled by Railway (Procfile: web: python server.py).
This runner only manages bot worker processes.

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
RESTART_BACKOFF      = 10   # seconds to wait before restarting a crashed process
POLL_INTERVAL        = 5    # seconds between health-check loops
MAX_CRASHES_PER_HOUR = 5    # halt permanently if a strategy crashes this many times in 1 hour


def _send_telegram_sync(text: str) -> None:
    """Fire-and-forget synchronous Telegram notification from the runner process."""
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


# Each entry: {"name": str, "strategy": dict, "proc": Popen, "last_crash": float}
_procs: list[dict] = []


def _load_strategies(path: str) -> list[dict]:
    """Read and validate strategies.json. Returns only enabled entries."""
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
    # Preserve Railway BOT_DB_FILE env var when db_file is relative
    # (Railway mounts the DB volume and sets BOT_DB_FILE to the absolute path)
    db_file = strategy["db_file"]
    if "BOT_DB_FILE" not in os.environ or os.path.isabs(db_file):
        env["BOT_DB_FILE"] = db_file
    env["BOT_STATE_FILE"]  = strategy["state_file"]
    return env


def _start_bot(strategy: dict) -> subprocess.Popen:
    print(f"[runner] Starting strategy '{strategy['name']}' ...")
    return subprocess.Popen(
        [sys.executable, os.path.join(BASE_DIR, "bot.py")],
        env=_build_env(strategy),
        cwd=BASE_DIR,
    )


def _shutdown(signum, frame):
    print("\n[runner] Shutting down ...")
    for entry in _procs:
        proc = entry["proc"]
        if proc.poll() is None:
            proc.terminate()
            print(f"[runner]   terminated '{entry['name']}'")
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategies", default=DEFAULT_STRATEGIES_FILE)
    args = parser.parse_args()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    os.chdir(BASE_DIR)

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
            "crash_times": [],   # timestamps of each crash in the last hour
            "halted":      False,
        })
        print(f"[runner]   '{s['name']}' PID={proc.pid}  state={s['state_file']}")

    print(f"[runner] {len(strategies)} strategy instance(s) running. Ctrl+C to stop.\n")

    while True:
        try:
            time.sleep(POLL_INTERVAL)
            now = time.time()

            for entry in _procs:
                # Skip permanently halted strategies — they need a manual restart
                if entry.get("halted"):
                    continue

                proc = entry["proc"]
                if proc.poll() is None:
                    continue

                code        = proc.returncode
                since_crash = now - entry["last_crash"]

                # Exit code 2 = pre-flight check failed (intentional clean stop).
                # Do NOT restart and do NOT count as a crash — needs human action.
                if code == 2:
                    msg = (f"PRE-FLIGHT FAILED: '{entry['name']}' refused to start (code=2). "
                           f"Resolve pre-flight issues (price validation, fee config, "
                           f"daily limits) then restart manually.")
                    print(f"[runner] {msg}")
                    _send_telegram_sync(
                        f"\U0001f6a8 <b>PRE-FLIGHT FAILED — {entry['name']}</b>\n"
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
                    # Record crash timestamp for circuit breaker
                    entry["crash_times"].append(now)
                elif since_crash >= RESTART_BACKOFF:
                    # Circuit breaker: count crashes in the last hour
                    cutoff = now - 3600
                    recent = [t for t in entry["crash_times"] if t > cutoff]
                    entry["crash_times"] = recent  # prune old entries in place

                    if len(recent) >= MAX_CRASHES_PER_HOUR:
                        msg = (
                            f"CRITICAL: '{entry['name']}' crashed {len(recent)}x "
                            f"in the last hour — halting restarts to prevent account damage."
                        )
                        print(f"[runner] {msg}")
                        _send_telegram_sync(
                            f"🚨 <b>CRASH LOOP HALTED — {entry['name']}</b>\n"
                            f"Crashed {len(recent)}× in the last hour.\n"
                            f"Manual restart required: ssh → python runner.py"
                        )
                        entry["halted"] = True
                    else:
                        new_proc = _start_bot(entry["strategy"])
                        entry["proc"]       = new_proc
                        entry["last_crash"] = 0.0
                        print(f"[runner] '{entry['name']}' restarted "
                              f"(PID={new_proc.pid}, crashes_1h={len(recent)}).")

            running = [e["name"] for e in _procs if e["proc"].poll() is None and not e.get("halted")]
            halted  = [e["name"] for e in _procs if e.get("halted")]
            status  = f"[runner] OK | running: {running}"
            if halted:
                status += f" | HALTED (crash loop): {halted}"
            print(status)
        except Exception as exc:
            print(f"[runner] Loop error (continuing): {exc}")


if __name__ == "__main__":
    main()
