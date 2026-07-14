"""
scripts/tune_fade.py - where does the S6 window-fade edge concentrate?

backtest_carry.py established the headline: after a decisive 15-min window the next
one REVERSES 53.4% of the time (fade side clears the ~51.7% breakeven at 50c). This
tool conditions that fade rate on everything the settlement history supports -
previous-move magnitude, same-direction streak length, ET session, asset - so S6's
gates can be tuned to the strongest sub-population BEFORE any live data is spent.

Offline analysis only (pyarrow required, deliberately not a bot dependency).

    python3 scripts/tune_fade.py [data/historical]
"""
import glob
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import pandas as pd
except ImportError:
    print("pandas required"); sys.exit(1)

import sessions

_ENTRY = 0.50
_FEE = 0.07 * _ENTRY * (1 - _ENTRY)
_BREAKEVEN = _ENTRY + _FEE

_MAG_BUCKETS = [(0, 8), (8, 15), (15, 30), (30, 50), (50, 10_000)]   # basis points
_MIN_CELL_N = 1000


def _wilson_lb(p, n, z=1.645):
    if n == 0:
        return float("nan")
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    adj = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - adj) / denom


def _net(p):
    return p * (1 - _ENTRY) - (1 - p) * _ENTRY - _FEE


def _load_pairs(data_dir):
    """One row per consecutive window pair across all assets:
    (asset, session, prev_move_bp, streak, fade_win)."""
    rows = []
    for path in sorted(glob.glob(os.path.join(data_dir, "*_kalshi_settlements.parquet"))):
        asset = os.path.basename(path).split("_")[0]
        df = pd.read_parquet(path).sort_values("window_open").reset_index(drop=True)
        res = df["result"].astype(int).tolist()
        opens = df["window_open"].tolist()
        closes = df["close_time"].tolist()
        strikes = df["strike"].tolist()
        streak = 1
        for i in range(len(df) - 1):
            gap = (opens[i + 1] - closes[i]).total_seconds()
            # streak of same-direction results ending at window i
            if i > 0 and res[i] == res[i - 1] and (opens[i] - closes[i - 1]).total_seconds() <= 300:
                streak += 1
            else:
                streak = 1
            if gap > 300:
                continue
            if not strikes[i] or not strikes[i + 1]:
                continue
            move_bp = abs(strikes[i + 1] - strikes[i]) / strikes[i] * 1e4
            fade_win = int(res[i + 1] != res[i])
            rows.append((asset, sessions.session_for_dt(opens[i + 1]), move_bp,
                         streak, fade_win))
    return pd.DataFrame(rows, columns=["asset", "session", "move_bp", "streak", "fade"])


def _print_bucket(label, sub, days):
    n = len(sub)
    if n == 0:
        print(f"  {label:<22} (empty)")
        return
    p = sub["fade"].mean()
    lb = _wilson_lb(p, n)
    thin = "  [THIN <1000 - do not act]" if n < _MIN_CELL_N else ""
    per_day = n / days / 4.0   # per asset per day
    print(f"  {label:<22} n={n:>6}  fade={p:.3f}  WLB={lb:.3f}  net/ct={_net(p):+.4f}  "
          f"~{per_day:.1f}/asset/day{thin}")


def main():
    data_dir = sys.argv[1] if len(sys.argv) > 1 else "data/historical"
    pairs = _load_pairs(data_dir)
    if pairs.empty:
        print(f"no settlement parquets under {data_dir}")
        sys.exit(1)
    days = 68.0   # span of the fixture data
    print(f"Window-fade conditioning over {len(pairs)} consecutive pairs "
          f"(breakeven p at {_ENTRY*100:.0f}c = {_BREAKEVEN:.3f})\n")

    print("BY PREVIOUS-MOVE MAGNITUDE (strike step, bp):")
    for lo, hi in _MAG_BUCKETS:
        sub = pairs[(pairs.move_bp >= lo) & (pairs.move_bp < hi)]
        _print_bucket(f"{lo}-{hi if hi < 10_000 else '+'}bp", sub, days)

    print("\nBY STREAK LENGTH (same-direction windows before the fade):")
    for lab, sel in (("streak 1", pairs.streak == 1), ("streak 2", pairs.streak == 2),
                     ("streak 3+", pairs.streak >= 3)):
        _print_bucket(lab, pairs[sel], days)

    print("\nBY ET SESSION (of the faded window):")
    for sess in sessions.ET_SESSION_ORDER:
        _print_bucket(sess, pairs[pairs.session == sess], days)

    print("\nBY ASSET:")
    for asset in sorted(pairs.asset.unique()):
        _print_bucket(asset, pairs[pairs.asset == asset], days)

    print("\nGATE CANDIDATES (min move x min streak - S6 trades pairs passing BOTH):")
    print(f"  {'gate':<28} {'n':>6} {'fade':>6} {'WLB':>6} {'net/ct':>8} {'/asset/day':>11}")
    best = None
    for min_bp in (0, 8, 15, 30, 50):
        for min_streak in (1, 2, 3):
            sub = pairs[(pairs.move_bp >= min_bp) & (pairs.streak >= min_streak)]
            n = len(sub)
            if n == 0:
                continue
            p = sub["fade"].mean()
            lb = _wilson_lb(p, n)
            per_day = n / days / 4.0
            mark = ""
            # Actionable: statistically positive, thick enough, and enough flow to
            # reach 200 settled trades within ~2 weeks (>= ~1.5/asset/day * 4 assets).
            if n >= _MIN_CELL_N and lb > _BREAKEVEN and per_day >= 1.5:
                if best is None or lb * _net(p) > best[0]:
                    best = (lb * _net(p), min_bp, min_streak, n, p, lb, per_day)
                    mark = "  <-"
            print(f"  move>={min_bp:>2}bp & streak>={min_streak:<2}     {n:>6} {p:>6.3f} "
                  f"{lb:>6.3f} {_net(p):>+8.4f} {per_day:>11.1f}{mark}")
    print()
    if best:
        _, bp, st, n, p, lb, per_day = best
        print(f"RECOMMENDATION: s6_min_prev_move={bp/1e4:.4f} "
              f"{'(+ s6_min_streak=' + str(st) + ')' if st > 1 else '(no streak gate)'} - "
              f"fade {p:.3f} (WLB {lb:.3f}) on n={n}, ~{per_day:.1f} trades/asset/day, "
              f"expected net/ct {_net(p):+.4f} at 50c.")
        print(f"Suggested s6_fade_premium = {p - 0.5:.3f} (the measured premium over the mid).")
    else:
        print("RECOMMENDATION: no conditional gate beats the unconditional decisive fade "
              "with acceptable flow - keep current defaults.")


if __name__ == "__main__":
    main()
