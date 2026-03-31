"""
runner.py — Launches server.py plus one bot.py subprocess per enabled strategy.

Each strategy is defined in strategies.json. The runner monitors all processes
and auto-restarts any that crash, with a 10-second backoff per process.

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


# ── Process registry ────────────────────────────────────────────────────────────
# Each entry: {"name": str, "proc": Popen, "env": dict, "last_crash": float}
_procs: list[dict] = []
_server_entry: dict | None = None


def _load_strategies(path: str) -> list[dict]:
    """Read and validate strategies.json. Returns only enabled entries."""
    try:
        with open(path) as fh:
            strategies = json.load(fh)
    except FileNotFoundError:
        print(f"[runner] strategies.json not found at {path}. "
              f"Copy strategies.json.example or create one.")
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
    """Build the subprocess environment for a strategy."""
    env = os.environ.copy()
    env["BOT_CONFIG_FILE"] = strategy["config_file"]
    env["BOT_DB_FILE"]     = strategy["db_file"]
    env["BOT_STATE_FILE"]  = strategy["state_file"]
    return env


def _start_bot(strategy: dict) -> subprocess.Popen:
    name = strategy["name"]
    env  = _build_env(strategy)
    print(f"[runner] Starting strategy '{name}' ...")
    return subprocess.Popen(
        [sys.executable, os.path.join(BASE_DIR, "bot.py")],
        env=env,
        cwd=BASE_DIR,
    )


def _start_server() -> subprocess.Popen:
    print("[runner] Starting server.py ...")
    return subprocess.Popen(
        [sys.executable, os.path.join(BASE_DIR, "server.py")],
        cwd=BASE_DIR,
    )


def _shutdown(signum, frame):
    """Terminate all subprocesses on Ctrl+C / SIGTERM."""
    print("\n[runner] Shutting down all processes ...")
    all_entries = _procs + ([_server_entry] if _server_entry else [])
    for entry in all_entries:
        proc = entry.get("proc")
        if proc and proc.poll() is None:
            proc.terminate()
            print(f"[runner]   terminated '{entry['name']}'")
    sys.exit(0)


def main():
    global _server_entry

    parser = argparse.ArgumentParser()
    parser.add_argument("--strategies", default=DEFAULT_STRATEGIES_FILE,
                        help="Path to strategies.json")
    args = parser.parse_args()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    os.chdir(BASE_DIR)

    strategies = _load_strategies(args.strategies)
    if not strategies:
        print("[runner] No enabled strategies found. Check strategies.json.")
        sys.exit(1)

    # Launch all bot instances
    for s in strategies:
        proc = _start_bot(s)
        _procs.append({
            "name":       s["name"],
            "strategy":   s,
            "proc":       proc,
            "last_crash": 0.0,
        })
        print(f"[runner]   '{s['name']}' PID={proc.pid}  "
              f"state={s['state_file']}")

    # Launch server
    server_proc = _start_server()
    _server_entry = {"name": "server.py", "proc": server_proc, "last_crash": 0.0}
    print(f"[runner] server.py PID={server_proc.pid}")
    print(f"[runner] Dashboard: http://localhost:5000")
    print(f"[runner] Running {len(strategies)} strategy instance(s). "
          f"Ctrl+C to stop.\n")

    # ── Monitor loop ─────────────────────────────────────────────────────────
    while True:
        time.sleep(POLL_INTERVAL)

        now = time.time()

        # Check each strategy bot
        for entry in _procs:
            proc = entry["proc"]
            if proc.poll() is None:
                continue   # still running

            code = proc.returncode
            since_crash = now - entry["last_crash"]
            if since_crash < RESTART_BACKOFF:
                # Still in backoff — report but don't restart yet
                remaining = int(RESTART_BACKOFF - since_crash)
                print(f"[runner] '{entry['name']}' exited ({code}). "
                      f"Restarting in {remaining}s ...")
                continue

            if entry["last_crash"] == 0.0:
                # First crash — start the backoff clock
                print(f"[runner] '{entry['name']}' exited ({code}). "
                      f"Waiting {RESTART_BACKOFF}s before restart ...")
                entry["last_crash"] = now
            else:
                # Backoff elapsed — restart
                new_proc = _start_bot(entry["strategy"])
                entry["proc"]       = new_proc
                entry["last_crash"] = 0.0
                print(f"[runner] '{entry['name']}' restarted (PID={new_proc.pid}).")

        # Check server
        if _server_entry:
            sproc = _server_entry["proc"]
            if sproc.poll() is not None:
                code  = sproc.returncode
                since = now - _server_entry["last_crash"]
                if _server_entry["last_crash"] == 0.0:
                    print(f"[runner] server.py exited ({code}). "
                          f"Waiting {RESTART_BACKOFF}s before restart ...")
                    _server_entry["last_crash"] = now
                elif since >= RESTART_BACKOFF:
                    new_sproc = _start_server()
                    _server_entry["proc"]       = new_sproc
                    _server_entry["last_crash"] = 0.0
                    print(f"[runner] server.py restarted (PID={new_sproc.pid}).")

        # Status line
        running = [e["name"] for e in _procs if e["proc"].poll() is None]
        print(f"[runner] OK | bots running: {running}")


if __name__ == "__main__":
    main()
