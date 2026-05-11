"""
bot.py — Entrypoint for the Kalshi 15-minute prediction market trading bot.

All core logic lives in focused modules: bot_infra (config/db/notify),
bot_market (Kalshi API + orders), bot_risk (risk + trade execution +
preflight), bot_strategy (S1/S2 brains), bot_loops (phase handlers +
main loop), bot_state (shared globals), asset_manager.

Start via runner.py, not directly.
"""

import asyncio
import logging
import os
import sqlite3

import aiohttp

from reconcile import classify_pending_trade

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import obs
import bot_state
import asset_manager
from asset_manager import get_price as _am_get_price, coinbase_price_task, seed_price_history
from bot_infra import read_config, _init_config, init_db, test_db_write, send_telegram
from bot_market import load_credentials, get_btc_price
from bot_risk import verify_kalshi_connection, run_preflight_checks
from bot_loops import main_loop

# ─────────────────────────────── logging ───────────────────────────────────
log = logging.getLogger("bot")


async def _startup_reconcile(session: aiohttp.ClientSession, mode: str) -> None:
    """
    Classify all pending trades older than 30 min against Kalshi and apply
    the reconcile action per row, inside individual transactions.
    Sends one Telegram summary. Replaces blind zombie-trade cleanup.
    """
    conn = sqlite3.connect(bot_state._DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, market_id, side, contracts, entry_price_cents, ts, order_id, mode, asset "
            "FROM trades "
            "WHERE (outcome IN ('pending', '') OR outcome IS NULL) "
            "AND ts < datetime('now', '-30 minutes')"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        msg = "Startup reconcile: 0 marked filled, 0 phantoms, 0 left pending, 0 errors."
        log.info(msg)
        await send_telegram(msg)
        return

    n_filled = n_phantom = n_pending = n_error = 0
    detail_lines: list[str] = []

    for row in rows:
        trade_id = row["id"]
        try:
            result = await classify_pending_trade(session, row, mode)
        except Exception as exc:
            log.error("Reconcile error trade %s: %s", trade_id, exc, exc_info=True)
            n_error += 1
            continue

        action = result["action"]

        if action == "leave_pending":
            n_pending += 1
            log.info("Startup reconcile: trade %s → leave_pending (%s)", trade_id, result.get("reason", ""))
            continue

        try:
            conn2 = sqlite3.connect(bot_state._DB_FILE)
            try:
                if action == "mark_filled":
                    conn2.execute(
                        "UPDATE trades SET outcome=?, exit_price_cents=?, "
                        "pnl_dollars=?, fill_confirmed=1 WHERE id=?",
                        (result["outcome"], result["exit_price_cents"], result["pnl_dollars"], trade_id),
                    )
                elif action == "mark_expired_unfilled":
                    conn2.execute(
                        "UPDATE trades SET outcome='expired_unfilled', exit_price_cents=0, "
                        "pnl_dollars=0.0, fill_confirmed=0 WHERE id=?",
                        (trade_id,),
                    )
                elif action == "mark_phantom":
                    conn2.execute(
                        "UPDATE trades SET outcome='phantom', exit_price_cents=0, "
                        "pnl_dollars=0.0, fill_confirmed=0 WHERE id=?",
                        (trade_id,),
                    )
                conn2.commit()
            finally:
                conn2.close()
        except Exception as exc:
            log.error("Reconcile DB update failed trade %s: %s", trade_id, exc, exc_info=True)
            n_error += 1
            continue

        if action == "mark_filled":
            n_filled += 1
            detail = (f"trade {trade_id} ({row['market_id']}) → filled "
                      f"outcome={result['outcome']} pnl=${result['pnl_dollars']:.2f}")
        else:
            n_phantom += 1
            reason = result.get("reason", "")
            detail = f"trade {trade_id} ({row['market_id']}) → {action} ({reason})"

        if len(detail_lines) < 10:
            log.info("Startup reconcile: %s", detail)
        detail_lines.append(detail)

    if len(detail_lines) > 10:
        log.info("Startup reconcile: ... %d more trades (see DB)", len(detail_lines) - 10)

    summary = (f"Startup reconcile: {n_filled} marked filled, {n_phantom} phantoms, "
               f"{n_pending} left pending, {n_error} errors.")
    log.info(summary)
    await send_telegram(summary)


# ═══════════════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════════════

async def main() -> None:
    """Bootstrap: load credentials, init DB, start BTC feed, run main loop."""
    obs.setup_logging("bot")
    _init_config()
    load_credentials(mode=read_config().get("mode", "paper"))
    init_db()
    test_db_write()

    # Reconcile pending trades against Kalshi before entering the main loop.
    # Replaces the old blind zombie-trade cleanup that wrote -entry×count PnL without
    # checking whether orders were actually filled.
    _mode = read_config().get("mode", "paper")
    async with aiohttp.ClientSession() as _startup_session:
        try:
            await _startup_reconcile(_startup_session, _mode)
        except Exception as _re:
            log.error(
                "Reconcile failed at startup — proceeding with bot start, "
                "manual review needed. %s", _re,
            )
        # Verify Kalshi credentials after reconcile (same session, same startup block).
        # Skipped in paper mode — no real credentials are loaded there.
        if _mode != "paper":
            await verify_kalshi_connection(_startup_session)

    # Start Coinbase price feed for all assets
    _startup_config = read_config()
    _enabled = _startup_config.get("enabled_assets", ["ETH", "SOL", "XRP"])
    # Always subscribe BTC regardless of enabled_assets — other strategies use
    # btc_prices_60m for correlation signals and the deque must stay populated.
    _feed_assets = list(dict.fromkeys(["BTC"] + _enabled))
    await seed_price_history(_feed_assets)
    asyncio.create_task(coinbase_price_task(_feed_assets))

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
