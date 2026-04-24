#!/usr/bin/env python3
"""
run_sweep.py -- Kalshi bot backtest suite.
Run from project root: py run_sweep.py

Sections
--------
  1. Simple Backtest        -- 6 param combos x all assets
  2. Walk-Forward Analysis  -- 8-window rolling OOS validation, per asset
  3. Monte Carlo Stress     -- execution noise (slippage / latency), per asset
"""

import os
import subprocess
import sys
import time

ROOT   = os.path.dirname(os.path.abspath(__file__))
PY     = sys.executable
BT     = os.path.join(ROOT, "backtest.py")
AMOUNT = 25.0
ASSETS = ["BTC", "ETH", "SOL", "XRP"]

# ---------------------------------------------------------------------------
# Parameter combinations (used for Section 1 simple backtest only)
# WFA and MC use their own internal param search / best-params from prior runs
# ---------------------------------------------------------------------------
COMBOS = [
    # label                      ev     vol    conf
    ("EV=5%  VOL=1.20 CONF=76", 0.05, 1.20,  76),
    ("EV=7%  VOL=1.50 CONF=76", 0.07, 1.50,  76),
    ("EV=9%  VOL=1.80 CONF=76", 0.09, 1.80,  76),
    ("EV=5%  VOL=1.80 CONF=76", 0.05, 1.80,  76),
    ("EV=7%  VOL=1.80 CONF=76", 0.07, 1.80,  76),
    ("EV=10% VOL=1.80 CONF=76", 0.10, 1.80,  76),
]

# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
_log: list[tuple[str, bool, float]] = []

def run(tag: str, cmd: list) -> int:
    bar = "=" * 72
    print(f"\n{bar}", flush=True)
    print(f"  {tag}", flush=True)
    print(f"  $ {' '.join(str(x) for x in cmd)}", flush=True)
    print(bar, flush=True)
    t0 = time.time()
    rc = subprocess.run(cmd, cwd=ROOT).returncode
    elapsed = time.time() - t0
    status = "OK" if rc == 0 else f"FAILED (rc={rc})"
    print(f"\n  [{status}]  {elapsed:.0f}s elapsed", flush=True)
    _log.append((tag, rc == 0, elapsed))
    return rc


# ===========================================================================
# Section 1 — Simple Backtest
# ===========================================================================
print("\n" + "#" * 72)
print("#  SECTION 1 — SIMPLE BACKTEST  (6 combos x BTC/ETH/SOL/XRP)")
print("#" * 72)

for label, ev, vol, conf in COMBOS:
    run(
        f"BACKTEST  {label}",
        [PY, BT,
         "--ev",         str(ev),
         "--vol-gate",   str(vol),
         "--confidence", str(conf),
         "--amount",     str(AMOUNT),
         "--asset"]      + ASSETS,
    )


# ===========================================================================
# Section 2 — Walk-Forward Analysis (per asset)
# ===========================================================================
print("\n" + "#" * 72)
print("#  SECTION 2 — WALK-FORWARD ANALYSIS  (8 windows, 50 MC sims each)")
print("#  Internal param search per window — reports OOS efficiency ratio.")
print("#  Runs once per asset.")
print("#" * 72)

for asset in ASSETS:
    run(
        f"WFA  {asset}  windows=8  mc-sims=50",
        [PY, BT,
         "--walk-forward",
         "--wf-windows", "8",
         "--wf-mc-sims", "50",
         "--amount",     str(AMOUNT),
         "--asset",      asset],
    )


# ===========================================================================
# Section 3 — Monte Carlo Stress Test (per asset)
# ===========================================================================
print("\n" + "#" * 72)
print("#  SECTION 3 — MONTE CARLO STRESS TEST  (slippage + latency noise)")
print("#  Uses best params found by WFA. 300 noise iters, 20 bps max slip.")
print("#  Runs once per asset.")
print("#" * 72)

for asset in ASSETS:
    run(
        f"STRESS-TEST  {asset}  iters=300  slip=20bps",
        [PY, BT,
         "--stress-test",
         "--st-iters",        "300",
         "--st-max-slippage", "20",
         "--amount",          str(AMOUNT),
         "--asset",           asset],
    )


# ===========================================================================
# Summary
# ===========================================================================
total_s = sum(t for _, _, t in _log)
n_ok    = sum(1 for _, ok, _ in _log if ok)
n_fail  = len(_log) - n_ok

print("\n" + "=" * 72)
print("  SUITE SUMMARY")
print("=" * 72)
for tag, ok, elapsed in _log:
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}]  {elapsed:>5.0f}s  {tag}")

mins, secs = divmod(int(total_s), 60)
print(f"\n  {n_ok}/{len(_log)} passed   Total time: {mins}m {secs}s")
if n_fail:
    print(f"  {n_fail} run(s) failed — check output above.")
    sys.exit(1)
print("=" * 72)
