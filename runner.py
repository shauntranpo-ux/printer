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


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STRATEGIES_FILE = os.path.join(BASE_DIR, "strategies.json")
RESTART_BACKOFF = 10   # seconds to wait before restarting a crashed process
POLL_INTERVAL   = 5    # seconds between health-check loops


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
    env["BOT_DB_FILE"]     = strategy["db_file"]
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
        _procs.append({"name": s["name"], "strategy": s, "proc": proc, "last_crash": 0.0})
        print(f"[runner]   '{s['name']}' PID={proc.pid}  state={s['state_file']}")

    print(f"[runner] {len(strategies)} strategy instance(s) running. Ctrl+C to stop.\n")

    while True:
        time.sleep(POLL_INTERVAL)
        now = time.time()

        for entry in _procs:
            proc = entry["proc"]
            if proc.poll() is None:
                continue

            code        = proc.returncode
            since_crash = now - entry["last_crash"]

            if entry["last_crash"] == 0.0:
                print(f"[runner] '{entry['name']}' exited ({code}). "
                      f"Waiting {RESTART_BACKOFF}s before restart ...")
                entry["last_crash"] = now
            elif since_crash >= RESTART_BACKOFF:
                new_proc = _start_bot(entry["strategy"])
                entry["proc"]       = new_proc
                entry["last_crash"] = 0.0
                print(f"[runner] '{entry['name']}' restarted (PID={new_proc.pid}).")

        running = [e["name"] for e in _procs if e["proc"].poll() is None]
        print(f"[runner] OK | bots running: {running}")


if __name__ == "__main__":
    main()
