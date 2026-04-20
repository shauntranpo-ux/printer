"""
Edge diagnostic: breaks down trade outcomes across the dimensions that matter.

Run: py scripts/analyze_edge.py
     py scripts/analyze_edge.py --db kalshi_bot.db --mode live

Dimensions checked:
  - Win/loss dollar asymmetry (structural PnL driver)
  - By contract price bucket (cheap vs mid vs expensive)
  - By side (YES vs NO)
  - By claimed EV tier
  - By confidence bucket
  - By time at entry
  - By asset
"""

import argparse
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
DB_DEFAULT = REPO_ROOT / "kalshi_bot.db"


def pct(n, d):
    return f"{n/d*100:.1f}%" if d else "  n/a"


def signed(v):
    if v is None:
        return "   n/a"
    return f"+${v:.2f}" if v >= 0 else f"-${abs(v):.2f}"


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def row(label, n, wins, pnl, avg_stake=None, avg_win=None, avg_loss=None):
    wr = pct(wins, n)
    pnl_s = signed(pnl)
    stake_s = f"${avg_stake:.2f}" if avg_stake else "     "
    extra = ""
    if avg_win is not None and avg_loss is not None:
        extra = f"  win={signed(avg_win)} loss={signed(avg_loss)}"
    print(f"  {label:<22}  n={n:>4}  wr={wr:>6}  pnl={pnl_s:>9}  stake={stake_s}{extra}")


def run(db_path: str, mode_filter: str | None):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    mode_clause = ""
    params = []
    if mode_filter:
        mode_clause = "AND mode = ?"
        params.append(mode_filter)

    base = f"FROM trades WHERE outcome IN ('win','loss') {mode_clause}"

    def q(sql, p=None):
        return conn.execute(sql, (p or []) + params).fetchall()

    # ── Summary ──────────────────────────────────────────────────────────────
    section("SUMMARY")
    rows = q(f"""
        SELECT COUNT(*) n,
               SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) wins,
               SUM(pnl_dollars) pnl,
               AVG(CASE WHEN outcome='win' THEN pnl_dollars END) avg_win,
               AVG(CASE WHEN outcome='loss' THEN pnl_dollars END) avg_loss,
               AVG(trade_amount_dollars) avg_stake
        {base}
    """)
    r = rows[0]
    if r["n"] == 0:
        print("  No completed trades found.")
        return
    row("ALL TRADES", r["n"], r["wins"], r["pnl"],
        r["avg_stake"], r["avg_win"], r["avg_loss"])

    breakeven_wr = None
    if r["avg_win"] and r["avg_loss"] and r["avg_loss"] < 0:
        breakeven_wr = abs(r["avg_loss"]) / (abs(r["avg_loss"]) + r["avg_win"])
        print(f"\n  Win/loss ratio:  avg win {signed(r['avg_win'])}  avg loss {signed(r['avg_loss'])}")
        print(f"  Break-even WR:   {breakeven_wr*100:.1f}%  (actual: {pct(r['wins'], r['n'])})")
        edge = (r["wins"] / r["n"]) - breakeven_wr
        print(f"  Edge over break-even: {edge*100:+.1f}pp")

    # ── By asset ─────────────────────────────────────────────────────────────
    section("BY ASSET")
    for r in q(f"""
        SELECT asset,
               COUNT(*) n,
               SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) wins,
               SUM(pnl_dollars) pnl,
               AVG(trade_amount_dollars) avg_stake
        {base}
        GROUP BY asset ORDER BY SUM(pnl_dollars) DESC
    """):
        row(r["asset"] or "?", r["n"], r["wins"], r["pnl"], r["avg_stake"])

    # ── By side ──────────────────────────────────────────────────────────────
    section("BY SIDE")
    for r in q(f"""
        SELECT side,
               COUNT(*) n,
               SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) wins,
               SUM(pnl_dollars) pnl,
               AVG(CASE WHEN outcome='win' THEN pnl_dollars END) avg_win,
               AVG(CASE WHEN outcome='loss' THEN pnl_dollars END) avg_loss,
               AVG(trade_amount_dollars) avg_stake
        {base}
        GROUP BY side ORDER BY side
    """):
        row(r["side"] or "?", r["n"], r["wins"], r["pnl"],
            r["avg_stake"], r["avg_win"], r["avg_loss"])

    # ── By contract price ────────────────────────────────────────────────────
    section("BY CONTRACT PRICE (entry_price_cents)")
    for r in q(f"""
        SELECT CASE
                 WHEN entry_price_cents <  20 THEN '< 20c  (deep OTM)'
                 WHEN entry_price_cents <  35 THEN '20-35c'
                 WHEN entry_price_cents <  50 THEN '35-50c'
                 WHEN entry_price_cents <  65 THEN '50-65c'
                 WHEN entry_price_cents <  80 THEN '65-80c'
                 ELSE                              '80c+   (deep ITM)'
               END bucket,
               MIN(entry_price_cents) _sort,
               COUNT(*) n,
               SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) wins,
               SUM(pnl_dollars) pnl,
               AVG(CASE WHEN outcome='win' THEN pnl_dollars END) avg_win,
               AVG(CASE WHEN outcome='loss' THEN pnl_dollars END) avg_loss
        {base}
        GROUP BY bucket ORDER BY _sort
    """):
        row(r["bucket"], r["n"], r["wins"], r["pnl"],
            avg_win=r["avg_win"], avg_loss=r["avg_loss"])

    # ── By confidence ────────────────────────────────────────────────────────
    section("BY CONFIDENCE SCORE")
    for r in q(f"""
        SELECT CASE
                 WHEN confidence_score IS NULL THEN 'no score'
                 WHEN confidence_score < 70    THEN '< 70'
                 WHEN confidence_score < 76    THEN '70-75'
                 WHEN confidence_score < 80    THEN '76-79'
                 WHEN confidence_score < 85    THEN '80-84'
                 WHEN confidence_score < 90    THEN '85-89'
                 ELSE                               '90+'
               END bucket,
               COALESCE(confidence_score, -1) _sort,
               COUNT(*) n,
               SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) wins,
               SUM(pnl_dollars) pnl,
               AVG(trade_amount_dollars) avg_stake
        {base}
        GROUP BY bucket ORDER BY _sort
    """):
        row(r["bucket"], r["n"], r["wins"], r["pnl"], r["avg_stake"])

    # ── By model_prob vs implied_prob gap ────────────────────────────────────
    section("BY MODEL vs MARKET GAP  (model_prob - implied_prob)")
    for r in q(f"""
        SELECT CASE
                 WHEN (model_prob - implied_prob) IS NULL  THEN 'no data'
                 WHEN (model_prob - implied_prob) < -0.10  THEN '< -10pp  (model bearish vs mkt)'
                 WHEN (model_prob - implied_prob) < -0.05  THEN '-10 to -5pp'
                 WHEN (model_prob - implied_prob) < 0.00   THEN '-5 to 0pp'
                 WHEN (model_prob - implied_prob) < 0.05   THEN '0 to +5pp'
                 WHEN (model_prob - implied_prob) < 0.10   THEN '+5 to +10pp'
                 ELSE                                           '> +10pp  (model bullish vs mkt)'
               END bucket,
               COALESCE(model_prob - implied_prob, -99) _sort,
               COUNT(*) n,
               SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) wins,
               SUM(pnl_dollars) pnl
        {base}
        GROUP BY bucket ORDER BY _sort
    """):
        row(r["bucket"], r["n"], r["wins"], r["pnl"])

    # ── By time at entry ─────────────────────────────────────────────────────
    section("BY TIME AT ENTRY  (seconds_left)")
    for r in q(f"""
        SELECT CASE
                 WHEN seconds_left_at_entry IS NULL THEN 'unknown'
                 WHEN seconds_left_at_entry <  60   THEN '< 1min'
                 WHEN seconds_left_at_entry <  180  THEN '1-3min'
                 WHEN seconds_left_at_entry <  300  THEN '3-5min'
                 WHEN seconds_left_at_entry <  600  THEN '5-10min'
                 ELSE                                    '10min+'
               END bucket,
               COALESCE(seconds_left_at_entry, -1) _sort,
               COUNT(*) n,
               SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) wins,
               SUM(pnl_dollars) pnl
        {base}
        GROUP BY bucket ORDER BY _sort
    """):
        row(r["bucket"], r["n"], r["wins"], r["pnl"])

    # ── Bad dimensions summary ────────────────────────────────────────────────
    section("STRUCTURAL WARNINGS")
    warnings = []

    # Check NO side WR
    no_data = q(f"""
        SELECT COUNT(*) n, SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) wins, SUM(pnl_dollars) pnl
        {base} AND side='no'
    """)[0]
    if no_data["n"] >= 3 and (no_data["wins"] or 0) / no_data["n"] < 0.45:
        warnings.append(
            f"  WARN: NO side: {pct(no_data['wins'], no_data['n'])} WR on {no_data['n']} trades "
            f"({signed(no_data['pnl'])}) - below breakeven"
        )

    # Check cheap contract WR
    cheap = q(f"""
        SELECT COUNT(*) n, SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) wins, SUM(pnl_dollars) pnl
        {base} AND entry_price_cents < 35
    """)[0]
    if cheap["n"] >= 3:
        wr = (cheap["wins"] or 0) / cheap["n"]
        if wr < 0.35:
            warnings.append(
                f"  WARN: <35c contracts: {pct(cheap['wins'], cheap['n'])} WR on {cheap['n']} trades "
                f"({signed(cheap['pnl'])}) - model wrong on cheap contracts"
            )

    # Check late entry WR
    late = q(f"""
        SELECT COUNT(*) n, SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) wins, SUM(pnl_dollars) pnl
        {base} AND seconds_left_at_entry < 180
    """)[0]
    if late["n"] >= 3:
        wr = (late["wins"] or 0) / late["n"]
        if wr < 0.45:
            warnings.append(
                f"  WARN: <3min entries: {pct(late['wins'], late['n'])} WR on {late['n']} trades "
                f"({signed(late['pnl'])}) — too late to trade"
            )

    # Check win/loss dollar ratio
    wl = q(f"""
        SELECT AVG(CASE WHEN outcome='win' THEN pnl_dollars END) avg_win,
               AVG(CASE WHEN outcome='loss' THEN pnl_dollars END) avg_loss
        {base}
    """)[0]
    if wl["avg_win"] and wl["avg_loss"] and wl["avg_loss"] < 0:
        ratio = wl["avg_win"] / abs(wl["avg_loss"])
        if ratio < 0.8:
            warnings.append(
                f"  WARN: Win/loss ratio {ratio:.2f}x — losses bigger than wins "
                f"(win={signed(wl['avg_win'])} loss={signed(wl['avg_loss'])})"
            )

    if warnings:
        for w in warnings:
            print(w)
    else:
        print("  No structural warnings detected.")

    conn.close()
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze edge structure across trade dimensions")
    parser.add_argument("--db",   default=str(DB_DEFAULT), help="Path to kalshi_bot.db")
    parser.add_argument("--mode", default=None,            help="Filter by mode (live, paper)")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"DB not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    run(args.db, args.mode)
