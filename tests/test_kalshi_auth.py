"""tests/test_kalshi_auth.py - Unit tests for auth robustness + order retry headroom."""
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# 1. Clock-skew adjustment: large diff -> bot_state updated

async def test_clock_skew_adjustment():
    """Date header 5000ms in the future -> kalshi_clock_skew_ms set to ~5000."""
    import bot_state
    import bot_market

    old_skew = bot_state.kalshi_clock_skew_ms
    bot_state.kalshi_clock_skew_ms = 0

    import email.utils, time

    future_ts = time.time() + 5.0
    date_hdr = email.utils.formatdate(future_ts, usegmt=True)

    mock_resp = MagicMock()
    mock_resp.headers = {"Date": date_hdr}
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=mock_resp)

    try:
        await bot_market._maybe_adjust_clock_skew(session)
        assert abs(bot_state.kalshi_clock_skew_ms - 5000) < 1000
    finally:
        bot_state.kalshi_clock_skew_ms = old_skew


# 2. Clock-skew within tolerance -> skew stays 0

async def test_clock_skew_within_tolerance():
    """Date header within +/-2s -> kalshi_clock_skew_ms unchanged."""
    import bot_state
    import bot_market
    import email.utils, time

    old_skew = bot_state.kalshi_clock_skew_ms
    bot_state.kalshi_clock_skew_ms = 0

    date_hdr = email.utils.formatdate(time.time() + 0.3, usegmt=True)

    mock_resp = MagicMock()
    mock_resp.headers = {"Date": date_hdr}
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=mock_resp)

    try:
        await bot_market._maybe_adjust_clock_skew(session)
        assert bot_state.kalshi_clock_skew_ms == 0
    finally:
        bot_state.kalshi_clock_skew_ms = old_skew


# 3. Demo fallback: missing creds -> bot disabled, no live creds loaded

def test_demo_fallback_no_live_creds():
    """Missing KALSHI_DEMO_API_KEY -> api_key="", private_key=None, bot_enabled=False."""
    import bot_state
    import bot_market

    old_api_key = bot_state.api_key
    old_pkey = bot_state.private_key
    old_flag = bot_state.demo_fallback_alert

    env = {
        "KALSHI_DEMO_API_KEY": "",
        "KALSHI_DEMO_PRIVATE_KEY": "",
    }
    with patch.dict(os.environ, env, clear=False):
        with patch("bot_market.read_config", return_value={"mode": "demo", "bot_enabled": True}):
            with patch("bot_market.write_config") as mock_wc:
                bot_market.load_credentials("demo")

    try:
        assert bot_state.api_key == ""
        assert bot_state.private_key is None
        assert bot_state.demo_fallback_alert is True
        # write_config must have been called with bot_enabled=False
        written = mock_wc.call_args[0][0]
        assert written.get("bot_enabled") is False
    finally:
        bot_state.api_key = old_api_key
        bot_state.private_key = old_pkey
        bot_state.demo_fallback_alert = old_flag


# 4. place_order safety guard: live mode with demo URL -> refuse

async def test_place_order_safety_guard_live_with_demo_url():
    """place_order mode=live but BASE_URL=demo -> fill_confirmed=False, no HTTP calls."""
    import bot_state
    import bot_market

    old_url = bot_state.KALSHI_BASE_URL
    bot_state.KALSHI_BASE_URL = bot_state.KALSHI_DEMO_BASE_URL

    session = MagicMock()
    session.post = MagicMock(side_effect=AssertionError("HTTP must not be called"))

    try:
        result = await bot_market.place_order(
            session, ticker="KXBTC15M-test", side="yes",
            contracts=1, entry_price_cents=50, mode="live",
        )
        assert result["fill_confirmed"] is False
        session.post.assert_not_called()
    finally:
        bot_state.KALSHI_BASE_URL = old_url


# 5. 429 exponential backoff: 3 retries then success

async def test_429_exponential_backoff():
    """Three 429 responses then 200 -> asyncio.sleep called with [1, 2, 4]."""
    import bot_market
    import bot_state

    old_url = bot_state.KALSHI_BASE_URL
    bot_state.KALSHI_BASE_URL = bot_state.KALSHI_LIVE_BASE_URL

    def _make_resp(status, body, headers=None):
        r = MagicMock()
        r.status = status
        r.headers = headers or {}
        r.json = AsyncMock(return_value=body)
        r.__aenter__ = AsyncMock(return_value=r)
        r.__aexit__ = AsyncMock(return_value=False)
        return r

    fill_body = {
        "order": {
            "order_id": "ord-abc",
            "status": "executed",
            "contracts_count_fp": "1.00",
            "filled_count_fp": "1.00",
            "remaining_count_fp": "0.00",
            "yes_price_dollars": "0.5000",
        }
    }
    session = MagicMock()
    session.post = MagicMock(side_effect=[
        _make_resp(429, {}, {}),
        _make_resp(429, {}, {}),
        _make_resp(429, {}, {}),
        _make_resp(200, fill_body),
    ])

    sleep_calls = []

    fake_ob = {
        "best_yes_ask": 50, "best_no_ask": 52,
        "best_yes_bid": 48, "yes_liquidity": 100, "no_liquidity": 100, "obi": 0.0,
    }

    with patch("bot_market.fetch_orderbook", new=AsyncMock(return_value=fake_ob)):
        with patch("bot_market.kalshi_headers", return_value={}):
            with patch("bot_market._verify_order_fill", new=AsyncMock(return_value=True)):
                with patch("bot_market._maybe_fill_verification_notify", new=AsyncMock()):
                    with patch("asyncio.sleep", new=AsyncMock(side_effect=lambda s: sleep_calls.append(s))):
                        try:
                            await bot_market.place_order(
                                session, ticker="KXBTC15M-test", side="yes",
                                contracts=1, entry_price_cents=50, mode="live",
                            )
                        except Exception:
                            pass

    bot_state.KALSHI_BASE_URL = old_url
    assert sleep_calls == [1, 2, 4], f"Expected [1, 2, 4], got {sleep_calls}"


# 6. 5xx backoff: 2 server errors then 200

async def test_5xx_backoff():
    """Two 503 responses then 200 -> asyncio.sleep called with [1, 2]."""
    import bot_market
    import bot_state

    old_url = bot_state.KALSHI_BASE_URL
    bot_state.KALSHI_BASE_URL = bot_state.KALSHI_LIVE_BASE_URL

    def _make_resp(status, body):
        r = MagicMock()
        r.status = status
        r.headers = {}
        r.json = AsyncMock(return_value=body)
        r.__aenter__ = AsyncMock(return_value=r)
        r.__aexit__ = AsyncMock(return_value=False)
        return r

    fill_body = {
        "order": {
            "order_id": "ord-xyz",
            "status": "executed",
            "contracts_count_fp": "1.00",
            "filled_count_fp": "1.00",
            "remaining_count_fp": "0.00",
            "yes_price_dollars": "0.5000",
        }
    }
    session = MagicMock()
    session.post = MagicMock(side_effect=[
        _make_resp(503, {}),
        _make_resp(503, {}),
        _make_resp(200, fill_body),
    ])

    sleep_calls = []
    fake_ob = {
        "best_yes_ask": 50, "best_no_ask": 52,
        "best_yes_bid": 48, "yes_liquidity": 100, "no_liquidity": 100, "obi": 0.0,
    }

    with patch("bot_market.fetch_orderbook", new=AsyncMock(return_value=fake_ob)):
        with patch("bot_market.kalshi_headers", return_value={}):
            with patch("bot_market._verify_order_fill", new=AsyncMock(return_value=True)):
                with patch("bot_market._maybe_fill_verification_notify", new=AsyncMock()):
                    with patch("asyncio.sleep", new=AsyncMock(side_effect=lambda s: sleep_calls.append(s))):
                        try:
                            await bot_market.place_order(
                                session, ticker="KXBTC15M-test", side="yes",
                                contracts=1, entry_price_cents=50, mode="live",
                            )
                        except Exception:
                            pass

    bot_state.KALSHI_BASE_URL = old_url
    assert sleep_calls == [1, 2], f"Expected [1, 2], got {sleep_calls}"


# ── 7. Auth failure alert cooldown: second call within window -> no double fire ─

async def test_auth_failure_alert_cooldown():
    """_alert_auth_failure called twice within 60s -> send_telegram called once."""
    import bot_market

    old_ts = bot_market._last_auth_alert_ts
    bot_market._last_auth_alert_ts = 0.0

    try:
        with patch("bot_market.send_telegram", new=AsyncMock()) as mock_tg:
            await bot_market._alert_auth_failure(401, "/portfolio/orders", "unauthorized")
            await bot_market._alert_auth_failure(401, "/portfolio/orders", "unauthorized")
        mock_tg.assert_called_once()
    finally:
        bot_market._last_auth_alert_ts = old_ts
