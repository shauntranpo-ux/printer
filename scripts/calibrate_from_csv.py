"""
scripts/calibrate_from_csv.py
Read the trades CSV export and compute per-bucket win rates with Wilson CI.
Prints the _S1_WIN_RATE dict to paste into bot_strategy.py.

Usage:
    python scripts/calibrate_from_csv.py <path_to_trades.csv>
"""
import csv, json, math, sys

# Must match bot_strategy.py constants exactly
DIST_BOUNDS = [0.005, 0.010, 0.020]
TIME_BOUNDS  = [6.0, 9.0]

def dist_bucket(ap: float) -> int:
    for i, b in enumerate(DIST_BOUNDS):
        if ap < b:
            return i
    return len(DIST_BOUNDS)

def time_bucket(ml: float) -> int:
    for i, b in enumerate(TIME_BOUNDS):
        if ml < b:
            return i
    return len(TIME_BOUNDS)

def wilson_lower(wins: int, n: int, z: float = 1.645) -> float:
    """95% one-sided Wilson CI lower bound."""
    if n == 0:
        return 0.0
    p = wins / n
    num = p + z*z/(2*n) - z * math.sqrt((p*(1-p) + z*z/(4*n)) / n)
    den = 1 + z*z/n
    return num / den

def main():
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print("Usage: python scripts/calibrate_from_csv.py <trades.csv>")
        sys.exit(1)

    from collections import defaultdict
    buckets: dict = defaultdict(lambda: [0, 0])  # [wins, total]

    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("brain") != "s1":
                continue
            if row.get("outcome") not in ("win", "loss"):
                continue
            try:
                sigs = json.loads(row["entry_signals"])
                ap   = float(sigs.get("abs_pct", 0))
                ml   = float(row["seconds_left_at_entry"]) / 60.0
                di   = dist_bucket(ap)
                ti   = time_bucket(ml)
                key  = (row["asset"], di, ti)
                buckets[key][1] += 1
                if row["outcome"] == "win":
                    buckets[key][0] += 1
            except Exception:
                continue

    ASSETS = ["BTC", "ETH", "SOL", "XRP", "DOGE"]
    N_DIST  = len(DIST_BOUNDS) + 1  # 4 buckets
    N_TIME  = len(TIME_BOUNDS) + 1  # 3 buckets

    print("=" * 60)
    print("CALIBRATION RESULTS")
    print("=" * 60)
    print()

    out: dict = {}
    for asset in ASSETS:
        out[asset] = {}
        print(f"Asset: {asset}")
        for di in range(N_DIST):
            for ti in range(N_TIME):
                wins, total = buckets.get((asset, di, ti), [0, 0])
                if total == 0:
                    out[asset][(di, ti)] = None
                    print(f"  dist={di} time={ti}: n=0 -> None (no data)")
                    continue
                wr  = wins / total
                wlb = wilson_lower(wins, total)
                breakeven = 0.40
                usable = total >= 20 and wlb > breakeven
                val = round(wr, 4) if usable else None
                out[asset][(di, ti)] = val
                flag = "USABLE" if usable else f"SKIP n<20 or WLB({wlb:.3f})<=BE({breakeven:.2f})"
                print(f"  dist={di} time={ti}: n={total}, WR={wr:.3f}, WLB={wlb:.3f} -> {flag}")
        print()

    print()
    print("=" * 60)
    print("PASTE THIS INTO bot_strategy.py _S1_WIN_RATE:")
    print("=" * 60)
    print("_S1_WIN_RATE: dict = {")
    for asset in ASSETS:
        items = ", ".join(
            f"({di},{ti}): {out[asset].get((di,ti), 'None')}"
            for di in range(N_DIST)
            for ti in range(N_TIME)
        )
        print(f'    "{asset}": {{{items}}},')
    print("}")

if __name__ == "__main__":
    main()
