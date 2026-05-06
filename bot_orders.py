"""bot_orders.py — Contract math, order placement, fill verification."""
import asyncio
import logging
import math
import time

import aiohttp

import bot_state
from bot_kalshi import kalshi_headers, fetch_orderbook, seconds_elapsed
from bot_notify import send_telegram, _maybe_fill_verification_notify, _phase_for_eth, _notify_ctx

log = logging.getLogger("bot")


def calculate_contracts(
    trade_amount_dollars: float,
    entry_price_cents: int,
    liquidity: int,
) -> tuple[int, float]:
    """
    Fixed position sizing — always spend exactly trade_amount_dollars.

    Returns:
        (contracts, dollars_used)
    """
    if entry_price_cents <= 0:
        return 0, 0.0

    price_dollars = entry_price_cents / 100.0

    def _taker_fee(n: int) -> float:
        raw = 0.07 * n * price_dollars * (1.0 - price_dollars)
        return math.ceil(raw * 100) / 100.0

    contracts = int(trade_amount_dollars * 100 / entry_price_cents)
    contracts = min(contracts, liquidity)
    contracts = max(contracts, 0)

    # Reduce until stake + fee fits within budget (fee is paid at purchase time)
    while contracts > 0 and contracts * price_dollars + _taker_fee(contracts) > trade_amount_dollars:
        contracts -= 1

    dollars_used = contracts * price_dollars

    log.info(
        f"Fixed sizing: price={entry_price_cents}c "
        f"bet=${dollars_used:.2f} fee=${_taker_fee(contracts):.2f} -> {contracts} contracts"
    )
    return contracts, dollars_used


# ══════════════════════════════════════════════════════════════════════════════════
#  Probability helpers
# ══════════════════════════════════════════════════════════════════════════════════

def implied_prob(contract_price_cents: float) -> float:
    """Convert contract price in cents to implied probability (0—1)."""
    return contract_price_cents / 100.0



# ══════════════════════════════════════════════════════════════════════════════════
#  Order placement


async def _portfolio_has_position(
    session: aiohttp.ClientSession,
    ticker: str,
    side: str,
) -> bool:
    """Check whether the portfolio already holds a position for (ticker, side)."""
    if not ticker or not side:
        return False
    try:
        pos_path = f"/portfolio/positions?ticker={ticker}"
        async with session.get(
            bot_state.KALSHI_BASE_URL + pos_path,
            headers=kalshi_headers("GET", pos_path),
            timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                return False
            pos_data = await resp.json()
        positions = pos_data.get("market_positions") or pos_data.get("positions") or []
        for p in positions:
            if p.get("ticker") == ticker:
                held = p.get("position", 0)
                if side == "yes" and held > 0:
                    return True
                if side == "no" and held < 0:
                    return True
    except Exception as exc:
        log.warning(f"_portfolio_has_position error for {ticker}: {exc}")
    return False

# ══════════════════════════════════════════════════════════════════════════════════

async def _verify_order_fill(
    session: aiohttp.ClientSession,
    order_id: str,
    expected_filled: int,
    ticker: str = "",
    side: str = "",
) -> bool:
    """
    Confirm a fill is recorded in Kalshi by re-fetching the order.

    Returns True if Kalshi confirms at least one contract filled.
    On HTTP errors, falls back to a portfolio position check (requires ticker+side).
    On network exceptions, returns False — the conservative choice is to not book a
    phantom position rather than assume a fill that may not exist.
    """
    try:
        chk_path = f"/portfolio/orders/{order_id}"
        async with session.get(
            bot_state.KALSHI_BASE_URL + chk_path,
            headers=kalshi_headers("GET", chk_path),
            timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                log.warning(f"_verify_order_fill: GET {order_id} HTTP {resp.status} — checking portfolio")
                return await _portfolio_has_position(session, ticker, side)
            chk = await resp.json()
        order = chk.get("order") or chk
        status = order.get("status", "")
        total     = order.get("contracts_count") or expected_filled
        remaining = order.get("remaining_count")
        fc        = order.get("filled_count")
        if fc is not None:
            confirmed_filled = fc
        elif remaining is not None:
            confirmed_filled = total - remaining
        else:
            # Can't determine — trust the POST response
            confirmed_filled = expected_filled
        log.info(
            f"_verify_order_fill: {order_id} status={status!r} "
            f"filled={confirmed_filled}/{total}"
        )
        return confirmed_filled > 0
    except Exception as exc:
        log.warning(f"_verify_order_fill error for {order_id}: {exc} — returning False (conservative)")
        return False


async def place_order(
    session: aiohttp.ClientSession,
    ticker: str,
    side: str,
    contracts: int,
    entry_price_cents: int,
    mode: str,
    market: dict | None = None,
    asset: str = "BTC",
    secs_left: float = 900.0,
) -> dict:
    """
    Place a market order on Kalshi.

    Fills at best available price immediately. No price-bumping or GTC/IOC.

    In paper mode, simulates an instant fill without hitting the API.

    Returns:
        Dict with keys: fill_confirmed (bool), fill_price_cents (int|None),
        order_id (str|None).
    """
    if contracts <= 0:
        log.error(f"place_order called with contracts={contracts} — refusing to send invalid order")
        return {"fill_confirmed": False, "fill_price_cents": None, "order_id": None}

    # Preserve the strategy-chosen entry price for fill-verification telemetry.
    _original_strategy_target_c = entry_price_cents

    # Re-fetch fresh orderbook — used for paper slippage simulation AND non-paper
    # telemetry. Must run before the paper branch so paper fills reflect the live ask.
    fresh_ob = None
    try:
        fresh_ob = await fetch_orderbook(session, ticker, market)
        if fresh_ob is not None:
            fp = fresh_ob["best_yes_ask"] if side == "yes" else fresh_ob["best_no_ask"]
            if fp is not None and fp != entry_price_cents:
                log.info(f"Price updated {entry_price_cents}c -> {fp}c for {side.upper()} on {ticker}")
                entry_price_cents = fp
    except Exception as _fe:
        log.warning(f"Fresh price fetch failed: {_fe}")

    _market_ask_at_post_c = None
    try:
        _ob = fresh_ob if isinstance(fresh_ob, dict) else {}
        _market_ask_at_post_c = _ob.get("best_yes_ask") if side == "yes" else _ob.get("best_no_ask")
    except Exception:
        _market_ask_at_post_c = None

    if mode == "paper":
        paper_fill = _market_ask_at_post_c if _market_ask_at_post_c is not None else entry_price_cents
        log.info(f"[PAPER] Simulated BUY {side} {contracts}x @ {paper_fill}c on {ticker}")
        return {
            "fill_confirmed": True,
            "fill_price_cents": paper_fill,
            "order_id": f"paper_{int(time.time() * 1000)}",
        }

    path = "/portfolio/orders"
    price_this_attempt = entry_price_cents

    # ── Demo mode: post + poll ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
    # Demo API behaviour is unpredictable with order types; poll for a fill
    # rather than relying on an immediate response.
    if mode == "demo":
        client_order_id = f"demo_{int(time.time() * 1000)}"
        body = {
            "ticker": ticker,
            "side": side,
            "type": "market",
            "count": contracts,
            "action": "buy",
            "client_order_id": client_order_id,
        }
        log.info(f"[demo] market {side.upper()} {contracts}x on {ticker}")
        try:
            async with session.post(
                bot_state.KALSHI_BASE_URL + path,
                headers=kalshi_headers("POST", path),
                json=body,
                timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
            ) as resp:
                data = await resp.json()
                http_status = resp.status
        except Exception as exc:
            log.error(f"[demo] Order POST failed: {exc}")
            return {"fill_confirmed": False, "fill_price_cents": None, "order_id": None}

        if http_status not in (200, 201):
            log.error(f"[demo] Order HTTP {http_status}: {data}")
            return {"fill_confirmed": False, "fill_price_cents": None, "order_id": None}

        order_id = (data.get("order") or {}).get("order_id") or data.get("order_id")
        if not order_id:
            log.error(f"[demo] No order_id in response: {data}")
            return {"fill_confirmed": False, "fill_price_cents": None, "order_id": None}

        _poll_interval = 3.0
        _poll_timeout  = 30.0
        _elapsed       = 0.0
        filled_order   = None
        while _elapsed < _poll_timeout:
            await asyncio.sleep(_poll_interval)
            _elapsed += _poll_interval
            try:
                chk_path = f"/portfolio/orders/{order_id}"
                async with session.get(
                    bot_state.KALSHI_BASE_URL + chk_path,
                    headers=kalshi_headers("GET", chk_path),
                    timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
                ) as chk_resp:
                    chk_data = await chk_resp.json()
                chk_order = chk_data.get("order") or chk_data
                status = chk_order.get("status", "")
                if status not in ("resting", "pending"):
                    filled_order = chk_order
                    break
            except Exception as pe:
                log.warning(f"[demo] Poll error: {pe}")

        if filled_order is None:
            log.info(f"[demo] No fill after {_poll_timeout:.0f}s — cancelling {order_id}")
            try:
                del_path = f"/portfolio/orders/{order_id}"
                async with session.delete(
                    bot_state.KALSHI_BASE_URL + del_path,
                    headers=kalshi_headers("DELETE", del_path),
                    timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
                ) as del_resp:
                    await del_resp.json()
            except Exception as ce:
                log.warning(f"[demo] Cancel failed: {ce}")
            return {"fill_confirmed": False, "fill_price_cents": None, "order_id": order_id}

        status = filled_order.get("status", "")
        if status in ("cancelled", "canceled", "expired"):
            return {"fill_confirmed": False, "fill_price_cents": None, "order_id": order_id}

        _total     = filled_order.get("contracts_count")
        _remaining = filled_order.get("remaining_count")
        _fc        = filled_order.get("filled_count")
        cc = _fc if _fc is not None else ((_total - _remaining) if (_total is not None and _remaining is not None) else contracts)
        fp_raw = filled_order.get("yes_price", entry_price_cents)
        fp     = fp_raw if side == "yes" else (100 - fp_raw)  # yes_price is integer cents 0-100
        log.info(f"[demo] Order {order_id} filled={cc}x @ {fp}c status={status!r}")
        _fill_yes_price = fp_raw
        await _maybe_fill_verification_notify(
            asset, ticker, side, market, secs_left,
            _original_strategy_target_c, price_this_attempt,
            _market_ask_at_post_c, _fill_yes_price,
        )
        return {"fill_confirmed": cc > 0, "fill_price_cents": fp, "order_id": order_id, "filled_contracts": cc}

    # ── Live mode: single market order ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
    # Notify for late-window entries (<5 min) — user watches Telegram closely here.
    if secs_left < 300.0:
        _placed_mins = int(secs_left // 60)
        _placed_secs = int(secs_left % 60)
        _placed_elapsed = seconds_elapsed(market) if market else 0.0
        _placed_ctx = _notify_ctx(
            asset, ticker, (_placed_elapsed + secs_left) / 60.0,
            _phase_for_eth(asset, _placed_elapsed),
        )
        asyncio.create_task(send_telegram(
            f"<b>🔵 [S2 D3 Hybrid] {_placed_ctx} MARKET ORDER PLACED</b>\n"
            f"<b>{side.upper()} — {'UP' if side == 'yes' else 'DOWN'}</b>  {contracts} contracts\n"
            f"Expires in {_placed_mins}m {_placed_secs}s"
        ))

    log.info(f"[live] market {side.upper()} {contracts}x on {ticker} ({secs_left:.0f}s left)")

    for attempt in range(2):  # 2 attempts intentional — late-window 15m trades can't afford long retry loops
        if attempt > 0:
            log.info(f"[live] Market order retry {attempt}/1...")
            await asyncio.sleep(1.0)

        client_order_id = f"kalshi_{int(time.time() * 1000)}_{attempt}"
        body = {
            "ticker": ticker,
            "side": side,
            "type": "market",
            "count": contracts,
            "action": "buy",
            "client_order_id": client_order_id,
        }

        try:
            async with session.post(
                bot_state.KALSHI_BASE_URL + path,
                headers=kalshi_headers("POST", path),
                json=body,
                timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
            ) as resp:
                data = await resp.json()
                http_status = resp.status
        except Exception as exc:
            log.error(f"[live] Order POST failed (attempt {attempt}): {exc}")
            try:
                chk_path = f"/portfolio/positions?ticker={ticker}"
                async with session.get(
                    bot_state.KALSHI_BASE_URL + chk_path,
                    headers=kalshi_headers("GET", chk_path),
                    timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
                ) as chk_resp:
                    chk_data = await chk_resp.json()
                positions = chk_data.get("market_positions") or chk_data.get("positions") or []
                for p in positions:
                    if p.get("ticker") == ticker:
                        held = p.get("position", 0)
                        if (side == "yes" and held > 0) or (side == "no" and held < 0):
                            held_count = abs(held)
                            log.info(f"POST exception but portfolio shows {held_count}x — treating as filled")
                            return {"fill_confirmed": True, "fill_price_cents": price_this_attempt, "order_id": None, "filled_contracts": held_count}
            except Exception as chk_exc:
                log.error(f"Portfolio check after POST exception failed: {chk_exc}")
            continue

        if http_status == 429:
            log.warning(f"[live] Rate-limited (429) on attempt {attempt} — waiting 2s")
            await asyncio.sleep(2.0)
            continue

        if http_status not in (200, 201):
            log.error(f"[live] Order HTTP {http_status}: {data}")
            err_code = (data.get("error") or {}).get("code", "")
            _non_retryable = {
                "insufficient_funds", "authentication_error", "not_found", "forbidden",
                "market_not_open", "market_settled", "market_not_found",
                "order_limit_exceeded", "contract_limit_exceeded", "position_limit_exceeded",
                "invalid_count", "invalid_order", "min_contracts_not_met",
            }
            if err_code in _non_retryable:
                log.error(f"Non-retryable error ({err_code}). Stopping order attempts.")
                _failed_elapsed = seconds_elapsed(market) if market else 0.0
                _failed_ctx = _notify_ctx(
                    asset, ticker, (_failed_elapsed + secs_left) / 60.0,
                    _phase_for_eth(asset, _failed_elapsed),
                )
                await send_telegram(
                    f"<b>🔵 [S2 D3 Hybrid] {_failed_ctx} MARKET ORDER FAILED</b>  —  {err_code}\n"
                    f"{side.upper()}  {contracts}x"
                )
                break
            continue

        order_id = (data.get("order") or {}).get("order_id") or data.get("order_id")
        if not order_id:
            log.error(f"[live] No order_id in response: {data}")
            continue

        post_order  = data.get("order") or data
        post_status = post_order.get("status", "")
        log.info(f"[live] Order {order_id} POST status={post_status!r}")

        # Market orders should not rest, but poll briefly as a safety net
        if post_status in ("resting", "pending"):
            log.warning(f"[live] Market order {order_id} returned {post_status!r} — polling 5s")
            for _pi in range(5):
                await asyncio.sleep(1.0)
                try:
                    _op = f"/portfolio/orders/{order_id}"
                    async with session.get(
                        bot_state.KALSHI_BASE_URL + _op,
                        headers=kalshi_headers("GET", _op),
                        timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
                    ) as _r:
                        _d = await _r.json()
                    _polled = _d.get("order") or _d
                    if _polled.get("status", "") not in ("resting", "pending"):
                        post_order  = _polled
                        post_status = _polled.get("status", "")
                        log.info(f"[live] Order {order_id} status changed to {post_status!r} (poll {_pi+1}/5)")
                        break
                except Exception:
                    break
            if post_status in ("resting", "pending"):
                try:
                    del_path = f"/portfolio/orders/{order_id}"
                    async with session.delete(
                        bot_state.KALSHI_BASE_URL + del_path,
                        headers=kalshi_headers("DELETE", del_path),
                        timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
                    ) as _del_resp:
                        await _del_resp.json()
                except Exception as _de:
                    log.warning(f"[live] Cancel of resting market order {order_id} failed: {_de}")
                continue

        if post_status in ("canceled", "cancelled"):
            _total     = post_order.get("contracts_count") or contracts
            _remaining = post_order.get("remaining_count")
            _remaining = _remaining if _remaining is not None else _total
            _filled    = _total - _remaining
            if _filled > 0:
                _fp_raw      = post_order.get("yes_price", price_this_attempt)
                _fp_canceled = _fp_raw if side == "yes" else (100 - _fp_raw)
                log.info(f"[live] Market order {order_id} partial fill: {_filled}/{_total} @ {_fp_canceled}c")
                _fill_yes_price = _fp_raw
                await _maybe_fill_verification_notify(
                    asset, ticker, side, market, secs_left,
                    _original_strategy_target_c, price_this_attempt,
                    _market_ask_at_post_c, _fill_yes_price,
                )
                _verified = await _verify_order_fill(session, order_id, _filled, ticker, side)
                return {"fill_confirmed": _verified, "fill_price_cents": _fp_canceled, "order_id": order_id, "filled_contracts": _filled}
            log.info(f"[live] Market order {order_id} zero-fill")
            break

        _total     = post_order.get("contracts_count")
        _remaining = post_order.get("remaining_count")
        _fc        = post_order.get("filled_count")
        if _fc is not None:
            filled_count = _fc
        elif _total is not None and _remaining is not None:
            filled_count = _total - _remaining
        elif _total is not None:
            filled_count = _total
        else:
            filled_count = contracts

        if filled_count == 0:
            log.warning(f"[live] Order {order_id} status={post_status!r} but filled_count=0")
            continue

        _fill_yes_price = post_order.get("yes_price", price_this_attempt)
        fill_price = _fill_yes_price if side == "yes" else (100 - _fill_yes_price)
        log.info(f"[live] Order FILLED: {order_id} @ {fill_price}c x{filled_count} status={post_status!r}")
        await _maybe_fill_verification_notify(
            asset, ticker, side, market, secs_left,
            _original_strategy_target_c, price_this_attempt,
            _market_ask_at_post_c, _fill_yes_price,
        )
        _verified = await _verify_order_fill(session, order_id, filled_count, ticker, side)
        return {"fill_confirmed": _verified, "fill_price_cents": fill_price, "order_id": order_id, "filled_contracts": filled_count}

    # Portfolio check — ground truth after all attempts exhausted
    log.warning(f"Market order not confirmed for {ticker} — checking portfolio")
    try:
        pos_path = f"/portfolio/positions?ticker={ticker}"
        async with session.get(
            bot_state.KALSHI_BASE_URL + pos_path,
            headers=kalshi_headers("GET", pos_path),
            timeout=aiohttp.ClientTimeout(total=bot_state.API_TIMEOUT),
        ) as resp:
            pos_data = await resp.json()
        positions = pos_data.get("market_positions") or pos_data.get("positions") or []
        for p in positions:
            if p.get("ticker") == ticker:
                held = p.get("position", 0)
                # _attempted_tickers (set before place_order) prevents matching a prior trade's held position
                if side == "yes" and held > 0:
                    log.info(f"Portfolio check: found YES position {held}x on {ticker} — order DID fill")
                    return {"fill_confirmed": True, "fill_price_cents": entry_price_cents, "order_id": None, "filled_contracts": held}
                if side == "no" and held < 0:
                    held_no = abs(held)
                    log.info(f"Portfolio check: found NO position {held_no}x on {ticker} — order DID fill")
                    return {"fill_confirmed": True, "fill_price_cents": entry_price_cents, "order_id": None, "filled_contracts": held_no}
        log.info(f"Portfolio check: no position found for {ticker}")
    except Exception as exc:
        log.error(f"Portfolio check error: {exc}")

    log.error(f"Market order not filled for {ticker} {side}")
    _nofill_elapsed = seconds_elapsed(market) if market else 0.0
    _nofill_ctx = _notify_ctx(
        asset, ticker, (_nofill_elapsed + secs_left) / 60.0,
        _phase_for_eth(asset, _nofill_elapsed),
    )
    await send_telegram(
        f"<b>🔵 [S2 D3 Hybrid] {_nofill_ctx} MARKET ORDER NOT FILLED</b>  —  no liquidity\n"
        f"{side.upper()} — {'UP' if side == 'yes' else 'DOWN'}  {contracts}x"
    )
    return {"fill_confirmed": False, "fill_price_cents": None, "order_id": None}
