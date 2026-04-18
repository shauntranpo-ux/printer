"""
Section 3 paper-trade runner.

Starts the bot in paper mode with the new BTC strategy enabled.
Runs for HOURS_TO_RUN hours, then stops.

Usage:
    python scripts\\section3_paper_run.py --hours 4

The bot logs to stdout. Trades are persisted to the usual kalshi_bot.db.
Use the existing dashboard (dashboard.html + server.py) to inspect results
after the session.
"""

import argparse
import asyncio
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=4.0)
    args = parser.parse_args()

    sys.path.insert(0, ".")
    import bot

    # Read config and warn if anything looks unsafe
    cfg = bot.read_config()
    mode = cfg.get("mode", "paper")
    enabled = cfg.get("bot_enabled", False)
    new_strat = cfg.get("use_new_strategies", {}).get("BTC", False)

    print(f"[section3] mode={mode} bot_enabled={enabled} use_new_BTC={new_strat}")
    if mode != "paper":
        print("[section3] REFUSING to run -- mode is not paper.")
        sys.exit(1)

    # Force bot_enabled=True for this session only (in-memory)
    cfg["bot_enabled"] = True
    bot.write_config(cfg)

    # Schedule shutdown
    stop_at = time.time() + args.hours * 3600

    async def watchdog():
        while time.time() < stop_at:
            await asyncio.sleep(30)
        print(f"[section3] {args.hours}h elapsed; shutting down.")
        # Restore config
        cfg["bot_enabled"] = False
        bot.write_config(cfg)
        # Force exit
        import os
        os._exit(0)

    async def run():
        await asyncio.gather(
            bot.main(),
            watchdog(),
        )

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        cfg["bot_enabled"] = False
        bot.write_config(cfg)
        print("[section3] interrupted; config restored.")


if __name__ == "__main__":
    main()
