"""
scripts/backtest_carry.py - test the S6 window-carry thesis on real Kalshi settlements.

S6 buys the direction the PREVIOUS 15-min window resolved, at ~50c early in the next
window. Its whole edge is persistence: P(result[i+1] == result[i]) must clear the
~50c-entry breakeven (0.5 + fee at 50c ~ 0.5175 -> ~53.5% with round-trip padding).
data/historical/*_kalshi_settlements.parquet gives per-window (window_open, close_time,
ticker, strike, result) - enough to measure that rate directly, plus the conditional
rate behind S6's s6_min_prev_move gate (approximated by the strike step between
consecutive windows: Kalshi anchors each window's strike near the spot at open, so
strike[i+1]-strike[i] ~ the move during window i).

Offline analysis only - NOT loaded at runtime, and pyarrow is deliberately not a bot
dependency.

    python3 scripts/backtest_carry.py [data/historical]
"""
import glob
import math
import os
import sys

try:
    import pandas as pd
except ImportError:
    print("pandas required"); sys.exit(1)

_MIN_PREV_MOVE = 0.0008    # mirrors config s6_min_prev_move
_ENTRY = 0.50              # S6's 40-60c band midpoint
_FEE = 0.07 * _ENTRY * (1 - _ENTRY)   # taker fee at a 50c entry


def _load(path):
    try:
        return pd.read_parquet(path)
    except ImportError:
        print("pyarrow/fastparquet required to read parquet - `pip install pyarrow`")
        sys.exit(1)


def analyze(path):
    df = _load(path).sort_values("window_open").reset_index(drop=True)
    asset = os.path.basename(path).split("_")[0]
    # Strictly consecutive windows only: next opens when previous closes (<=20 min gap).
    gap = (df["window_open"].shift(-1) - df["close_time"]).dt.total_seconds()
    consec = gap.fillna(1e9) <= 300.0
    prev_res = df["result"].astype(int)
    next_res = df["result"].shift(-1)
    strike = df["strike"]
    step = (df["strike"].shift(-1) - strike).abs() / strike
    m = consec & next_res.notna()
    n = int(m.sum())
    same = int((prev_res[m] == next_res[m]).sum())
    p_all = same / n if n else float("nan")
    # Decisive previous windows only (S6's gate).
    md = m & (step >= _MIN_PREV_MOVE)
    nd = int(md.sum())
    same_d = int((prev_res[md] == next_res[md]).sum())
    p_dec = same_d / nd if nd else float("nan")
    return asset, n, p_all, nd, p_dec


def net_per_contract(p):
    """Expected net $/contract buying the carried side at ~50c."""
    return p * (1 - _ENTRY) - (1 - p) * _ENTRY - _FEE


def main():
    data_dir = sys.argv[1] if len(sys.argv) > 1 else "data/historical"
    paths = sorted(glob.glob(os.path.join(data_dir, "*_kalshi_settlements.parquet")))
    if not paths:
        print(f"no settlement parquets under {data_dir}")
        sys.exit(1)
    breakeven = _ENTRY + _FEE   # p at which net/ct = 0 for a 50c entry
    print(f"S6 window-carry thesis vs real settlements (breakeven p at {_ENTRY*100:.0f}c "
          f"entry = {breakeven:.3f})\n")
    print(f"{'asset':<6} {'pairs':>6} {'P(carry)':>9} {'net$/ct':>8}   "
          f"{'decisive':>8} {'P|decisive':>10} {'net$/ct':>8}")
    print("-" * 66)
    tot_n = tot_same = tot_nd = tot_same_d = 0
    for path in paths:
        asset, n, p_all, nd, p_dec = analyze(path)
        tot_n += n; tot_same += round(p_all * n) if n else 0
        tot_nd += nd; tot_same_d += round(p_dec * nd) if nd else 0
        print(f"{asset:<6} {n:>6} {p_all:>9.3f} {net_per_contract(p_all):>+8.4f}   "
              f"{nd:>8} {p_dec:>10.3f} {net_per_contract(p_dec):>+8.4f}")
    if tot_n:
        p = tot_same / tot_n
        pd_ = tot_same_d / tot_nd if tot_nd else float("nan")
        print("-" * 66)
        print(f"{'ALL':<6} {tot_n:>6} {p:>9.3f} {net_per_contract(p):>+8.4f}   "
              f"{tot_nd:>8} {pd_:>10.3f} {net_per_contract(pd_):>+8.4f}")
        # Wilson lower bounds on BOTH sides of the bet - the honest read. If carry
        # fails, its mirror (fade the previous window) may clear breakeven instead.
        def _wilson_lb(ph, nn, z=1.645):
            denom = 1 + z * z / nn
            centre = ph + z * z / (2 * nn)
            adj = z * math.sqrt(ph * (1 - ph) / nn + z * z / (4 * nn * nn))
            return (centre - adj) / denom

        if tot_nd:
            carry_lb = _wilson_lb(pd_, tot_nd)
            fade_p = 1.0 - pd_
            fade_lb = _wilson_lb(fade_p, tot_nd)
            print(f"\nDecisive-window pooled rates (n={tot_nd}):")
            print(f"  CARRY p={pd_:.3f}  Wilson-LB={carry_lb:.3f}  net/ct {net_per_contract(pd_):+.4f}")
            print(f"  FADE  p={fade_p:.3f}  Wilson-LB={fade_lb:.3f}  net/ct {net_per_contract(fade_p):+.4f}")
            print(f"  breakeven p at {_ENTRY*100:.0f}c entry = {breakeven:.3f}")
            if carry_lb > breakeven:
                print("VERDICT: persistence edge SUPPORTED - carry (S6 as designed) has a real prior.")
            elif fade_lb > breakeven:
                print("VERDICT: windows ANTI-persist - the FADE side clears breakeven with "
                      "statistical confidence. S6 should buy the OPPOSITE of the previous "
                      "window's direction.")
            else:
                print("VERDICT: neither side clears breakeven - no window-to-window edge in history.")


if __name__ == "__main__":
    main()
