"""
scripts/xasset_check.py - does BTC confirmation add anything to the S6 fade edge?

All four settlement histories share the same 15-min window grid, so cross-asset
conditioning is testable without any new data: when an alt's previous window moved
>= 15bp, does requiring BTC's same-window move to AGREE in direction raise the fade
edge? And does BTC's window-i RESULT predict an alt's window-i+1 result (lead-lag)?

Answer on Feb-May 2026 history: NO on both counts. Pooled alt fades at >=15bp run
Wilson-LB 0.536 with BTC agreement vs 0.536 unconditional (big alt moves co-move with
BTC ~82% of the time, so the gate barely selects), the BTC-opposed bucket is too thin
to act on (n=159), and BTC(i) vs alt(i+1) agreement (~0.48) is just the alt's own
anti-persistence bleeding through correlation. Kept as a permanent record so the idea
isn't re-tried from scratch; re-run if the settlement history grows.

Offline analysis only - NOT loaded at runtime.

    python3 scripts/xasset_check.py [data/historical]
"""
import glob
import math
import os
import sys

try:
    import pandas as pd
except ImportError:
    print("pandas required"); sys.exit(1)

_MIN_ALT_MOVE = 0.0015   # the shipped s6_min_prev_move gate
_MIN_BTC_MOVE = 0.0008   # "BTC moved too" threshold under test


def _wilson_lb(w, n, z=1.96):
    if n == 0:
        return 0.0
    p = w / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    adj = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - adj) / denom


def _pairs(path, asset):
    df = pd.read_parquet(path).sort_values("window_open").reset_index(drop=True)
    nxt = df.shift(-1)
    gap = (nxt["window_open"] - df["close_time"]).dt.total_seconds().abs()
    out = pd.DataFrame({
        "window_open": df["window_open"],
        "prev_result": df["result"].astype(float),
        "next_result": nxt["result"],
        "prev_move": (nxt["strike"] - df["strike"]) / df["strike"],
        "asset": asset,
    })[gap.fillna(1e9) <= 300]
    return out.dropna()


def main():
    data_dir = sys.argv[1] if len(sys.argv) > 1 else "data/historical"
    paths = {os.path.basename(p).split("_")[0]: p
             for p in glob.glob(os.path.join(data_dir, "*_kalshi_settlements.parquet"))}
    if "BTC" not in paths or len(paths) < 2:
        print(f"need BTC plus at least one alt settlement parquet under {data_dir}")
        sys.exit(1)
    btc = _pairs(paths["BTC"], "BTC")[["window_open", "prev_move", "prev_result"]]
    btc = btc.rename(columns={"prev_move": "btc_move", "prev_result": "btc_result"})
    alts = sorted(a for a in paths if a != "BTC")

    print("BTC(i) result vs alt(i+1) result - 15-min lead-lag:")
    frames = []
    for a in alts:
        j = _pairs(paths[a], a).merge(btc, on="window_open", how="inner")
        frames.append(j)
        n = len(j)
        agree = int((j["btc_result"] == j["next_result"]).sum())
        print(f"  {a:4s} agree {agree}/{n} = {agree / n:.3f}  WLB={_wilson_lb(agree, n):.3f}")

    allj = pd.concat(frames, ignore_index=True)
    allj["fade_win"] = (allj["prev_result"] != allj["next_result"]).astype(float)
    big = allj[allj["prev_move"].abs() >= _MIN_ALT_MOVE]
    print(f"\nPooled alt fades at prev_move>={_MIN_ALT_MOVE * 1e4:.0f}bp, by BTC same-window move:")
    buckets = (
        ("btc-agrees", (big["btc_move"] * big["prev_move"] > 0) & (big["btc_move"].abs() >= _MIN_BTC_MOVE)),
        ("btc-opposes", (big["btc_move"] * big["prev_move"] < 0) & (big["btc_move"].abs() >= _MIN_BTC_MOVE)),
        ("btc-flat", big["btc_move"].abs() < _MIN_BTC_MOVE),
        ("unconditional", big["prev_move"].notna()),
    )
    for label, mask in buckets:
        d = big[mask]
        n = len(d)
        w = int(d["fade_win"].sum())
        print(f"  {label:14s} n={n:5d} fade={w / n if n else 0:.3f} WLB={_wilson_lb(w, n):.3f}")
    print("\nVERDICT: if the btc-agrees WLB does not clearly beat unconditional (it did not"
          " on Feb-May 2026: 0.536 vs 0.536), a BTC-confirmation gate on S6 adds nothing.")


if __name__ == "__main__":
    main()
