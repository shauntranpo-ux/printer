#!/usr/bin/env python3
"""
run_altcoin_sweep.py — ETH / SOL / XRP parameter optimisation.

Grid: min_ev × vol_gate × confidence  (4×4×4 = 64 combos per asset, 192 total)
Then: WFA (8 windows, 50 MC sims per window) with best params per asset
Then: Monte Carlo stress test (300 iters, 20 bps slip) with best params per asset

Best params selected by per-trade Sharpe (honest metric, not inflated annualised).
"""

import json, os, sqlite3, subprocess, sys, time
from datetime import datetime, timezone
from itertools import product

ROOT   = os.path.dirname(os.path.abspath(__file__))
PY     = sys.executable
BT     = os.path.join(ROOT, "backtest.py")
AMOUNT = 25.0
ASSETS = ["ETH", "SOL", "XRP"]

EV_VALUES   = [0.05, 0.07, 0.09, 0.11]
VOL_VALUES  = [1.5,  1.8,  2.0,  2.5]
CONF_VALUES = [65,   70,   76,   80]

SWEEP_START = datetime.now(timezone.utc).isoformat()

# ── helpers ──────────────────────────────────────────────────────────────────

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


def best_params_from_db(asset: str) -> dict:
    """Query DB for highest per-trade Sharpe for this asset since sweep start."""
    db_path = os.path.join(ROOT, "kalshi_bot.db")
    fallback = {"min_ev": 0.07, "vol_threshold": 1.80, "min_confidence": 70}
    try:
        if not os.path.exists(db_path):
            print(f"  [{asset}] DB not found — using fallback defaults")
            return fallback
        with sqlite3.connect(db_path) as conn:
            # Try with min_confidence column (added in this run)
            try:
                row = conn.execute("""
                    SELECT min_ev, vol_threshold, min_confidence, sharpe_ratio
                    FROM   stress_test_results
                    WHERE  asset = ?
                      AND  run_ts >= ?
                    ORDER  BY sharpe_ratio DESC
                    LIMIT  1
                """, (asset, SWEEP_START)).fetchone()
            except sqlite3.OperationalError:
                # min_confidence column might not exist in old DB yet
                row = conn.execute("""
                    SELECT min_ev, vol_threshold, NULL, sharpe_ratio
                    FROM   stress_test_results
                    WHERE  asset = ?
                      AND  run_ts >= ?
                    ORDER  BY sharpe_ratio DESC
                    LIMIT  1
                """, (asset, SWEEP_START)).fetchone()

            if row:
                ev, vol, conf, sharpe = row
                print(f"  [{asset}] best: ev={ev:.0%}  vol={vol:.2f}  conf={conf}  sharpe={sharpe:.4f}")
                return {
                    "min_ev":         float(ev),
                    "vol_threshold":  float(vol),
                    "min_confidence": int(conf) if conf is not None else 70,
                }
            print(f"  [{asset}] no DB rows since sweep start — using fallback")
            return fallback
    except Exception as exc:
        print(f"  [{asset}] DB query failed: {exc} — using fallback")
        return fallback


# ── SECTION 1: Grid Sweep ────────────────────────────────────────────────────

n_combos = len(EV_VALUES) * len(VOL_VALUES) * len(CONF_VALUES)
print("\n" + "#" * 72)
print(f"#  SECTION 1 — GRID SWEEP")
print(f"#  {n_combos} combos × {len(ASSETS)} assets = {n_combos * len(ASSETS)} runs")
print(f"#  EV:   {EV_VALUES}")
print(f"#  VOL:  {VOL_VALUES}")
print(f"#  CONF: {CONF_VALUES}")
print("#" * 72)

for asset in ASSETS:
    print(f"\n{'='*72}")
    print(f"  GRID: {asset}  ({n_combos} combos)")
    print(f"{'='*72}")
    for ev, vol, conf in product(EV_VALUES, VOL_VALUES, CONF_VALUES):
        tag = f"GRID {asset}  ev={ev:.0%} vol={vol:.1f} conf={conf}"
        run(tag, [PY, BT,
                  "--ev",         str(ev),
                  "--vol-gate",   str(vol),
                  "--confidence", str(conf),
                  "--amount",     str(AMOUNT),
                  "--asset",      asset])

# ── Find best params per asset ───────────────────────────────────────────────

print("\n" + "#" * 72)
print("#  GRID COMPLETE — querying DB for best params per asset")
print("#" * 72)

best_params: dict[str, dict] = {}
for asset in ASSETS:
    best_params[asset] = best_params_from_db(asset)

print("\n  Best params by asset (will be used for WFA + stress test):")
for asset, p in best_params.items():
    print(f"    {asset}: ev={p['min_ev']:.0%}  vol={p['vol_threshold']:.2f}  conf={p['min_confidence']}")


# ── SECTION 2: Walk-Forward Analysis ────────────────────────────────────────

print("\n" + "#" * 72)
print("#  SECTION 2 — WALK-FORWARD ANALYSIS  (8 windows, 50 MC sims each)")
print("#" * 72)

for asset in ASSETS:
    p = best_params[asset]
    run(
        f"WFA  {asset}  ev={p['min_ev']:.0%} vol={p['vol_threshold']:.2f} conf={p['min_confidence']}",
        [PY, BT,
         "--walk-forward",
         "--wf-windows",    "8",
         "--wf-mc-sims",    "50",
         "--ev",            str(p["min_ev"]),
         "--vol-gate",      str(p["vol_threshold"]),
         "--confidence",    str(p["min_confidence"]),
         "--amount",        str(AMOUNT),
         "--asset",         asset],
    )


# ── SECTION 3: Monte Carlo Stress Test ──────────────────────────────────────

MC_RESULTS_PATH = os.path.join(ROOT, "monte_carlo_results.json")

print("\n" + "#" * 72)
print("#  SECTION 3 — MONTE CARLO STRESS TEST  (300 iters, 20 bps slip)")
print("#" * 72)

for asset in ASSETS:
    p = best_params[asset]

    mc_entry = {"top_20": [{"params": {"min_ev": p["min_ev"], "vol_threshold": p["vol_threshold"]}, "sharpe_ratio": 0.0}]}
    with open(MC_RESULTS_PATH, "w") as fh:
        json.dump(mc_entry, fh)

    run(
        f"STRESS  {asset}  ev={p['min_ev']:.0%} vol={p['vol_threshold']:.2f} conf={p['min_confidence']}  300×20bps",
        [PY, BT,
         "--stress-test",
         "--st-iters",         "300",
         "--st-max-slippage",  "20",
         "--ev",               str(p["min_ev"]),
         "--vol-gate",         str(p["vol_threshold"]),
         "--confidence",       str(p["min_confidence"]),
         "--amount",           str(AMOUNT),
         "--asset",            asset],
    )


# ── Summary ──────────────────────────────────────────────────────────────────

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
print("\n  Recommended params per asset:")
for asset, p in best_params.items():
    print(f"    {asset}: min_ev={p['min_ev']:.0%}  vol_thresh={p['vol_threshold']:.2f}  confidence={p['min_confidence']}")
if n_fail:
    print(f"\n  {n_fail} run(s) failed — check output above.")
    sys.exit(1)
print("=" * 72)
