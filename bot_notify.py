"""bot_notify.py â€” Telegram notifications and phase/context helpers."""
import asyncio
import logging

import aiohttp

import bot_state

log = logging.getLogger("bot")


def _phase_for_eth(asset, elapsed_seconds):
    """Return ETH hourly window-phase label ('Mid'/'Dwell'/'Late') or None.

    BTC and all 15m markets return None.
    """
    if asset != "ETH":
        return None
    m = elapsed_seconds / 60.0
    if 9 <= m <= 11:
        return "Mid"
    if 30 <= m <= 42:
        return "Dwell"
    if m >= 45:
        return "Late"
    return None


def _notify_ctx(asset, ticker, duration_min=15.0, phase=None):
    """Format a context prefix for Telegram notifications."""
    parts = [asset, "15m", ticker]
    return f"[{' | '.join(parts)}]"


async def _maybe_fill_verification_notify(
    asset: str,
    ticker: str,
    side: str,
    market: dict | None,
    secs_left: float,
    entry_price_cents: int | None,
    price_this_attempt: int | None,
    market_ask_at_post_c: int | None,
    fill_yes_price: int | None,
) -> None:
    """Send a fill-verification Telegram message to spot price-selection bugs in flight.

    Compares:
      - Target:     the strategy-chosen entry price (may be None for BTC).
      - Market ask: the ask observed just before POST.
      - Posted:     the price actually sent to Kalshi (may differ via retry drift).
      - Filled:     the price Kalshi returned on fill.

    Warns with âš ï¸ when abs(filled - target) > 3Â¢. Silently skips when fill_yes_price is None.
    """
    if fill_yes_price is None:
        return
    _target = entry_price_cents  # may be None for BTC (strategy doesn't emit it)
    _ask = market_ask_at_post_c
    _posted = price_this_attempt
    _filled = fill_yes_price
    _target_str = f"{int(round(_target))}Â¢" if _target is not None else "â€”"
    _ask_str    = f"{int(round(_ask))}Â¢"    if _ask    is not None else "â€”"
    _posted_str = f"{int(round(_posted))}Â¢" if _posted is not None else "â€”"
    _filled_str = f"{int(round(_filled))}Â¢"
    if _target is not None:
        _slip_target = int(round(_filled - _target))
        _slip_target_str = f"{_slip_target:+d}Â¢ vs target"
        _warn = "âš ï¸ " if abs(_slip_target) > 3 else "ðŸŽ¯ "
    else:
        _slip_target_str = "n/a vs target"
        _warn = "ðŸŽ¯ "
    _slip_market_str = (
        f"{int(round(_filled - _ask)):+d}Â¢ vs market" if _ask is not None else "n/a vs market"
    )
    _ctx = _notify_ctx(asset, ticker)
    await send_telegram(
        f"{_warn}<b>{_ctx} FILL VERIFICATION</b>\n"
        f"Target:     <b>{_target_str}</b>\n"
        f"Market ask: {_ask_str}\n"
        f"Posted:     {_posted_str}\n"
        f"Filled:     <b>{_filled_str}</b>\n"
        f"Slippage:   {_slip_target_str}  |  {_slip_market_str}"
    )


async def send_telegram(text: str) -> None:
    """Send a Telegram notification with up to 3 retries on failure."""
    if not bot_state.TELEGRAM_BOT_TOKEN or not bot_state.TELEGRAM_CHAT_ID:
        return  # silently skip â€” Telegram is optional
    url = f"https://api.telegram.org/bot{bot_state.TELEGRAM_BOT_TOKEN}/sendMessage"
    for attempt in range(1, 4):
        try:
            log.info(f"Telegram: sending (attempt {attempt}/3)â€¦")
            async with aiohttp.ClientSession() as tg:
                async with tg.post(
                    url,
                    json={"chat_id": bot_state.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    body = await resp.text()
                    if resp.status == 200:
                        log.info("Telegram: sent OK")
                        return
                    elif resp.status == 429:
                        log.warning(f"Telegram: rate-limited (429) â€” attempt {attempt}/3, retryingâ€¦")
                    else:
                        log.warning(f"Telegram: HTTP {resp.status} â€” {body}")
                        return  # non-retryable HTTP error
        except Exception as exc:
            log.warning(f"Telegram: error on attempt {attempt}/3 â€” {exc}")
        if attempt < 3:
            await asyncio.sleep(2)
    log.error("Telegram: failed after 3 attempts â€” notification dropped")

