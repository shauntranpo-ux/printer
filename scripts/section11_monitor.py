"""
Section 11 paper-trade monitor.

Pulls trades from kalshi_paper_validation.db (copy from Railway or
tunnel via volume mount), produces per-asset running summary.

Usage:
    python scripts\\section11_monitor.py
    python scripts\\section11_monitor.py --db path/to/kalshi_paper_validation.db
    python scripts\\section11_monitor.py --since 2026-04-20
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone


def summarize(db_path: str, since_ts: float = None):
    if not os.path.exists(db_path):
        print(f"DB not found: {db_path}")
        print("To copy from Railway, use `railway run` or download the volume.")
        sys.exit(1)

    con = sqlite3.connect(db_path)

    # Discover schema
    cur = con.execute("PRAGMA table_info(trades)")
    cols = [r[1] for r in cur.fetchall()]
    print(f"DB columns: {cols}\n")

    query = """
      SELECT asset, side, entry_price_cents, model_prob, outcome,
             pnl_dollars, strike, btc_price_at_entry, ts
      FROM trades
      WHERE outcome IN ('win', 'loss')
    """
    params = []
    if since_ts is not None:
        query += " AND ts >= ?"
        params.append(since_ts)
    query += " ORDER BY ts"

    rows = con.execute(query, params).fetchall()
    con.close()

    if not rows:
        print("No resolved trades yet.")
        return

    by_asset = {}
    for (asset, side, entry, model_prob, outcome, pnl, strike,
         btc_at_entry, ts) in rows:
        by_asset.setdefault(asset, []).append({
            "side": side,
            "entry": entry,
            "model_prob": model_prob,
            "outcome": outcome,
            "pnl": pnl or 0.0,
            "strike": strike,
            "btc_at_entry": btc_at_entry,
            "ts": ts,
        })

    total_trades = 0
    total_pnl = 0.0
    print(f"{'ASSET':<6} {'TRADES':>7} {'WIN%':>7} {'AVG_PNL':>9} "
          f"{'TOTAL_PNL':>10} {'YES_HIT':>8} {'NO_HIT':>8} "
          f"{'MODEL_AVG':>10} {'CAL_GAP':>9}")
    print("-" * 90)

    for asset in sorted(by_asset.keys()):
        trades = by_asset[asset]
        n = len(trades)
        wins = sum(1 for t in trades if t["outcome"] == "win")
        pnl_sum = sum(t["pnl"] for t in trades)
        hit = wins / n if n else 0.0
        avg = pnl_sum / n if n else 0.0

        yes_trades = [t for t in trades if t["side"] == "yes"]
        no_trades = [t for t in trades if t["side"] == "no"]
        yes_hit = (
            sum(1 for t in yes_trades if t["outcome"] == "win") / len(yes_trades)
            if yes_trades else 0.0
        )
        no_hit = (
            sum(1 for t in no_trades if t["outcome"] == "win") / len(no_trades)
            if no_trades else 0.0
        )

        valid_probs = [t for t in trades if t["model_prob"] is not None]
        if valid_probs:
            model_avg = sum(t["model_prob"] for t in valid_probs) / len(valid_probs)
            # Calibration gap: avg model prob should match actual win rate.
            # Simplified: avg_model_prob - actual_win_rate across all trades.
            cal_gap = model_avg - hit
        else:
            model_avg = 0.0
            cal_gap = 0.0

        total_trades += n
        total_pnl += pnl_sum

        print(f"{asset:<6} {n:>7d} {hit*100:>6.1f}% "
              f"${avg:>+8.3f} ${pnl_sum:>+9.2f} "
              f"{yes_hit*100:>7.1f}% {no_hit*100:>7.1f}% "
              f"{model_avg*100:>9.1f}% {cal_gap*100:>+8.1f}pp")

    print("-" * 90)
    print(f"{'TOTAL':<6} {total_trades:>7d} {'':>7} {'':>9} "
          f"${total_pnl:>+9.2f}")

    # Recent 10 trades
    print("\nMost recent 10 resolved trades:")
    all_flat = [
        (t["ts"], asset, t) for asset, trades in by_asset.items()
        for t in trades
    ]
    all_flat.sort(key=lambda x: x[0], reverse=True)
    for ts, asset, t in all_flat[:10]:
        try:
            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            dt_str = dt.strftime('%Y-%m-%d %H:%M')
        except Exception:
            dt_str = str(ts)[:16]
        prob_str = f"{t['model_prob']*100:5.1f}%" if t['model_prob'] is not None else "  n/a "
        print(f"  {dt_str} {asset:<5} "
              f"{t['side'].upper():<3} @{t['entry']:.0f}c "
              f"p={prob_str} -> {t['outcome']} "
              f"${t['pnl']:+.2f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="kalshi_paper_validation.db")
    parser.add_argument("--since", default=None, help="YYYY-MM-DD")
    args = parser.parse_args()

    since_ts = None
    if args.since:
        since_ts = datetime.fromisoformat(args.since).replace(
            tzinfo=timezone.utc
        ).timestamp()

    summarize(args.db, since_ts)


if __name__ == "__main__":
    main()
