"""
bot.py — Entrypoint for the Kalshi 15-minute prediction market trading bot.

All core logic lives in focused modules: bot_infra (config/db/notify),
bot_market (Kalshi API + orders), bot_risk (risk + trade execution +
preflight), bot_strategy (S1/S2 brains), bot_loops (phase handlers +
main loop), bot_state (shared globals), asset_manager, obi_monitor.

Start via runner.py, not directly.
"""

import asyncio
import logging
import os
import sqlite3
import sys

import aiohttp

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from obi_monitor import OBIMonitor
import bot_state
import asset_manager
from asset_manager import get_price as _am_get_price, coinbase_price_task
from bot_infra import read_config, _init_config, init_db, test_db_write, send_telegram
from bot_market import load_credentials, get_btc_price
from bot_risk import verify_kalshi_connection, run_preflight_checks
from bot_loops import main_loop

# ─────────────────────────────── logging ───────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("bot")


# ═══════════════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════════════

async def main() -> None:
    """Bootstrap: load credentials, init DB, start BTC feed, run main loop."""
    _init_config()
    load_credentials(mode=read_config().get("mode", "paper"))
    init_db()
    test_db_write()

    # Clean up zombie "pending" trades from prior crashed sessions.
    # Any trade still pending after 30+ minutes never settled — mark it as expired.
    try:
        conn = sqlite3.connect(bot_state._DB_FILE)
        cleaned = conn.execute(
            """UPDATE trades
               SET outcome         = 'expired_untracked',
                   exit_price_cents = 0,
                   pnl_dollars      = -(COALESCE(entry_price_cents,0) * COALESCE(contracts,1) / 100.0),
                   fill_confirmed   = 0
               WHERE outcome IN ('pending', '', NULL)
                 AND ts < datetime('now', '-30 minutes')"""
        ).rowcount
        conn.commit()
        conn.close()
        if cleaned:
            log.warning(f"Startup cleanup: marked {cleaned} zombie pending trade(s) as expired_untracked")
    except Exception as _e:
        log.warning(f"Startup zombie-trade cleanup failed (non-fatal): {_e}")


    # Verify Kalshi credentials and log account balance before doing anything.
    # Skipped in paper mode — no real credentials are loaded there.
    if read_config().get("mode", "paper") != "paper":
        async with aiohttp.ClientSession() as verify_session:
            await verify_kalshi_connection(verify_session)

    # Start Coinbase price feed for all assets
    _startup_config = read_config()
    _enabled = _startup_config.get("enabled_assets", ["ETH", "SOL", "XRP"])
    # Always subscribe BTC regardless of enabled_assets — other strategies use
    # btc_prices_60m for correlation signals and the deque must stay populated.
    _feed_assets = list(dict.fromkeys(["BTC"] + _enabled))
    from asset_manager import seed_price_history
    await seed_price_history(_feed_assets)
    asyncio.create_task(coinbase_price_task(_feed_assets))

    # OBI monitor (Coinbase Exchange level2 WebSocket)
    bot_state._obi_monitor = OBIMonitor(["BTC", "ETH", "SOL", "XRP", "DOGE"])
    asyncio.create_task(bot_state._obi_monitor.run())
    log.info("OBI monitor started for BTC, ETH, SOL, XRP, DOGE")


    # Wait for the first price from any enabled asset (timeout after 120s so Railway doesn't hang)
    _first_asset = _enabled[0] if _enabled else "ETH"
    log.info(f"Waiting for price feeds ({_enabled})...")
    waited = 0
    while _am_get_price(_first_asset) is None and waited < 120:
        await asyncio.sleep(1)
        waited += 1
        if waited % 30 == 0:
            log.warning(f"Still waiting for {_first_asset} price feed ({waited}s elapsed)...")
    _first_price = _am_get_price(_first_asset)
    if _first_price is None:
        log.warning("Price feed not available after 120s — continuing anyway; prices will populate shortly.")
    else:
        log.info(f"Price feed ready after {waited}s. {_first_asset}: ${_first_price:,.2f}")
    _startup_cfg = read_config()
    _btc_display = f"${get_btc_price():,.2f}" if get_btc_price() is not None else f"{_first_asset}: ${_first_price:,.2f}" if _first_price else "price N/A"
    await send_telegram(f"<b>Printer bot started</b>\n{_btc_display}\nMode: {_startup_cfg.get('mode','?').upper()}  |  Bot enabled: {_startup_cfg.get('bot_enabled', False)}")

    # Pre-flight check runs once before trading begins.
    # LIVE mode with unresolved issues → sys.exit(1). Paper mode → warn and continue.
    await run_preflight_checks(_startup_cfg)

    await main_loop()


if __name__ == "__main__":
    asyncio.run(main())
