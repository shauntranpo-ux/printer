"""tests/test_kalshi_compat.py - Unit tests for kalshi_compat.py helpers."""
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kalshi_compat import (
    dollars_to_cents,
    extract_fee_cents,
    extract_fill_price_cents,
    extract_order_counts,
    fp_to_int,
)


# fp_to_int

def test_fp_to_int_whole():
    assert fp_to_int("10.00") == 10

def test_fp_to_int_truncates_fraction():
    assert fp_to_int("10.50") == 10
    assert fp_to_int("10.99") == 10

def test_fp_to_int_zero():
    assert fp_to_int("0.00") == 0

def test_fp_to_int_none():
    assert fp_to_int(None) is None

def test_fp_to_int_empty():
    assert fp_to_int("") is None

def test_fp_to_int_unparseable():
    assert fp_to_int("abc") is None

def test_fp_to_int_negative_raises():
    with pytest.raises(ValueError):
        fp_to_int("-1.00")


# dollars_to_cents

def test_dollars_to_cents_basic():
    assert dollars_to_cents("0.5600") == 56

def test_dollars_to_cents_one_dollar():
    assert dollars_to_cents("1.0000") == 100

def test_dollars_to_cents_half_even_lower():
    # 0.5650 = 56.50 cents -> rounds to 56 (banker's rounding: nearest even)
    assert dollars_to_cents("0.5650") == 56

def test_dollars_to_cents_half_even_upper():
    # 0.5750 = 57.50 cents -> rounds to 58 (nearest even)
    assert dollars_to_cents("0.5750") == 58

def test_dollars_to_cents_very_small_rounds_to_zero():
    # 0.0050 = 0.50 cents -> banker's: 0 (nearest even to 0)
    assert dollars_to_cents("0.0050") == 0

def test_dollars_to_cents_one_and_half():
    # 0.0150 = 1.50 cents -> rounds to 2 (nearest even)
    assert dollars_to_cents("0.0150") == 2

def test_dollars_to_cents_none():
    assert dollars_to_cents(None) is None

def test_dollars_to_cents_empty():
    assert dollars_to_cents("") is None

def test_dollars_to_cents_unparseable():
    assert dollars_to_cents("bad") is None


# extract_order_counts

def test_extract_counts_new_fp_fields():
    order = {
        "contracts_count_fp": "5.00",
        "filled_count_fp": "3.00",
        "remaining_count_fp": "2.00",
    }
    c = extract_order_counts(order)
    assert c["total"] == 5
    assert c["filled"] == 3
    assert c["remaining"] == 2

def test_extract_counts_legacy_int_fields():
    order = {
        "contracts_count": 5,
        "filled_count": 3,
        "remaining_count": 2,
    }
    c = extract_order_counts(order)
    assert c["total"] == 5
    assert c["filled"] == 3
    assert c["remaining"] == 2

def test_extract_counts_fp_preferred_over_legacy():
    order = {
        "contracts_count_fp": "7.00",
        "contracts_count": 5,
        "filled_count_fp": "4.00",
        "filled_count": 3,
        "remaining_count_fp": "3.00",
        "remaining_count": 2,
    }
    c = extract_order_counts(order)
    assert c["total"] == 7
    assert c["filled"] == 4
    assert c["remaining"] == 3

def test_extract_counts_all_missing_returns_none():
    c = extract_order_counts({"status": "executed"})
    assert c["total"] is None
    assert c["filled"] is None
    assert c["remaining"] is None

def test_extract_counts_partial_missing():
    order = {"filled_count_fp": "2.00"}
    c = extract_order_counts(order)
    assert c["filled"] == 2
    assert c["total"] is None
    assert c["remaining"] is None

def test_extract_counts_count_fp_fallback_for_total():
    order = {"count_fp": "3.00"}
    c = extract_order_counts(order)
    assert c["total"] == 3


# _verify_order_fill regression: only status field -> must return False

async def test_verify_order_fill_no_count_fields_returns_false():
    """Regression: order with only status='executed' and no count fields must return False."""
    import bot_state
    import bot_market

    # Setup minimal credentials to prevent signing crash
    from cryptography.hazmat.primitives.asymmetric import rsa
    bot_state.private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048
    )
    bot_state.api_key = "test-key"

    order_response = {"order": {"order_id": "ord-1", "status": "executed"}}

    mock_resp = AsyncMock()
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=order_response)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)

    result = await bot_market._verify_order_fill(mock_session, "ord-1", expected_filled=3)
    assert result is False


# extract_fill_price_cents

def test_extract_fill_price_new_dollars_yes():
    fill = {"yes_price_dollars": "0.6000", "no_price_dollars": "0.4000"}
    assert extract_fill_price_cents(fill, "yes") == 60

def test_extract_fill_price_new_dollars_no():
    fill = {"yes_price_dollars": "0.6000", "no_price_dollars": "0.4000"}
    assert extract_fill_price_cents(fill, "no") == 40

def test_extract_fill_price_legacy_yes():
    fill = {"yes_price": 60}
    assert extract_fill_price_cents(fill, "yes") == 60

def test_extract_fill_price_legacy_no():
    fill = {"no_price": 40}
    assert extract_fill_price_cents(fill, "no") == 40

def test_extract_fill_price_dollars_preferred_over_legacy():
    fill = {"yes_price_dollars": "0.5600", "yes_price": 42}
    assert extract_fill_price_cents(fill, "yes") == 56

def test_extract_fill_price_missing_returns_none():
    assert extract_fill_price_cents({}, "yes") is None
    assert extract_fill_price_cents({}, "no") is None


# extract_fee_cents

def test_extract_fee_cents_new_string():
    assert extract_fee_cents({"fee_cost": "0.0700"}) == 7

def test_extract_fee_cents_legacy_int():
    assert extract_fee_cents({"fee_cost": 7}) == 7

def test_extract_fee_cents_missing():
    assert extract_fee_cents({}) is None


# place_order body construction

async def test_place_order_demo_body_ioc_no_type_field():
    """Demo mode: body must have time_in_force=IOC, yes_price=42, no type field, no no_price."""
    import bot_state
    import bot_market
    from cryptography.hazmat.primitives.asymmetric import rsa

    bot_state.private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048
    )
    bot_state.api_key = "test-key"
    bot_state.KALSHI_BASE_URL = bot_state.KALSHI_DEMO_BASE_URL

    fake_ob = {
        "best_yes_ask": 42,
        "best_no_ask": 60,
        "best_yes_bid": 38,
        "yes_liquidity": 100,
        "no_liquidity": 100,
        "obi": 0.0,
    }

    mock_session = MagicMock()

    # GET poll response: order already executed, so the demo poll loop exits on the first
    # check instead of sleeping through the full 30s timeout.
    poll_payload = {
        "order": {
            "order_id": "demo-test-001",
            "status": "executed",
            "contracts_count_fp": "1.00",
            "filled_count_fp": "1.00",
            "remaining_count_fp": "0.00",
            "yes_price_dollars": "0.4200",
            "no_price_dollars": "0.5800",
        }
    }
    poll_resp = AsyncMock()
    poll_resp.__aenter__ = AsyncMock(return_value=poll_resp)
    poll_resp.__aexit__ = AsyncMock(return_value=False)
    poll_resp.status = 200
    poll_resp.json = AsyncMock(return_value=poll_payload)
    mock_session.get = MagicMock(return_value=poll_resp)

    with patch("bot_market.fetch_orderbook", new=AsyncMock(return_value=fake_ob)), \
         patch("asyncio.sleep", new=AsyncMock()):
        with patch.object(mock_session, "post") as mock_post_ctx:
            mock_resp = AsyncMock()
            mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_resp.__aexit__ = AsyncMock(return_value=False)
            mock_resp.status = 200
            order_payload = {
                "order": {
                    "order_id": "demo-test-001",
                    "status": "executed",
                    "contracts_count_fp": "1.00",
                    "filled_count_fp": "1.00",
                    "remaining_count_fp": "0.00",
                    "yes_price_dollars": "0.4200",
                }
            }
            mock_resp.json = AsyncMock(return_value=order_payload)
            mock_post_ctx.return_value = mock_resp

            result = await bot_market.place_order(
                mock_session,
                ticker="KXBTC15M-25Jan01-T93000",
                side="yes",
                contracts=1,
                entry_price_cents=42,
                mode="demo",
            )

    assert mock_post_ctx.called
    call_kwargs = mock_post_ctx.call_args
    sent_body = call_kwargs[1].get("json") or (call_kwargs[0][1] if len(call_kwargs[0]) > 1 else {})

    assert "type" not in sent_body, "type field must not be sent"
    assert sent_body.get("time_in_force") == "immediate_or_cancel"
    assert sent_body.get("yes_price") == 42
    assert "no_price" not in sent_body, "no_price must not be sent for yes side"
