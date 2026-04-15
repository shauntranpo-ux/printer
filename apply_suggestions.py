#!/usr/bin/env python3
"""
apply_suggestions.py — Interactive review and application of config suggestions.

Reads suggested_config_changes.json, shows each suggestion one at a time,
prompts Y/N, and writes approved changes to config.json. Backs up config first.
Logs every applied change to config_change_log.json.

Usage:
    python apply_suggestions.py             # review and apply latest suggestions
    python apply_suggestions.py --history   # show all past config changes
    python apply_suggestions.py --help      # show this help

NEVER auto-applies suggestions. You always review each one manually.
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

CONFIG_FILE     = os.environ.get("BOT_CONFIG_FILE", "config.json")
SUGGESTED_FILE  = Path("suggested_config_changes.json")
CHANGE_LOG_FILE = Path("config_change_log.json")
BACKUP_DIR      = Path("config_backups")

ASSETS = ["BTC", "ETH", "SOL", "XRP", "DOGE"]

# Fields that can be overridden per-asset via asset_overrides
OVERRIDABLE_PER_ASSET = {
    "min_ev_base",
    "max_entry_price_cents",
    "min_reward_cents",
    "max_risk_reward_ratio",
    "vol_gate_thresh",
    "kelly_cap",
}


# ── Config I/O ─────────────────────────────────────────────────────────────────

def read_config() -> dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)


def write_config(cfg: dict) -> None:
    """Write config atomically via temp file."""
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_FILE)


def backup_config() -> str:
    BACKUP_DIR.mkdir(exist_ok=True)
    ts   = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = BACKUP_DIR / f"{ts}_config.json"
    shutil.copy2(CONFIG_FILE, dest)
    return str(dest)


# ── Change log ─────────────────────────────────────────────────────────────────

def load_change_log() -> list:
    if CHANGE_LOG_FILE.exists():
        try:
            return json.loads(CHANGE_LOG_FILE.read_text())
        except Exception:
            return []
    return []


def save_change_log(entries: list) -> None:
    CHANGE_LOG_FILE.write_text(json.dumps(entries, indent=2))


# ── Applying suggestions ───────────────────────────────────────────────────────

def apply_suggestion(cfg: dict, suggestion: dict) -> None:
    """Mutate cfg in-place with one suggestion."""
    field = suggestion["field"]
    asset = suggestion.get("asset", "ALL")
    value = suggestion["suggested_value"]

    if asset == "ALL" or asset not in ASSETS or field not in OVERRIDABLE_PER_ASSET:
        # Global change
        cfg[field] = value
    else:
        # Per-asset override — ensure the structure exists
        cfg.setdefault("asset_overrides", {})
        for a in ASSETS:
            cfg["asset_overrides"].setdefault(a, {})
        cfg["asset_overrides"][asset][field] = value


# ── Display ────────────────────────────────────────────────────────────────────

def show_suggestion(idx: int, total: int, s: dict) -> None:
    asset = s.get("asset", "ALL")
    scope = f"[{asset}]" if asset not in ("ALL", None) else "[ALL ASSETS]"
    print()
    print(f"  Suggestion {idx}/{total}")
    print(f"  {'─' * 52}")
    print(f"  Field:      {s.get('field', '?')} {scope}")
    print(f"  Change:     {s.get('current_value')}  →  {s.get('suggested_value')}")
    print(f"  Confidence: {s.get('confidence', '?')} | Data points: {s.get('data_points', '?')}")
    print(f"  Reason:     {s.get('reason', 'N/A')}")


def ask_yn(question: str) -> bool:
    while True:
        try:
            resp = input(f"\n  {question} [y/n]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\n  Cancelled.")
            sys.exit(0)
        if resp in ("y", "yes"):
            return True
        if resp in ("n", "no"):
            return False
        print("  Please enter y or n.")


def show_history() -> None:
    log = load_change_log()
    if not log:
        print("No config changes recorded yet.")
        return
    print(f"\n{'=' * 62}")
    print(f"  CONFIG CHANGE HISTORY  ({len(log)} total changes)")
    print(f"{'=' * 62}")
    for entry in log:
        ts    = entry.get("applied_at", "?")
        field = entry.get("field", "?")
        asset = entry.get("asset", "ALL")
        old   = entry.get("old_value")
        new   = entry.get("new_value")
        date  = entry.get("based_on_date", "?")
        scope = f" [{asset}]" if asset not in ("ALL", None) else ""
        print(f"  {ts}  {field}{scope}: {old} → {new}  (from {date})")
        reason = entry.get("reason", "")
        if reason:
            print(f"    {reason[:90]}{'...' if len(reason) > 90 else ''}")
    print(f"{'=' * 62}\n")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Review and apply config suggestions from the daily Claude analyzer.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python apply_suggestions.py           # review and apply latest suggestions
  python apply_suggestions.py --history # show all past config changes

Workflow:
  1. Run: python claude_analyzer.py    -- generates suggested_config_changes.json
  2. Run: python apply_suggestions.py -- review each suggestion interactively
  3. Bot reads config.json each cycle -- changes take effect immediately (paper mode)
                                         restart bot for live mode

Safety:
  - config.json is backed up to config_backups/ before any changes
  - Every applied change is logged to config_change_log.json
  - Suggestions are NEVER auto-applied
        """,
    )
    parser.add_argument("--history", action="store_true", help="Show all past config changes and exit.")
    args = parser.parse_args()

    if args.history:
        show_history()
        return

    if not SUGGESTED_FILE.exists():
        print("No suggestions file found.")
        print(f"Expected: {SUGGESTED_FILE}")
        print("Run: python claude_analyzer.py  to generate suggestions.")
        sys.exit(0)

    try:
        data = json.loads(SUGGESTED_FILE.read_text())
    except Exception as exc:
        print(f"ERROR reading {SUGGESTED_FILE}: {exc}")
        sys.exit(1)

    suggestions = data.get("suggestions", [])
    if not suggestions:
        print("No parameter suggestions in the file.")
        print("The analyzer found no issues to fix today.")
        sys.exit(0)

    generated    = data.get("generated", "?")
    based_on     = data.get("based_on_date", "?")
    based_trades = data.get("based_on_trades", 0)

    print(f"\n{'=' * 62}")
    print(f"  CONFIG SUGGESTIONS")
    print(f"  Generated:  {generated}")
    print(f"  Based on:   {based_on}  ({based_trades} trades)")
    print(f"  Suggestions: {len(suggestions)}")
    print(f"{'=' * 62}")

    try:
        cfg = read_config()
    except Exception as exc:
        print(f"ERROR reading {CONFIG_FILE}: {exc}")
        sys.exit(1)

    approved: list = []

    for i, s in enumerate(suggestions, 1):
        show_suggestion(i, len(suggestions), s)
        if ask_yn("Apply this change?"):
            approved.append(s)
            print("  ✓ Queued")
        else:
            print("  Skipped.")

    if not approved:
        print("\nNo changes applied.")
        return

    # Final confirmation
    print(f"\n  Ready to apply {len(approved)} change(s):")
    for s in approved:
        scope = f" [{s['asset']}]" if s.get("asset") and s["asset"] != "ALL" else ""
        print(f"    {s['field']}{scope}: {s.get('current_value')} → {s.get('suggested_value')}")

    if not ask_yn(f"Confirm writing {len(approved)} change(s) to {CONFIG_FILE}?"):
        print("Cancelled. No changes made.")
        return

    # Backup
    backup_path = backup_config()
    print(f"\n  Backup saved → {backup_path}")

    # Apply and log
    change_log = load_change_log()
    now_ts     = datetime.now(timezone.utc).isoformat()

    for s in approved:
        apply_suggestion(cfg, s)
        change_log.append({
            "applied_at":     now_ts,
            "field":          s.get("field"),
            "asset":          s.get("asset", "ALL"),
            "old_value":      s.get("current_value"),
            "new_value":      s.get("suggested_value"),
            "reason":         s.get("reason", ""),
            "confidence":     s.get("confidence", "?"),
            "data_points":    s.get("data_points"),
            "based_on_date":  data.get("based_on_date"),
            "based_on_trades": based_trades,
        })

    write_config(cfg)
    save_change_log(change_log)

    print(f"\n  Applied {len(approved)} change(s) to {CONFIG_FILE}")
    print(f"  Change log updated → {CHANGE_LOG_FILE}")
    print(f"  The bot reads config.json every cycle — changes apply on next evaluation.")
    print()


if __name__ == "__main__":
    main()
