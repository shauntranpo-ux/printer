"""
Reads kalshi_bot.db and reports BTC bidirectional trade stats:
  - Trades where continuation would have forced YES but actual = NO
  - Hit rate per side
  - PnL per side
  - Comparison to legacy expected behavior

Run after a paper-trade session:
    python scripts\\section3_bidirectional_report.py
"""

import os
import sqlite3
import sys
from datetime import datetime, timezone


DB = os.environ.get("BOT_DB_FILE", "kalshi_bot.db")


def main():
    if not os.path.exists(DB):
        print(f"No DB at {DB}")
        sys.exit(1)

    con = sqlite3.connect(DB)
    # Discover schema
    cur = con.execute("PRAGMA table_info(trades)")
    cols = [r[1] for r in cur.fetchall()]

    query = """
      SELECT side, outcome, pnl_dollars, btc_price_at_entry, strike,
             entry_price_cents, ts, asset
      FROM trades
      WHERE asset = 'BTC' AND outcome IN ('win', 'loss')
      ORDER BY ts DESC
    """
    rows = con.execute(query).fetchall()
    con.close()

    yes_trades = []
    no_trades = []
    flipped_trades = []  # side != naive continuation

    for side, outcome, pnl, btc, strike, entry, ts, asset in rows:
        if side == "yes":
            yes_trades.append((outcome, pnl))
        else:
            no_trades.append((outcome, pnl))

        if btc is not None and strike is not None:
            naive = "yes" if btc > strike else "no"
            if side != naive:
                flipped_trades.append((side, outcome, pnl, btc, strike, ts))

    def summarize(label, trades):
        if not trades:
            return f"{label}: n=0"
        n = len(trades)
        wins = sum(1 for o, _ in trades if o == "win")
        pnl = sum(p for _, p in trades if p is not None)
        hit = wins / n * 100 if n else 0
        return f"{label}: n={n} hit={hit:.1f}% pnl=${pnl:+.2f}"

    print("BTC bidirectional analysis")
    print("=" * 60)
    print(summarize("All YES", yes_trades))
    print(summarize("All NO ", no_trades))
    print(summarize("Flipped", [(o, p) for _, o, p, _, _, _ in flipped_trades]))
    print()
    print(f"Flipped-side trades ({len(flipped_trades)} total):")
    for side, outcome, pnl, btc, strike, ts in flipped_trades[:20]:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc) if isinstance(ts, (int, float)) else ts
        print(f"  {dt} {side.upper():3} btc={btc:.2f} strike={strike:.2f} "
              f"-> {outcome:5s} pnl=${pnl:+.2f}")


if __name__ == "__main__":
    main()
