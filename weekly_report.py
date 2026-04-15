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

Requires daily_analysis/*.json files created by claude_analyzer.py.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

DAILY_DIR  = Path("daily_analysis")
WEEKLY_DIR = Path("weekly_reports")
MODEL      = "claude-sonnet-4-20250514"
ASSETS     = ["BTC", "ETH", "SOL", "XRP", "DOGE"]


# ── Data loading ───────────────────────────────────────────────────────────────

def load_week_analyses(week_label: str) -> list:
    """Load all daily analysis files for a given ISO week (e.g. '2025-W15')."""
    try:
        year_s, week_s = week_label.split("-W")
        year, week = int(year_s), int(week_s)
    except (ValueError, AttributeError):
        print(f"ERROR: Invalid week format '{week_label}'. Use YYYY-WNN (e.g. 2025-W15).")
        sys.exit(1)

    # ISO week Monday
    monday = datetime.strptime(f"{year}-W{week:02d}-1", "%Y-W%W-%w")

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


# ── Prompt builder ─────────────────────────────────────────────────────────────

def build_weekly_prompt(week_label: str, analyses: list, agg: dict) -> str:
    n = len(analyses)

    # Day trend table
    trend_rows = []
    for d in agg["daily_trend"]:
        pnl_sign = "+" if d["net_pnl"] >= 0 else ""
        trend_rows.append(
            f"  {d['date']} | grade={d['grade']:2s} | trades={d['trades']:3d} | "
            f"wr={d['win_rate']:.1f}% | pnl={pnl_sign}${d['net_pnl']:.2f}"
        )
    trend_table = "\n".join(trend_rows) if trend_rows else "  (no data)"

    # Per-asset summary
    asset_rows = []
    for asset in ASSETS:
        info = agg["asset_weekly"].get(asset, {})
        if not info:
            continue
        keep_ok = sum(1 for v in info["keep_votes"] if v)
        total_v = len(info["keep_votes"])
        asset_rows.append(
            f"  {asset}: grades=[{', '.join(info['grades'])}] "
            f"keep_trading={keep_ok}/{total_v} days"
        )
    asset_summary = "\n".join(asset_rows) if asset_rows else "  (no asset data)"

    # Edge decay: first half vs second half of the week
    half = n // 2
    first_half  = agg["daily_trend"][:half] if half else []
    second_half = agg["daily_trend"][half:] if half else []

    def _avg_wr(days):
        wrs = [d["win_rate"] for d in days if d["trades"] > 0]
        return sum(wrs) / len(wrs) if wrs else 0.0

    def _pnl_per_trade(days):
        pnl = sum(d["net_pnl"] for d in days)
        trds = sum(d["trades"] for d in days)
        return pnl / trds if trds else 0.0

    if first_half and second_half:
        h1_wr, h2_wr   = _avg_wr(first_half), _avg_wr(second_half)
        h1_ppt, h2_ppt = _pnl_per_trade(first_half), _pnl_per_trade(second_half)
        ppt_delta = ((h1_ppt - h2_ppt) / abs(h1_ppt) * 100) if h1_ppt else 0
        edge_decay = (
            f"  First half:  WR={h1_wr:.1f}%  PnL/trade=${h1_ppt:.4f}\n"
            f"  Second half: WR={h2_wr:.1f}%  PnL/trade=${h2_ppt:.4f}\n"
            f"  PnL/trade change: {ppt_delta:+.1f}%"
        )
    else:
        edge_decay = "  (fewer than 4 days — edge decay analysis unavailable)"

    rep_str = (
        json.dumps(agg["repeated_suggestions"], indent=2)
        if agg["repeated_suggestions"]
        else "  (no suggestions repeated 3+ times this week)"
    )

    daily_summary_compact = json.dumps(
        [
            {
                "date":             a["_date"],
                "grade":            a.get("overall_grade"),
                "assessment":       a.get("overall_assessment"),
                "tomorrow_strategy": a.get("tomorrow_strategy"),
                "risk_warnings":    a.get("risk_warnings", []),
                "patterns":         a.get("patterns_detected", []),
            }
            for a in analyses
        ],
        indent=2,
    )

    return f"""You are a quantitative trading analyst reviewing a full week of trading for a Kalshi 15-minute binary options bot that trades BTC, ETH, SOL, XRP, and DOGE.

WEEK: {week_label}
DAYS WITH ANALYSIS DATA: {n}/7

WEEKLY TOTALS:
- Total trades: {agg['total_trades']}
- Wins: {agg['total_wins']} | Losses: {agg['total_losses']}
- Overall win rate: {agg['overall_win_rate']:.1f}%
- Total PnL: ${agg['total_pnl']:.2f}

DAY-BY-DAY TREND:
{trend_table}

PER-ASSET WEEKLY SUMMARY (grades and keep_trading votes per day):
{asset_summary}

EDGE DECAY ANALYSIS (first half of week vs. second half):
{edge_decay}

REPEATED PARAMETER SUGGESTIONS (confidence = high if suggested 3+ days):
{rep_str}

ALL DAILY SUMMARIES:
{daily_summary_compact}

Provide your weekly analysis in this EXACT JSON format:

{{
  "week": "{week_label}",
  "days_analyzed": {n},
  "overall_grade": "A/B/C/D/F",
  "overall_assessment": "2-3 sentence summary of the week",
  "weekly_pnl_per_asset": {{
    "BTC":  {{"trades": 0, "pnl": 0.0, "win_rate": 0.0, "recommendation": "CONTINUE/PAUSE/REDUCE_SIZE/STOP"}},
    "ETH":  {{"trades": 0, "pnl": 0.0, "win_rate": 0.0, "recommendation": "CONTINUE/PAUSE/REDUCE_SIZE/STOP"}},
    "SOL":  {{"trades": 0, "pnl": 0.0, "win_rate": 0.0, "recommendation": "CONTINUE/PAUSE/REDUCE_SIZE/STOP"}},
    "XRP":  {{"trades": 0, "pnl": 0.0, "win_rate": 0.0, "recommendation": "CONTINUE/PAUSE/REDUCE_SIZE/STOP"}},
    "DOGE": {{"trades": 0, "pnl": 0.0, "win_rate": 0.0, "recommendation": "CONTINUE/PAUSE/REDUCE_SIZE/STOP"}}
  }},
  "win_rate_trend": "IMPROVING/STABLE/DECLINING",
  "win_rate_trend_detail": "specific numbers from the daily data, e.g. Mon 58% → Fri 71%",
  "edge_decay_assessment": "NONE/MILD/MODERATE/SEVERE — include PnL/trade numbers",
  "high_confidence_changes": [
    {{
      "field": "config field",
      "asset": "ALL or specific asset",
      "current_value": 0,
      "suggested_value": 0,
      "reason": "suggested N times this week, pattern: ...",
      "times_suggested": 0
    }}
  ],
  "patterns_across_week": [
    "pattern with specific numbers, e.g. XRP loses 80% of trades when entry > 70c"
  ],
  "next_week_strategy": "2-3 sentences on what to change or maintain next week"
}}

Be specific. Use actual numbers from the daily data. Respond with ONLY the JSON. No preamble. No markdown fences."""


# ── Console output ─────────────────────────────────────────────────────────────

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
        description="Weekly trend report for the Kalshi trading bot.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python weekly_report.py               # current ISO week
  python weekly_report.py --week 2025-W15  # specific week
  python weekly_report.py --force       # overwrite existing report

Prerequisite:
  Run python claude_analyzer.py for each trading day first.

Output:
  weekly_reports/YYYY-WNN.json
        """,
    )
    parser.add_argument("--week",  help="ISO week label (e.g. 2025-W15). Defaults to current week.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing report.")
    args = parser.parse_args()

    if not _ANTHROPIC_AVAILABLE:
        print("ERROR: anthropic package not installed. Run: pip install anthropic")
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ERROR: Set ANTHROPIC_API_KEY environment variable to use the analyzer.")
        print("       Get your key from console.anthropic.com")
        sys.exit(1)

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
        print(f"Run: python claude_analyzer.py  (for each trading day)")
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
    print(f"Sending to Claude ({MODEL})...")

    prompt  = build_weekly_prompt(week_label, analyses, agg)
    client  = anthropic.Anthropic(api_key=api_key)
    report  = None
    raw     = ""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text
        text = raw.strip()
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else text
            if text.startswith("json"):
                text = text[4:]
        report = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        print(f"WARNING: Claude returned non-JSON: {exc}")
        report = {
            "week":               week_label,
            "overall_grade":      "N/A",
            "overall_assessment": f"Claude returned non-JSON. Error: {exc}",
            "claude_error":       str(exc),
            "claude_raw":         raw[:500],
        }
    except Exception as exc:
        print(f"WARNING: Claude API call failed: {exc}")
        report = {
            "week":               week_label,
            "overall_grade":      "N/A",
            "overall_assessment": f"Claude API error: {exc}",
            "claude_error":       str(exc),
        }

    report["_weekly_stats"] = agg
    out_file.write_text(json.dumps(report, indent=2))
    print(f"Weekly report saved → {out_file}")

    print_weekly_summary(report, agg)


if __name__ == "__main__":
    main()
