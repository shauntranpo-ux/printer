#!/usr/bin/env python3
"""
run_sweep.py -- Kalshi bot backtest suite (simplified strategy).

Sections
--------
  1. Grid Sweep   -- min_ev x vol_thresh grid per 15m asset, find best params
  2. WFA          -- 8-window walk-forward per asset (uses own internal param search)
  3. Stress Test  -- Monte Carlo execution noise per asset (uses best grid params)
"""

import json
import os
import subprocess
import sys
import time

ROOT   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY     = sys.executable
BT     = os.path.join(ROOT, "backtest.py")
AMOUNT = 25.0
ASSETS = ["BTC", "ETH", "SOL", "XRP"]

MC_RESULTS_PATH = os.path.join(ROOT, "monte_carlo_results.json")

# 2-D parameter grid
EV_VALUES  = [0.03, 0.05, 0.07, 0.09, 0.10]   # min_ev as fraction
VOL_VALUES = [1.2,  1.5,  1.8,  2.0,  2.5]    # vol_ratio_threshold

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
# Section 1 — Grid Sweep (min_ev x vol_thresh per asset)
# ===========================================================================
print("\n" + "#" * 72)
print(f"#  SECTION 1 — GRID SWEEP  ({len(EV_VALUES)}x{len(VOL_VALUES)} combos x {len(ASSETS)} assets)")
print("#  Finds best min_ev + vol_thresh per 15m market (simplified strategy)")
print("#" * 72)

# best_params_by_asset[asset] = {"min_ev": float, "vol_threshold": float}
best_params_by_asset: dict[str, dict] = {}

for asset in ASSETS:
    print(f"\n{'='*72}")
    print(f"  GRID: {asset}  ({len(EV_VALUES) * len(VOL_VALUES)} combos)")
    print(f"{'='*72}")

    asset_results = []
    for ev in EV_VALUES:
        for vol in VOL_VALUES:
            tag = f"GRID {asset}  ev={ev:.0%} vol={vol:.1f}"
            rc  = run(
                tag,
                [PY, BT,
                 "--ev",       str(ev),
                 "--vol-gate", str(vol),
                 "--amount",   str(AMOUNT),
                 "--asset",    asset],
            )
            asset_results.append({"asset": asset, "ev": ev, "vol": vol, "ok": rc == 0})

    # Load the DB to find best Sharpe for this asset (backtest writes to SQLite)
    # Since we can't easily read the DB here, use a simple subprocess call to
    # get the last result's metrics — we track "best" by running all combos and
    # reading the DB summary at the end.
    # For now, use the last-run result as a proxy; a full DB query would be more
    # accurate but requires importing the backtest module here.
    # Set a conservative default if all failed.
    best_params_by_asset[asset] = {
        "min_ev":        0.07,   # fallback default
        "vol_threshold": 1.80,
    }

# After all grid runs complete, query the DB for best Sharpe per asset
print("\n" + "#" * 72)
print("#  GRID COMPLETE — querying DB for best params per asset")
print("#" * 72)

try:
    import sqlite3
    _db_path = os.path.join(ROOT, "results", "backtest_results.db")
    if os.path.exists(_db_path):
        with sqlite3.connect(_db_path) as _conn:
            for asset in ASSETS:
                _row = _conn.execute("""
                    SELECT min_ev, vol_threshold, sharpe_ratio
                    FROM   stress_test_results
                    WHERE  asset = ?
                    ORDER  BY sharpe_ratio DESC
                    LIMIT  1
                """, (asset,)).fetchone()
                if _row:
                    best_params_by_asset[asset] = {
                        "min_ev":        float(_row[0]),
                        "vol_threshold": float(_row[1]),
                    }
                    print(f"  {asset}: best ev={_row[0]:.0%}  vol={_row[1]:.2f}"
                          f"  sharpe={_row[2]:.3f}")
                else:
                    print(f"  {asset}: no DB results — using fallback defaults")
    else:
        print(f"  DB not found at {_db_path} — using fallback defaults")
except Exception as exc:
    print(f"  DB query failed: {exc} — using fallback defaults")

print("\n  Best params by asset:")
for asset, params in best_params_by_asset.items():
    print(f"    {asset}: ev={params['min_ev']:.0%}  vol={params['vol_threshold']:.2f}")


# ===========================================================================
# Section 2 — Walk-Forward Analysis (per asset, internal param search)
# ===========================================================================
print("\n" + "#" * 72)
print("#  SECTION 2 — WALK-FORWARD  (8 windows, 50 MC sims each, per asset)")
print("#  Internal param search per window — reports OOS efficiency ratio.")
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
# Section 3 — Monte Carlo Stress Test (per asset, using best grid params)
# ===========================================================================
print("\n" + "#" * 72)
print("#  SECTION 3 — STRESS TEST  (300 noise iters, 20 bps max slip, per asset)")
print("#  Uses best params from Section 1 grid sweep.")
print("#" * 72)

for asset in ASSETS:
    params = best_params_by_asset[asset]

    # Write best params to monte_carlo_results.json so stress_test reads them
    _mc_entry = {
        "top_20": [{
            "params": {
                "min_ev":        params["min_ev"],
                "vol_threshold": params["vol_threshold"],
            },
            "sharpe_ratio": 0.0,  # placeholder
        }]
    }
    with open(MC_RESULTS_PATH, "w") as _fh:
        json.dump(_mc_entry, _fh)

    run(
        f"STRESS-TEST  {asset}  iters=300  slip=20bps  "
        f"(ev={params['min_ev']:.0%} vol={params['vol_threshold']:.2f})",
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
print("\n  Best params per asset (from grid sweep):")
for asset, params in best_params_by_asset.items():
    print(f"    {asset}: min_ev={params['min_ev']:.0%}  vol_thresh={params['vol_threshold']:.2f}")
if n_fail:
    print(f"\n  {n_fail} run(s) failed — check output above.")
    sys.exit(1)
print("=" * 72)
