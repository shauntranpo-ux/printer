#!/usr/bin/env python3
"""
weekly_report.py — Weekly trend report for the Kalshi multi-asset bot.

Reads all daily analysis files from the past 7 days and produces a Claude-powered
trend report, edge decay analysis, and high-confidence parameter changes.

Usage:
    python weekly_report.py               # report for current ISO week
    python weekly_report.py --week 2025-W15  # specific ISO week
    python weekly_report.py --force       # overwrite existing report
    python weekly_report.py --help        # show this help

Requires daily_analysis/*.json files (one per trading day).
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

DAILY_DIR  = Path("daily_analysis")
WEEKLY_DIR = Path("weekly_reports")

# ── Data loading ───────────────────────────────────────────────────────────────

def load_week_analyses(week_label: str) -> list:
    """Load all daily analysis files for a given ISO week (e.g. '2025-W15')."""
    try:
        year_s, week_s = week_label.split("-W")
        year, week = int(year_s), int(week_s)
        # fromisocalendar is correct for ISO 8601; strptime %W/%w is the non-ISO
        # "week of year" and gives wrong dates near year boundaries.
        # fromisocalendar also raises ValueError for invalid weeks (e.g. 2025-W53).
        monday = datetime.fromisocalendar(year, week, 1)
    except (ValueError, AttributeError):
        print(f"ERROR: Invalid week format '{week_label}'. Use YYYY-WNN (e.g. 2025-W15).")
        sys.exit(1)

    results = []
    for i in range(7):
        date_str = (monday + timedelta(days=i)).strftime("%Y-%m-%d")
        fp = DAILY_DIR / f"{date_str}.json"
        if fp.exists():
            try:
                data = json.loads(fp.read_text())
                data["_date"] = date_str
                results.append(data)
            except Exception as exc:
                print(f"  WARNING: could not read {fp}: {exc}")
    return results


def aggregate_weekly(analyses: list) -> dict:
    """Aggregate cross-day statistics from daily analysis dicts."""
    total_trades = sum(a.get("_local_stats", {}).get("total_trades", 0) for a in analyses)
    total_wins   = sum(a.get("_local_stats", {}).get("wins", 0) for a in analyses)
    total_losses = sum(a.get("_local_stats", {}).get("losses", 0) for a in analyses)
    total_pnl    = sum(a.get("_local_stats", {}).get("net_pnl", 0) for a in analyses)

    # Day-by-day trend
    daily_trend = []
    for a in analyses:
        stats = a.get("_local_stats", {})
        daily_trend.append({
            "date":      a["_date"],
            "trades":    stats.get("total_trades", 0),
            "win_rate":  stats.get("win_rate_pct", 0),
            "net_pnl":   stats.get("net_pnl", 0),
            "grade":     a.get("overall_grade", "N/A"),
        })

    # Per-asset — aggregate grades and keep_trading votes across days
    asset_weekly: dict = {}
    for a in analyses:
        for asset, info in a.get("per_asset_analysis", {}).items():
            rec = asset_weekly.setdefault(asset, {
                "grades": [], "keep_votes": [], "assessments": [],
            })
            rec["grades"].append(info.get("grade", "N/A"))
            rec["keep_votes"].append(bool(info.get("keep_trading", True)))
            rec["assessments"].append(f"{a['_date']}: {info.get('assessment', '')}")

    # Repeated parameter suggestions (3+ identical suggestions = high confidence)
    suggestion_tally: dict = {}
    all_suggestions = []
    for a in analyses:
        for s in a.get("parameter_suggestions", []):
            key = (s.get("field"), s.get("asset"), s.get("suggested_value"))
            suggestion_tally[key] = suggestion_tally.get(key, 0) + 1
            all_suggestions.append({**s, "_date": a["_date"]})

    repeated = []
    seen: set = set()
    for s in all_suggestions:
        key = (s.get("field"), s.get("asset"), s.get("suggested_value"))
        if key not in seen and suggestion_tally.get(key, 0) >= 3:
            seen.add(key)
            repeated.append({**s, "times_suggested": suggestion_tally[key]})

    return {
        "total_trades":        total_trades,
        "total_wins":          total_wins,
        "total_losses":        total_losses,
        "total_pnl":           round(total_pnl, 4),
        "overall_win_rate":    round(total_wins / total_trades * 100, 1) if total_trades else 0,
        "daily_trend":         daily_trend,
        "asset_weekly":        asset_weekly,
        "all_suggestions":     all_suggestions,
        "repeated_suggestions": repeated,
    }


# ── Helpers ────────────────────────────────────────────────────────────────────



def print_weekly_summary(report: dict, agg: dict) -> None:
    print("\n" + "=" * 62)
    print(f"  WEEKLY REPORT — {report.get('week', '?')}")
    print("=" * 62)
    print(
        f"  Grade: {report.get('overall_grade', '?')}  |  "
        f"Days: {report.get('days_analyzed', 0)}/7  |  "
        f"Trades: {agg['total_trades']}  |  "
        f"PnL: ${agg['total_pnl']:.2f}"
    )
    print(
        f"  Win rate: {agg['overall_win_rate']:.1f}%  |  "
        f"Trend: {report.get('win_rate_trend', '?')}"
    )
    print(f"\n  {report.get('overall_assessment', '')}")

    per_asset = report.get("weekly_pnl_per_asset", {})
    if per_asset:
        print("\n  PER-ASSET RECOMMENDATIONS:")
        for asset in ASSETS:
            info = per_asset.get(asset, {})
            if not info or info.get("trades", 0) == 0:
                continue
            rec = info.get("recommendation", "?")
            print(
                f"    {asset:4s}: {rec:12s} | trades={info.get('trades', 0):3d} | "
                f"wr={info.get('win_rate', 0):.1f}% | pnl=${info.get('pnl', 0):.2f}"
            )

    print("\n  DAY-BY-DAY:")
    for d in agg["daily_trend"]:
        pnl_sign = "+" if d["net_pnl"] >= 0 else ""
        print(
            f"    {d['date']}: {d.get('grade', '?'):2s} | "
            f"wr={d['win_rate']:.1f}% | pnl={pnl_sign}${d['net_pnl']:.2f}"
        )

    changes = report.get("high_confidence_changes", [])
    if changes:
        print(f"\n  HIGH-CONFIDENCE CHANGES ({len(changes)}):")
        for c in changes:
            scope = f" [{c['asset']}]" if c.get("asset") and c["asset"] != "ALL" else ""
            print(f"    {c['field']}{scope}: {c.get('current_value')} → {c.get('suggested_value')}")
            print(f"      {c.get('reason', '')}")

    decay = report.get("edge_decay_assessment", "")
    if decay:
        print(f"\n  EDGE DECAY: {decay}")

    patterns = report.get("patterns_across_week", [])
    if patterns:
        print("\n  WEEKLY PATTERNS:")
        for p in patterns:
            print(f"    - {p}")

    print(f"\n  NEXT WEEK: {report.get('next_week_strategy', '')}")
    print("=" * 62 + "\n")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Weekly aggregation report for the Kalshi trading bot.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python weekly_report.py               # current ISO week
  python weekly_report.py --week 2025-W15  # specific week
  python weekly_report.py --force       # overwrite existing report

Output:
  weekly_reports/YYYY-WNN.json
        """,
    )
    parser.add_argument("--week",  help="ISO week label (e.g. 2025-W15). Defaults to current week.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing report.")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    if args.week:
        week_label = args.week
    else:
        iso = now.isocalendar()
        week_label = f"{iso[0]}-W{iso[1]:02d}"

    WEEKLY_DIR.mkdir(exist_ok=True)
    out_file = WEEKLY_DIR / f"{week_label}.json"

    if out_file.exists() and not args.force:
        print(f"Weekly report for {week_label} already exists: {out_file}")
        print("Use --force to overwrite.")
        sys.exit(0)

    print(f"Generating weekly report for {week_label}...")

    analyses = load_week_analyses(week_label)
    if not analyses:
        print(f"No daily analysis files found for {week_label}.")
        sys.exit(0)

    print(f"Loaded {len(analyses)} daily analyses: {[a['_date'] for a in analyses]}")

    agg = aggregate_weekly(analyses)

    if agg["total_trades"] == 0:
        print("No completed trades recorded in any analysis for this week.")
        sys.exit(0)

    print(
        f"Week totals: {agg['total_trades']} trades, "
        f"{agg['overall_win_rate']:.1f}% WR, ${agg['total_pnl']:.2f} PnL"
    )

    report = {
        "week":             week_label,
        "days_analyzed":    len(analyses),
        "overall_grade":    "N/A",
        "overall_assessment": (
            f"{agg['total_trades']} trades over {len(analyses)} days | "
            f"WR={agg['overall_win_rate']:.1f}% | PnL=${agg['total_pnl']:.2f}"
        ),
        "_weekly_stats":    agg,
    }

    out_file.write_text(json.dumps(report, indent=2))
    print(f"Weekly report saved → {out_file}")

    print_weekly_summary(report, agg)


if __name__ == "__main__":
    main()
