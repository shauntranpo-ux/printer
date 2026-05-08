"""CLI wrapper — prints daily stats to stdout.

Usage:
    py scripts/stats_report.py [--date YYYY-MM-DD]

Reads BOT_DB_FILE env var (defaults to kalshi_bot.db).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bot_stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Print daily bot stats to stdout.")
    parser.add_argument("--date", default=None, help="UTC date (YYYY-MM-DD). Defaults to today.")
    args = parser.parse_args()

    db_path = os.environ.get("BOT_DB_FILE", "kalshi_bot.db")
    stats = bot_stats.query_stats(db_path, today_date=args.date)
    stats["consecutive_losses"] = 0  # not available outside running bot
    stats["mode"] = os.environ.get("BOT_MODE", "PAPER").upper()

    print(bot_stats.format_terminal(stats))


if __name__ == "__main__":
    main()
