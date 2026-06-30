"""
scripts/maker_report.py — is maker execution worth the (invasive) build?

Reads the `maker_log` counterfactual table (written at settlement by
bot_loops._record_maker_counterfactual) and compares, per strategy/asset:
  - maker fill rate (how often a passive 1c-inside-the-ask order would have filled)
  - TAKER net $/contract = what we actually realized (pay the ask + 7% fee)
  - MAKER-STRATEGY net $/contract = maker_pnl when filled, else 0 (an unfilled maker order
    means we simply DON'T trade that window — so unfilled = $0, not a loss). This already
    bakes in adverse selection: fills are settled with the real outcome.
  - delta = maker_strategy - taker, with a rough standard error.

Decision: build maker execution (step 3B) only if the delta is clearly positive across a
meaningful sample. A positive fill rate alone is NOT enough — unfilled-as-skip can help or
hurt depending on whether the skipped trades were winners or losers.

Usage:
    python scripts/maker_report.py [path_to.db]
    BOT_DB_FILE=/app/data/kalshi_bot.db python scripts/maker_report.py
"""
import math
import os
import sqlite3
import sys


def _stats(rows):
    n = len(rows)
    if n == 0:
        return None
    filled = sum(1 for r in rows if r["filled"])
    # Build aligned per-row (taker, maker-strategy) pairs in a SINGLE pass so the paired-diff
    # SE is never computed on misaligned lists (taker_pnl can be NULL on a malformed row).
    # maker strategy: realized maker_pnl when filled, else 0 (an unfilled order = no trade).
    pairs, maker_filled = [], []
    for r in rows:
        ms = r["maker_pnl"] if (r["filled"] and r["maker_pnl"] is not None) else 0.0
        if r["taker_pnl"] is not None:
            pairs.append((r["taker_pnl"], ms))
        if r["filled"] and r["maker_pnl"] is not None:
            maker_filled.append(r["maker_pnl"])
    m = len(pairs)
    taker_mean = (sum(t for t, _ in pairs) / m) if m else 0.0
    ms_mean = (sum(s for _, s in pairs) / m) if m else 0.0
    delta = ms_mean - taker_mean

    def _se(xs, mean):
        if len(xs) < 2:
            return float("nan")
        var = sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)
        return math.sqrt(var / len(xs))

    return {
        "n": n, "fill_rate": filled / n,
        "taker_mean": taker_mean, "ms_mean": ms_mean, "delta": delta,
        "maker_filled_mean": (sum(maker_filled) / len(maker_filled)) if maker_filled else float("nan"),
        "delta_se": _se([s - t for t, s in pairs], delta) if m else float("nan"),
    }


def _fmt(x, nd=4):
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{nd}f}"


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("BOT_DB_FILE", "kalshi_bot.db")
    if not os.path.exists(path):
        print(f"DB not found: {path}")
        sys.exit(1)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM maker_log").fetchall()
    except sqlite3.OperationalError as exc:
        print(f"maker_log not available ({exc}). Run the bot to populate it.")
        sys.exit(1)
    conn.close()
    if not rows:
        print("No maker samples yet. Let the bot run in paper for a while, then re-run.")
        return

    groups = {}
    for r in rows:
        groups.setdefault((r["strategy"], r["asset"]), []).append(r)
    groups[("ALL", "ALL")] = list(rows)

    print(f"Maker-vs-taker counterfactual — {len(rows)} settled trades from {path}\n")
    print(f"{'strat/asset':<22} {'n':>5} {'fill%':>6} {'taker$':>8} {'maker$':>8} "
          f"{'delta$':>8} {'delta_se':>8} {'mkrFill$':>8}")
    print("-" * 82)
    for (strat, asset), rs in sorted(groups.items()):
        s = _stats(rs)
        if not s:
            continue
        print(f"{str(strat)[:8]+'/'+str(asset):<22} {s['n']:>5} {s['fill_rate']*100:>5.1f}% "
              f"{_fmt(s['taker_mean']):>8} {_fmt(s['ms_mean']):>8} {_fmt(s['delta']):>8} "
              f"{_fmt(s['delta_se']):>8} {_fmt(s['maker_filled_mean']):>8}")

    overall = _stats(list(rows))
    print("\n" + "=" * 82)
    print(f"VERDICT (ALL): n={overall['n']}, fill={overall['fill_rate']*100:.1f}%, "
          f"maker-vs-taker delta={overall['delta']:+.4f}/contract (se {_fmt(overall['delta_se'])})")
    if overall["n"] < 200:
        print(f"  → INSUFFICIENT DATA ({overall['n']}<200). Keep collecting before deciding on 3B.")
    elif not math.isnan(overall["delta_se"]) and overall["delta"] > 2 * overall["delta_se"] and overall["delta"] > 0:
        print("  → Maker looks clearly better than taker. Worth building step 3B (resting orders).")
    elif overall["delta"] <= 0:
        print("  → Maker NOT better than taker. Do NOT build 3B; stay taker.")
    else:
        print("  → Inconclusive (delta within ~2 SE of 0). Collect more data before building 3B.")


if __name__ == "__main__":
    main()
