"""
bot.py — Core trading logic for the Kalshi 15-minute prediction market bot.

Connects to Coinbase for live crypto prices, polls Kalshi for the
soonest-expiring 15-minute markets (ETH, SOL, XRP), evaluates
evidence-based strategy signals, places paper or live orders, and enforces
daily loss / profit limits. Writes bot_state.json every cycle for server.py.

Start via runner.py, not directly.
"""

import asyncio
import json
import math
import sqlite3
import logging
import os
import re
import sys
import tempfile
import time
from base64 import b64encode
from collections import deque
from datetime import datetime, timezone, timedelta

import aiosqlite
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from obi_monitor import OBIMonitor
import asset_manager
import bot_state
from bot_config import (
    atomic_write_json, read_config, write_config,
    get_asset_config, _init_config,
)
from bot_db import (
    init_db, test_db_write, db_write_trade,
    db_update_trade, db_write_market_log, db_get_today_pnl,
)
from asset_manager import (
    ASSET_CONFIG,
    get_price           as _am_get_price,
    price_age_seconds   as _am_price_age,
    coinbase_price_task,
)
from bot_kalshi import (
    load_credentials, kalshi_headers, get_btc_price,
    fetch_current_market, fetch_market_for_asset, parse_strike,
    seconds_remaining, seconds_elapsed, fetch_orderbook,
    _simulated_amm_midpoint, _log_price_validation,
)
from bot_notify import (
    _phase_for_eth, _notify_ctx,
    _maybe_fill_verification_notify, send_telegram,
)

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ logging â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("bot")

from bot_strategy import (
    track_contract_price, _session_ev_adjustment, _strategy_name_for,
    _get_or_make_strategy_s2, strategy_brain_s2,
    _s1_empirical_win_prob, _s1_calculate_momentum, _s1_realized_vol,
    _s1_contract_velocity, strategy_brain_s1,
)

from bot_orders import (
    calculate_contracts, implied_prob,
    _portfolio_has_position, _verify_order_fill, place_order,
)



from bot_risk import (
    check_daily_limits, midnight_reset, _parse_strike_from_ticker,
    write_state_file, _log_entry,
)

from bot_trade import _execute_s1_trade, _settle_s1_trade, _try_settle_orphaned_s1
from bot_preflight import verify_kalshi_connection, run_preflight_checks
from bot_loops import (
    handle_ready_phase, handle_locked_phase,
    _init_asset_state, _process_asset,
    _non_btc_asset_loop, main_loop,
)



# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  Entry point
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

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

    # BTC/ETH funding dispersion monitors (Hyperliquid + Binance)
    _src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
    import sys as _sys_fm
    if _src_path not in _sys_fm.path:
        _sys_fm.path.insert(0, _src_path)
    from strategies.original.signals.funding_dispersion import FundingDispersionMonitor as _FDM
    bot_state._funding_monitor_btc = _FDM("BTC")
    bot_state._funding_monitor_eth = _FDM("ETH")

    async def _funding_refresh_loop() -> None:
        while True:
            try:
                await bot_state._funding_monitor_btc.refresh()
                await bot_state._funding_monitor_eth.refresh()
            except Exception as _fe:
                log.warning(f"funding refresh (BTC/ETH) error: {_fe}")
            await asyncio.sleep(60)

    asyncio.create_task(_funding_refresh_loop())
    log.info("BTC/ETH funding monitors started")

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


