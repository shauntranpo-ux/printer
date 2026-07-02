"""
scripts/edge_report.py - does the bot have a real edge?

Reads the `decision_log` table (every brain evaluation, with settlement outcome backfilled)
and reports, per (strategy, asset):
  - calibration: mean model P(win) vs realized win rate, and the market's vs realized
  - Brier scores (lower = better calibrated) for model and market
  - net-of-fee edge in $/contract for the gate's PICKS (would_trade=1), with a Wilson
    lower bound - the honest "is it +EV?" number
  - a rank-AUC of model probability vs outcome (>0.5 = some predictive signal)
  - the SKIPPED set (would_trade=0): are we rejecting winners?

This is survivorship-bias-free because decision_log records skipped decisions too, and the
periodic backfill settles them. Decision criterion (GATE 1): a strategy/asset has a proven
edge only if the PICKS' net-$/contract Wilson lower bound is clearly > 0.

Usage:
    python scripts/edge_report.py [path_to.db]
    BOT_DB_FILE=/app/data/kalshi_bot.db python scripts/edge_report.py
"""
import math
import os
import sqlite3
import sys

# Make the repo-root top-level modules importable whether run as a script or imported
# by the server (`from scripts.edge_report import ...`).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sessions  # noqa: E402  (ET session / day-type taxonomy)

# Below this many settled picks a time bucket is flagged "insufficient" - with slicing by
# session and day-type the per-bucket counts get thin fast; do not read edge into noise.
MIN_BUCKET_N = 50


def wilson_lower(wins: int, n: int, z: float = 1.645) -> float:
    """95% one-sided Wilson CI lower bound on a win rate."""
    if n == 0:
        return 0.0
    p = wins / n
    num = p + z * z / (2 * n) - z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    den = 1 + z * z / n
    return num / den


def _kalshi_fee(price_frac: float, rate: float = 0.07) -> float:
    p = max(0.0, min(1.0, price_frac))
    return rate * p * (1.0 - p)


def _auc(pairs: list) -> float:
    """Rank AUC of score vs binary label. pairs = [(score, label0/1)]. 0.5 = no signal."""
    pos = [s for s, y in pairs if y == 1]
    neg = [s for s, y in pairs if y == 0]
    if not pos or not neg:
        return float("nan")
    # Mann-Whitney U via rank sum
    ordered = sorted(pairs, key=lambda t: t[0])
    ranks = {}
    i = 0
    n = len(ordered)
    while i < n:
        j = i
        while j < n and ordered[j][0] == ordered[i][0]:
            j += 1
        avg_rank = (i + 1 + j) / 2.0  # 1-based average rank for ties
        for k in range(i, j):
            ranks[k] = avg_rank
        i = j
    rank_sum_pos = 0.0
    for idx, (_, y) in enumerate(ordered):
        if y == 1:
            rank_sum_pos += ranks[idx]
    n_pos, n_neg = len(pos), len(neg)
    u = rank_sum_pos - n_pos * (n_pos + 1) / 2.0
    return u / (n_pos * n_neg)


def _win(side: str, outcome: str) -> int:
    return 1 if ((side == "yes" and outcome == "yes") or (side == "no" and outcome == "no")) else 0


def _p_side(model_p_yes: float, side: str) -> float:
    return model_p_yes if side == "yes" else 1.0 - model_p_yes


def _pnl_stats(subset: list) -> dict:
    """Net-$/contract (after fee) + Wilson-LB net-$ for a set of settled decisions."""
    n = len(subset)
    wins = sum(_win(r["side"], r["outcome"]) for r in subset)
    pnls, entries = [], []
    for r in subset:
        if r["entry_price_cents"] is None:
            continue
        entry = r["entry_price_cents"] / 100.0
        won = _win(r["side"], r["outcome"])
        pnls.append((1.0 - entry if won else -entry) - _kalshi_fee(entry))
        entries.append(entry)
    mean_pnl = sum(pnls) / len(pnls) if pnls else None
    mean_entry = sum(entries) / len(entries) if entries else None
    wlb = wilson_lower(wins, n) if n else 0.0
    wlb_pnl = (wlb * (1.0 - mean_entry) - (1.0 - wlb) * mean_entry - _kalshi_fee(mean_entry)
               ) if mean_entry is not None else None
    return {"n": n, "win_rate": (wins / n if n else 0.0),
            "net_pnl": mean_pnl, "wlb_pnl": wlb_pnl}


def bucket_picks(picks: list, bucketer) -> dict:
    """Group settled PICKS by a time bucket (bucketer(ts_iso) -> label|None) -> per-bucket stats."""
    buckets: dict = {}
    for r in picks:
        key = bucketer(r["ts"])
        if key is None:
            continue
        buckets.setdefault(key, []).append(r)
    return {k: _pnl_stats(v) for k, v in buckets.items()}


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
        rows = conn.execute(
            "SELECT ts, strategy, asset, side, model_p_yes, market_mid_p_yes, "
            "entry_price_cents, would_trade, outcome FROM decision_log "
            "WHERE outcome IN ('yes','no') AND side IS NOT NULL AND model_p_yes IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        print(f"decision_log not available ({exc}). Run the bot to populate it.")
        sys.exit(1)
    conn.close()

    if not rows:
        print("No settled decisions yet. Let the bot run in paper for a while, then re-run.")
        return

    # group by (strategy, asset)
    groups: dict = {}
    for r in rows:
        groups.setdefault((r["strategy"], r["asset"]), []).append(r)

    print(f"Edge report - {len(rows)} settled decisions from {path}\n")
    print(f"{'strat/asset':<22} {'set':<6} {'n':>5} {'winR':>6} {'mdlP':>6} {'mktP':>6} "
          f"{'brierM':>7} {'brierK':>7} {'AUC':>5} {'$/ct':>7} {'$WilLB':>7}")
    print("-" * 96)

    overall_picks = []
    for (strat, asset), rs in sorted(groups.items()):
        for label, subset in (("PICKS", [r for r in rs if r["would_trade"]]),
                              ("SKIP", [r for r in rs if not r["would_trade"]]),
                              ("ALL", rs)):
            n = len(subset)
            if n == 0:
                continue
            wins = sum(_win(r["side"], r["outcome"]) for r in subset)
            win_rate = wins / n
            mean_model = sum(_p_side(r["model_p_yes"], r["side"]) for r in subset) / n
            mkt_vals = [r["market_mid_p_yes"] for r in subset if r["market_mid_p_yes"] is not None]
            mean_mkt = (sum(_p_side(r["market_mid_p_yes"], r["side"]) for r in subset
                            if r["market_mid_p_yes"] is not None) / len(mkt_vals)) if mkt_vals else None
            brier_m = sum((_p_side(r["model_p_yes"], r["side"]) - _win(r["side"], r["outcome"])) ** 2
                          for r in subset) / n
            brier_k = (sum((_p_side(r["market_mid_p_yes"], r["side"]) - _win(r["side"], r["outcome"])) ** 2
                           for r in subset if r["market_mid_p_yes"] is not None) / len(mkt_vals)) if mkt_vals else None
            auc = _auc([(_p_side(r["model_p_yes"], r["side"]), _win(r["side"], r["outcome"])) for r in subset])

            # net $/contract after fee for these would-be trades
            pnls = []
            for r in subset:
                if r["entry_price_cents"] is None:
                    continue
                entry = r["entry_price_cents"] / 100.0
                won = _win(r["side"], r["outcome"])
                pnl = (1.0 - entry) if won else (-entry)
                pnls.append(pnl - _kalshi_fee(entry))
            mean_pnl = sum(pnls) / len(pnls) if pnls else None
            # conservative net edge from the Wilson-LB win rate at the mean entry price
            mean_entry = (sum(r["entry_price_cents"] for r in subset if r["entry_price_cents"] is not None)
                          / max(1, len(pnls)) / 100.0) if pnls else None
            wlb = wilson_lower(wins, n)
            pnl_wlb = (wlb * (1.0 - mean_entry) - (1.0 - wlb) * mean_entry - _kalshi_fee(mean_entry)
                       ) if mean_entry is not None else None

            print(f"{strat[:8]+'/'+asset:<22} {label:<6} {n:>5} {win_rate:>6.3f} {mean_model:>6.3f} "
                  f"{_fmt(mean_mkt,3):>6} {_fmt(brier_m,4):>7} {_fmt(brier_k,4):>7} {_fmt(auc,3):>5} "
                  f"{_fmt(mean_pnl,4):>7} {_fmt(pnl_wlb,4):>7}")
            if label == "PICKS":
                overall_picks.extend(subset)
        print()

    # Per-time breakdowns over the gate's picks: which ET sessions / day-types actually pay.
    if overall_picks:
        for title, bucketer, order in (
            ("Edge by ET session", sessions.session_for_iso, sessions.ET_SESSION_ORDER),
            ("Edge by day type", sessions.day_type_for_iso, ["weekday", "weekend"]),
        ):
            stats = bucket_picks(overall_picks, bucketer)
            if not stats:
                continue
            print(f"- {title} (PICKS) -")
            print(f"{'bucket':<12} {'n':>5} {'winR':>6} {'$/ct':>7} {'$WilLB':>7}  note")
            for key in [k for k in order if k in stats] + [k for k in sorted(stats) if k not in order]:
                st = stats[key]
                note = "" if st["n"] >= MIN_BUCKET_N else f"insufficient (n<{MIN_BUCKET_N})"
                print(f"{key:<12} {st['n']:>5} {st['win_rate']:>6.3f} "
                      f"{_fmt(st['net_pnl'],4):>7} {_fmt(st['wlb_pnl'],4):>7}  {note}")
            print()

    # GATE-1 verdict on the gate's picks overall
    if overall_picks:
        n = len(overall_picks)
        wins = sum(_win(r["side"], r["outcome"]) for r in overall_picks)
        pnls = []
        for r in overall_picks:
            if r["entry_price_cents"] is None:
                continue
            entry = r["entry_price_cents"] / 100.0
            won = _win(r["side"], r["outcome"])
            pnls.append((1.0 - entry if won else -entry) - _kalshi_fee(entry))
        mean_pnl = sum(pnls) / len(pnls) if pnls else 0.0
        print("=" * 96)
        print(f"GATE-1 VERDICT (all PICKS): n={n}, win_rate={wins/n:.3f}, "
              f"net ${mean_pnl:.4f}/contract")
        if n < 200:
            print(f"  -> INSUFFICIENT DATA ({n}<200 picks). Keep collecting paper decisions.")
        elif mean_pnl > 0:
            print("  -> Net positive on picks. Confirm out-of-sample (GATE 2) before sizing.")
        else:
            print("  -> Net NEGATIVE/flat on picks. NO proven edge - do NOT size up; revise the signal.")


if __name__ == "__main__":
    main()
