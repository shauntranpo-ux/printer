#!/usr/bin/env python3
"""
claude_analyzer.py — Daily post-session Claude analysis for the Kalshi multi-asset bot.

Runs OFFLINE after a trading day ends. Reads from kalshi_bot.db and
price_validation_log.csv (if present), sends a structured summary to Claude,
and saves recommendations to daily_analysis/ and suggested_config_changes.json.

Usage:
    python claude_analyzer.py               # analyze today's trades (UTC)
    python claude_analyzer.py --date 2025-04-15  # re-analyze a specific date
    python claude_analyzer.py --force       # overwrite existing analysis
    python claude_analyzer.py --help        # show this help

This script adds ZERO latency to live trading — it is never called from bot.py.
"""

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Anthropic ──────────────────────────────────────────────────────────────────
try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

# ── Paths ──────────────────────────────────────────────────────────────────────
DB_FILE        = os.environ.get("BOT_DB_FILE", "kalshi_bot.db")
CONFIG_FILE    = os.environ.get("BOT_CONFIG_FILE", "config.json")
PRICE_VAL_CSV  = "price_validation_log.csv"
DAILY_DIR      = Path("daily_analysis")
SUGGESTED_FILE = Path("suggested_config_changes.json")

MODEL      = "claude-sonnet-4-20250514"
KALSHI_FEE = 0.07  # per contract, probability units

DISTANCE_BUCKETS = [
    (0.00,  0.10, "<0.1%"),
    (0.10,  0.20, "0.1-0.2%"),
    (0.20,  0.50, "0.2-0.5%"),
    (0.50,  1.00, "0.5-1.0%"),
    (1.00, 999.0, ">1.0%"),
]

TIME_BUCKETS = [
    (0,   2,   "<2 min"),
    (2,   5,   "2-5 min"),
    (5,   10,  "5-10 min"),
    (10, 999,  ">10 min"),
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _bucket(value: float, buckets: list) -> str:
    for lo, hi, label in buckets:
        if lo <= value < hi:
            return label
    return buckets[-1][2]


def connect_db(retries: int = 3) -> sqlite3.Connection:
    for attempt in range(retries):
        try:
            conn = sqlite3.connect(DB_FILE, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            return conn
        except sqlite3.OperationalError as exc:
            if attempt < retries - 1:
                print(f"  DB locked, retrying ({attempt + 1}/{retries})...")
                time.sleep(2)
            else:
                raise exc


def pull_trades(date_str: str) -> list:
    """Pull all completed trades for the given UTC date from kalshi_bot.db."""
    conn = connect_db()
    try:
        cur = conn.execute(
            """
            SELECT id, ts, market_id, market_title, mode, side, contracts,
                   entry_price_cents, trade_amount_dollars, confidence_score,
                   model_prob, implied_prob, btc_price_at_entry, strike,
                   seconds_left_at_entry, exit_price_cents, exit_reason,
                   outcome, pnl_dollars, profit_percent,
                   claude_confidence, claude_signals, asset
            FROM trades
            WHERE outcome IN ('win', 'loss')
              AND ts LIKE ?
            ORDER BY ts
            """,
            (f"{date_str}%",),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def compute_ev(trade: dict):
    """EV at entry as a decimal fraction. Returns None if data missing."""
    model_prob = trade.get("model_prob")
    entry = trade.get("entry_price_cents")
    if model_prob is None or entry is None:
        return None
    return model_prob - entry / 100.0 - KALSHI_FEE


def compute_distance_pct(trade: dict):
    """Absolute distance from strike as % of asset price."""
    price = trade.get("btc_price_at_entry")
    strike = trade.get("strike")
    if price and strike and price > 0:
        return abs(price - strike) / price * 100.0
    return None


def group_by(trades: list, key_fn) -> dict:
    groups: dict = {}
    for t in trades:
        k = key_fn(t)
        if k is None:
            continue
        groups.setdefault(k, []).append(t)
    return groups


def group_stats(group: list) -> dict:
    wins   = sum(1 for t in group if t["outcome"] == "win")
    losses = sum(1 for t in group if t["outcome"] == "loss")
    total  = wins + losses
    evs    = [e for t in group if (e := compute_ev(t)) is not None]
    return {
        "count":           total,
        "wins":            wins,
        "losses":          losses,
        "win_rate":        round(wins / total * 100, 1) if total else 0,
        "avg_entry_price": round(sum(t["entry_price_cents"] or 0 for t in group) / total, 1) if total else 0,
        "avg_pnl":         round(sum(t["pnl_dollars"] or 0 for t in group) / total, 4) if total else 0,
        "total_pnl":       round(sum(t["pnl_dollars"] or 0 for t in group), 4),
        "avg_ev_pct":      round(sum(evs) / len(evs) * 100, 2) if evs else None,
    }


def format_table(groups: dict, columns: list) -> str:
    if not groups:
        return "(no data)"
    rows = []
    for key in sorted(groups.keys()):
        stats = groups[key]
        row = {columns[0]: key}
        row.update(stats)
        rows.append("  " + " | ".join(str(row.get(c, "N/A")) for c in columns))
    header = "  " + " | ".join(columns)
    sep    = "  " + "-" * (len(header) - 2)
    return "\n".join([header, sep] + rows)


def pull_price_validation(date_str: str) -> dict:
    """Parse price_validation_log.csv for today's samples."""
    if not os.path.exists(PRICE_VAL_CSV):
        return {}
    gaps = []
    try:
        with open(PRICE_VAL_CSV, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if not row.get("ts", "").startswith(date_str):
                    continue
                try:
                    real = float(row["real_yes_ask"])
                    sim  = float(row["sim_yes_ask"])
                    if real > 0 and sim > 0:
                        gaps.append((real - sim, row.get("asset", "BTC")))
                except (KeyError, ValueError):
                    pass
    except Exception:
        return {}

    if not gaps:
        return {}

    avg_gap   = sum(g[0] for g in gaps) / len(gaps)
    worst     = max(gaps, key=lambda x: abs(x[0]))
    real_high = sum(1 for g in gaps if g[0] > 0)
    by_asset  = {}
    for gap, asset in gaps:
        by_asset.setdefault(asset, []).append(gap)
    asset_gaps = {a: round(sum(gs) / len(gs) * 100, 2) for a, gs in by_asset.items()}

    return {
        "samples":              len(gaps),
        "avg_gap_cents":        round(avg_gap * 100, 2),
        "worst_gap_cents":      round(worst[0] * 100, 2),
        "worst_gap_asset":      worst[1],
        "pct_real_exceeds_sim": round(real_high / len(gaps) * 100, 1),
        "per_asset_avg_gap_cents": asset_gaps,
    }


def read_config() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _parse_claude_json(raw: str) -> dict:
    """Extract JSON from Claude's response, stripping markdown fences if present."""
    text = raw.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


# ── Prompt builder ─────────────────────────────────────────────────────────────

def build_prompt(date_str: str, trades: list, config: dict, price_val: dict) -> str:
    wins   = [t for t in trades if t["outcome"] == "win"]
    losses = [t for t in trades if t["outcome"] == "loss"]
    total  = len(trades)
    win_rate  = len(wins) / total * 100 if total else 0
    net_pnl   = sum(t["pnl_dollars"] or 0 for t in trades)
    total_fees = sum((t["contracts"] or 0) * 0.07 for t in trades)
    assets_traded = sorted(set(t.get("asset", "BTC") for t in trades))

    # Per-asset
    asset_groups = group_by(trades, lambda t: t.get("asset", "BTC"))
    asset_stats  = {a: group_stats(g) for a, g in asset_groups.items()}
    per_asset_table = format_table(
        asset_stats,
        ["asset", "count", "wins", "losses", "win_rate", "avg_entry_price", "avg_pnl", "total_pnl"],
    )

    # Distance from strike
    dist_groups = group_by(
        trades,
        lambda t: (
            _bucket(dp, DISTANCE_BUCKETS)
            if (dp := compute_distance_pct(t)) is not None else None
        ),
    )
    dist_stats = {k: group_stats(g) for k, g in dist_groups.items()}
    distance_table = format_table(
        dist_stats,
        ["distance_bucket", "count", "wins", "losses", "win_rate", "avg_entry_price", "avg_pnl", "avg_ev_pct"],
    )

    # Time remaining
    time_groups = group_by(
        trades,
        lambda t: (
            _bucket((t.get("seconds_left_at_entry") or 0) / 60, TIME_BUCKETS)
            if t.get("seconds_left_at_entry") is not None else None
        ),
    )
    time_stats = {k: group_stats(g) for k, g in time_groups.items()}
    time_table = format_table(
        time_stats,
        ["minutes_remaining_bucket", "count", "wins", "losses", "win_rate", "avg_entry_price", "avg_pnl"],
    )

    # Losing trades detail
    losing_detail = []
    for t in losses:
        dp = compute_distance_pct(t)
        ev = compute_ev(t)
        losing_detail.append({
            "asset":             t.get("asset", "BTC"),
            "ticker":            t.get("market_id", ""),
            "entry_price_cents": t.get("entry_price_cents"),
            "distance_pct":      round(dp, 3) if dp is not None else None,
            "mins_remaining":    round((t.get("seconds_left_at_entry") or 0) / 60, 1),
            "ev_at_entry_pct":   round(ev * 100, 2) if ev is not None else None,
            "model_prob_pct":    round((t.get("model_prob") or 0) * 100, 1),
            "pnl_dollars":       t.get("pnl_dollars"),
        })

    pv_text = json.dumps(price_val, indent=2) if price_val else (
        "(price_validation_log.csv not found or no samples for this date)"
    )

    config_summary = {
        k: config.get(k)
        for k in [
            "min_ev_base", "max_entry_price_cents", "min_reward_cents",
            "max_risk_reward_ratio", "vol_gate_thresh", "kelly_cap",
            "trade_amount_dollars", "enabled_assets", "asset_overrides",
        ]
        if k in config
    }

    low_sample_note = ""
    if total < 10:
        low_sample_note = (
            f"\n\nNOTE: Only {total} completed trades today — below the 10-trade minimum "
            f"for statistically significant conclusions. Mark all suggestions low-confidence "
            f"and recommend collecting more data before applying parameter changes."
        )

    return f"""You are a quantitative trading analyst reviewing today's trading session for a Kalshi 15-minute binary options bot that trades BTC, ETH, SOL, XRP, and DOGE.

Here is today's complete session data:

SESSION SUMMARY:
- Date: {date_str}
- Total trades: {total} | Wins: {len(wins)} | Losses: {len(losses)} | Win rate: {win_rate:.1f}%
- Net PnL (estimated after fees): ${net_pnl:.2f}
- Estimated fees paid: ${total_fees:.2f}
- Assets traded: {', '.join(assets_traded) if assets_traded else 'none'}
{low_sample_note}

PER-ASSET BREAKDOWN:
{per_asset_table}
(columns: asset, trades, wins, losses, win_rate%, avg_entry_price_cents, avg_pnl_$, total_pnl_$)

PERFORMANCE BY DISTANCE FROM STRIKE:
{distance_table}
(columns: distance_bucket, trades, wins, losses, win_rate%, avg_entry_cents, avg_pnl_$, avg_ev%)

PERFORMANCE BY TIME REMAINING:
{time_table}
(columns: minutes_remaining_bucket, trades, wins, losses, win_rate%, avg_entry_cents, avg_pnl_$)

PERFORMANCE BY VOL REGIME:
(vol_at_entry not stored in trades DB — vol regime analysis unavailable)

LOSING TRADES DETAIL:
{json.dumps(losing_detail, indent=2)}

PRICE VALIDATION (real Kalshi prices vs. simulated AMM prices):
{pv_text}

CURRENT CONFIG:
{json.dumps(config_summary, indent=2)}

Based on this data, provide your analysis in this EXACT JSON format:

{{
  "date": "{date_str}",
  "overall_grade": "A/B/C/D/F",
  "overall_assessment": "2-3 sentence summary of the day",
  "per_asset_analysis": {{
    "BTC":  {{"grade": "A-F", "assessment": "1-2 sentences", "keep_trading": true, "reason": "why"}},
    "ETH":  {{"grade": "A-F", "assessment": "1-2 sentences", "keep_trading": true, "reason": "why"}},
    "SOL":  {{"grade": "A-F", "assessment": "1-2 sentences", "keep_trading": true, "reason": "why"}},
    "XRP":  {{"grade": "A-F", "assessment": "1-2 sentences", "keep_trading": true, "reason": "why"}},
    "DOGE": {{"grade": "A-F", "assessment": "1-2 sentences", "keep_trading": true, "reason": "why"}}
  }},
  "worst_performers": [
    {{"description": "what went wrong", "category": "distance_bucket/time_bucket/asset", "suggestion": "specific fix"}}
  ],
  "parameter_suggestions": [
    {{
      "field": "config field name",
      "asset": "ALL or specific asset",
      "current_value": 0,
      "suggested_value": 0,
      "reason": "why, with numbers from today's data",
      "confidence": "high/medium/low",
      "data_points": 0
    }}
  ],
  "patterns_detected": [
    "Pattern 1: losses cluster in first 3 minutes — e.g. 8/10 losses when time_remaining < 2min"
  ],
  "risk_warnings": [
    "concern about drawdown, overexposure, deteriorating edge, etc."
  ],
  "tomorrow_strategy": "1-2 sentences on what to do differently tomorrow based on today's data"
}}

Be specific. Use actual numbers from the data. Do not give generic advice. Every suggestion must reference specific data points. If there are fewer than 10 trades, flag every suggestion as low-confidence. Respond with ONLY the JSON. No preamble. No markdown fences."""


# ── Console output ─────────────────────────────────────────────────────────────

def print_summary(analysis: dict, trades: list) -> None:
    wins    = sum(1 for t in trades if t["outcome"] == "win")
    losses  = sum(1 for t in trades if t["outcome"] == "loss")
    net_pnl = sum(t["pnl_dollars"] or 0 for t in trades)

    print("\n" + "=" * 62)
    print(f"  DAILY ANALYSIS — {analysis.get('date', '?')}")
    print("=" * 62)
    print(
        f"  Grade: {analysis.get('overall_grade', '?')}  |  "
        f"Trades: {len(trades)}  |  W/L: {wins}/{losses}  |  "
        f"PnL: ${net_pnl:.2f}"
    )
    print(f"\n  {analysis.get('overall_assessment', '')}")

    per_asset = analysis.get("per_asset_analysis", {})
    if per_asset:
        print("\n  PER-ASSET:")
        for asset, info in per_asset.items():
            keep = "TRADE" if info.get("keep_trading") else "PAUSE"
            print(
                f"    {asset:4s}: {info.get('grade', '?'):2s} [{keep}] — "
                f"{info.get('assessment', '')}"
            )

    suggestions = analysis.get("parameter_suggestions", [])
    if suggestions:
        print(f"\n  PARAMETER SUGGESTIONS ({len(suggestions)}):")
        for s in suggestions:
            asset_tag = f" [{s['asset']}]" if s.get("asset") and s["asset"] != "ALL" else ""
            print(f"    {s['field']}{asset_tag}: {s.get('current_value')} → {s.get('suggested_value')}")
            print(f"      Reason: {s.get('reason', '')}")
            print(f"      Confidence: {s.get('confidence', '?')} | Data points: {s.get('data_points', '?')}")

    patterns = analysis.get("patterns_detected", [])
    if patterns:
        print("\n  PATTERNS DETECTED:")
        for p in patterns:
            print(f"    - {p}")

    warnings = analysis.get("risk_warnings", [])
    if warnings:
        print("\n  RISK WARNINGS:")
        for w in warnings:
            print(f"    ! {w}")

    print(f"\n  TOMORROW: {analysis.get('tomorrow_strategy', '')}")
    print("=" * 62 + "\n")


def save_suggested_changes(analysis: dict, total_trades: int) -> None:
    suggestions = analysis.get("parameter_suggestions", [])
    if not suggestions:
        return
    data = {
        "generated":       datetime.now(timezone.utc).isoformat(),
        "based_on_date":   analysis.get("date"),
        "based_on_trades": total_trades,
        "suggestions":     suggestions,
        "auto_apply":      False,
    }
    SUGGESTED_FILE.write_text(json.dumps(data, indent=2))
    print(f"  Suggestions saved → {SUGGESTED_FILE}")
    print(f"  Review with: python apply_suggestions.py")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Daily post-session Claude analysis for the Kalshi trading bot.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python claude_analyzer.py                    # analyze today's trades (UTC)
  python claude_analyzer.py --date 2025-04-15  # re-analyze a specific date
  python claude_analyzer.py --force            # overwrite existing analysis

Output files:
  daily_analysis/YYYY-MM-DD.json    Claude's full analysis (append-only history)
  suggested_config_changes.json     Latest parameter suggestions (overwritten daily)
        """,
    )
    parser.add_argument("--date",  help="Date to analyze (YYYY-MM-DD). Defaults to today UTC.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing analysis.")
    args = parser.parse_args()

    if not _ANTHROPIC_AVAILABLE:
        print("ERROR: anthropic package not installed. Run: pip install anthropic")
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ERROR: Set ANTHROPIC_API_KEY environment variable to use the analyzer.")
        print("       Get your key from console.anthropic.com")
        sys.exit(1)

    date_str = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        print(f"ERROR: Invalid date format '{date_str}'. Use YYYY-MM-DD.")
        sys.exit(1)

    DAILY_DIR.mkdir(exist_ok=True)
    out_file = DAILY_DIR / f"{date_str}.json"

    if out_file.exists() and not args.force:
        print(f"Analysis for {date_str} already exists: {out_file}")
        print("Use --force to overwrite.")
        sys.exit(0)

    print(f"Analyzing trades for {date_str}...")

    trades = pull_trades(date_str)
    print(f"Found {len(trades)} completed trades.")

    if not trades:
        print("No completed trades found for this date. Nothing to analyze.")
        sys.exit(0)

    config    = read_config()
    price_val = pull_price_validation(date_str)
    if price_val:
        print(f"Price validation: {price_val['samples']} samples, avg gap {price_val['avg_gap_cents']}c")
    else:
        print("Price validation: no data for today.")

    print(f"Sending to Claude ({MODEL})...")
    prompt = build_prompt(date_str, trades, config, price_val)

    client   = anthropic.Anthropic(api_key=api_key)
    analysis = None
    raw      = ""

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw      = response.content[0].text
        analysis = _parse_claude_json(raw)
    except json.JSONDecodeError as exc:
        print(f"WARNING: Claude returned non-JSON: {exc}")
        analysis = {
            "date":               date_str,
            "overall_grade":      "N/A",
            "overall_assessment": "Claude returned non-JSON. Local stats saved below.",
            "claude_error":       str(exc),
            "claude_raw":         raw[:500],
        }
    except Exception as exc:
        print(f"WARNING: Claude API call failed: {exc}")
        analysis = {
            "date":               date_str,
            "overall_grade":      "N/A",
            "overall_assessment": f"Claude API error: {exc}",
            "claude_error":       str(exc),
        }

    # Always embed local stats for record-keeping
    wins   = [t for t in trades if t["outcome"] == "win"]
    losses = [t for t in trades if t["outcome"] == "loss"]
    analysis["_local_stats"] = {
        "total_trades":  len(trades),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate_pct":  round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "net_pnl":       round(sum(t["pnl_dollars"] or 0 for t in trades), 4),
        "price_validation": price_val or None,
    }

    out_file.write_text(json.dumps(analysis, indent=2))
    print(f"Analysis saved → {out_file}")

    save_suggested_changes(analysis, len(trades))
    print_summary(analysis, trades)


if __name__ == "__main__":
    main()
