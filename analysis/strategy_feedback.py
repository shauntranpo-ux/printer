"""
Strategy feedback report - win rate vs model prediction per strategy + version.

Usage:
    py -3 analysis/strategy_feedback.py [--db path/to/trades.db] [--min-trades 5]

Output shows, for each (strategy, version, asset, direction) group:
  - trade count
  - actual win rate
  - model's average predicted P(win) (raw_p_yes)
  - calibration gap = actual_wr - avg_model_prob
  - avg EV at entry
  - verdict (overconfident / underconfident / calibrated)

When you deploy a meaningful strategy change, bump _S1_VERSION or _S2_VERSION
in bot.py. New trades will be tagged with the new version and show up as a
separate row here, so you can compare "before" vs "after" without noise.
"""
import argparse
import sqlite3
import os
import sys
from collections import defaultdict
from datetime import datetime


def _find_db() -> str:
    candidates = [
        "trades.db",
        "bot_trades.db",
        os.path.join(os.path.dirname(__file__), "..", "trades.db"),
        os.path.join(os.path.dirname(__file__), "..", "bot_trades.db"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return os.path.abspath(p)
    return ""


def _tier_label(wr: float) -> str:
    if wr >= 0.85:   return "TIER-3"
    if wr >= 0.75:   return "TIER-2"
    if wr >= 0.60:   return "TIER-1"
    if wr >= 0.50:   return "ok"
    if wr >= 0.40:   return "below-break-even"
    return "losing"


def _gap_verdict(gap: float) -> str:
    if gap < -0.15:   return "model OVERCONFIDENT"
    if gap > 0.15:    return "model UNDERCONFIDENT"
    return "calibrated"


def run(db_path: str, min_trades: int) -> None:
    if not os.path.exists(db_path):
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # Check which columns exist
    cols = {row[1] for row in cur.execute("PRAGMA table_info(trades)")}
    has_version = "strategy_version" in cols
    has_raw_p   = "raw_p_yes" in cols
    has_implied = "implied_prob" in cols

    version_col = "strategy_version" if has_version else "NULL"
    raw_p_col   = "raw_p_yes"        if has_raw_p   else "model_prob"
    implied_col  = "implied_prob"    if has_implied  else "NULL"

    rows = cur.execute(f"""
        SELECT
            strategy_variant,
            {version_col}  AS strategy_version,
            asset,
            side,
            outcome,
            {raw_p_col}    AS raw_p_yes,
            {implied_col}  AS implied_prob,
            ts
        FROM trades
        WHERE outcome IN ('win', 'loss')
        ORDER BY ts ASC
    """).fetchall()
    con.close()

    if not rows:
        print("No completed trades found.")
        return

    # Group by (strategy_variant, strategy_version, asset, side)
    groups: dict = defaultdict(lambda: {"wins": 0, "total": 0, "p_sum": 0.0, "p_n": 0,
                                         "ev_sum": 0.0, "ev_n": 0, "first_ts": None, "last_ts": None})

    for r in rows:
        key = (
            r["strategy_variant"] or "unknown",
            r["strategy_version"] or "unversioned",
            r["asset"] or "?",
            r["side"] or "?",
        )
        g = groups[key]
        g["total"] += 1
        if r["outcome"] == "win":
            g["wins"] += 1
        if r["raw_p_yes"] is not None:
            g["p_sum"] += r["raw_p_yes"]
            g["p_n"]   += 1
        if r["implied_prob"] is not None and r["raw_p_yes"] is not None:
            ev = r["raw_p_yes"] - r["implied_prob"] - 0.07
            g["ev_sum"] += ev
            g["ev_n"]   += 1
        ts = r["ts"]
        if ts and (g["first_ts"] is None or ts < g["first_ts"]):
            g["first_ts"] = ts
        if ts and (g["last_ts"] is None or ts > g["last_ts"]):
            g["last_ts"] = ts

    # Also compute per-strategy rollup
    strategy_totals: dict = defaultdict(lambda: {"wins": 0, "total": 0, "p_sum": 0.0, "p_n": 0})
    for (sv, ver, asset, side), g in groups.items():
        st = strategy_totals[(sv, ver)]
        st["total"] += g["total"]
        st["wins"]  += g["wins"]
        st["p_sum"] += g["p_sum"]
        st["p_n"]   += g["p_n"]

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    total_trades = sum(g["total"] for g in groups.values())

    print(f"\n{'='*70}")
    print(f"  Strategy Feedback Report  |  {now}  |  {total_trades} completed trades")
    print(f"{'='*70}\n")

    current_sv = None
    current_ver = None

    for key in sorted(groups.keys()):
        sv, ver, asset, side = key
        g = groups[key]
        if g["total"] < min_trades:
            continue

        if sv != current_sv or ver != current_ver:
            # Print strategy-level header + rollup
            st = strategy_totals[(sv, ver)]
            st_wr  = st["wins"] / st["total"] if st["total"] else 0
            st_avg = st["p_sum"] / st["p_n"]  if st["p_n"]   else None
            st_gap = (st_wr - st_avg)          if st_avg is not None else None

            print(f"{'─'*70}")
            label = f"{sv}  v={ver}"
            print(f"  {label}")
            if st["total"] >= min_trades:
                gap_str = f"{st_gap:+.0%}" if st_gap is not None else "n/a"
                verdict = _gap_verdict(st_gap) if st_gap is not None else ""
                print(
                    f"  OVERALL  {st['total']:>4} trades | "
                    f"WR={st_wr:.0%} {_tier_label(st_wr):<20} | "
                    f"avg_model={st_avg:.0%} | gap={gap_str} {verdict}"
                    if st_avg is not None else
                    f"  OVERALL  {st['total']:>4} trades | WR={st_wr:.0%} {_tier_label(st_wr)}"
                )
            print()
            current_sv = sv
            current_ver = ver

        wr      = g["wins"] / g["total"]
        avg_p   = g["p_sum"] / g["p_n"]  if g["p_n"]   else None
        gap     = (wr - avg_p)            if avg_p is not None else None
        avg_ev  = g["ev_sum"] / g["ev_n"] if g["ev_n"]  else None
        ts_rng  = f"{g['first_ts'][:10]} -> {g['last_ts'][:10]}" if g["first_ts"] else ""

        parts = [
            f"    {asset:<4} {side:<3}  {g['total']:>4} trades",
            f"WR={wr:.0%}",
            f"model={avg_p:.0%}" if avg_p is not None else "model=n/a",
            f"gap={gap:+.0%} {_gap_verdict(gap)}" if gap is not None else "",
            f"avg_ev={avg_ev:+.2f}" if avg_ev is not None else "",
            ts_rng,
        ]
        print("  |  ".join(p for p in parts if p))

    print(f"\n{'='*70}")
    print("  LEGEND")
    print("    gap = actual_WR - avg_model_prob")
    print("    gap < -15%  -> model overconfident (reduce prob_scale or raise EV threshold)")
    print("    gap > +15%  -> model underconfident (room to be more aggressive)")
    print("    gap in +/-15% -> calibrated")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Strategy feedback report")
    parser.add_argument("--db", default="", help="Path to trades.db")
    parser.add_argument("--min-trades", type=int, default=5,
                        help="Minimum trades to show a row (default 5)")
    args = parser.parse_args()

    db = args.db or _find_db()
    if not db:
        print("ERROR: could not find trades.db. Pass --db path/to/trades.db", file=sys.stderr)
        sys.exit(1)

    run(db, args.min_trades)
