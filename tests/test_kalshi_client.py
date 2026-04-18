import os
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import Response

from kalshi_botv3.kalshi.auth import KalshiSigner
from kalshi_botv3.kalshi.client import HttpKalshiClient, MockKalshiClient
from kalshi_botv3.kalshi.models import OrderSide, OrderStatus
from kalshi_botv3.kalshi.ticker_map import build_current_window_ticker, parse_ticker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_temp_rsa_key(tmp_path: Path) -> tuple[Path, rsa.RSAPrivateKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path = tmp_path / "test_key.pem"
    key_path.write_bytes(pem)
    return key_path, key


def _make_signer(tmp_path: Path) -> KalshiSigner:
    key_path, _ = _make_temp_rsa_key(tmp_path)
    return KalshiSigner(key_path, api_key_id="test-key-id")


# ---------------------------------------------------------------------------
# test_signer_produces_valid_signature
# ---------------------------------------------------------------------------


def test_signer_produces_valid_signature(tmp_path: Path) -> None:
    from base64 import b64decode

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    key_path, private_key = _make_temp_rsa_key(tmp_path)
    signer = KalshiSigner(key_path)

    ts = int(time.time() * 1000)
    sig_b64 = signer.sign(ts, "GET", "/trade-api/v2/exchange/status")
    sig_bytes = b64decode(sig_b64)

    message = f"{ts}GET/trade-api/v2/exchange/status".encode()
    public_key = private_key.public_key()
    public_key.verify(
        sig_bytes,
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=hashes.SHA256.digest_size,
        ),
        hashes.SHA256(),
    )


# ---------------------------------------------------------------------------
# test_mock_client_place_order_roundtrip
# ---------------------------------------------------------------------------


async def test_mock_client_place_order_roundtrip() -> None:
    async with MockKalshiClient() as client:
        resp = await client.place_order(
            ticker="KXBTC15M-26APR171500",
            side=OrderSide.YES,
            count=2,
            price_cents=55,
            client_order_id="test-coid-1",
        )

        assert resp.order_id.startswith("mock-")
        assert resp.client_order_id == "test-coid-1"
        assert resp.side == OrderSide.YES
        assert resp.count == 2
        assert resp.yes_price == 55

        fills = await client.get_fills("KXBTC15M-26APR171500")
        assert len(fills) == 1
        assert fills[0].count == 2

        canceled = await client.cancel_order(resp.order_id)
        assert canceled.status == OrderStatus.CANCELED


# ---------------------------------------------------------------------------
# test_http_client_retries_on_500
# ---------------------------------------------------------------------------


async def test_http_client_retries_on_500(tmp_path: Path) -> None:
    signer = _make_signer(tmp_path)
    call_count = 0

    with respx.mock(base_url="https://demo-api.kalshi.co") as mock:
        def handler(request: httpx.Request) -> Response:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return Response(500, json={"error": "server error"})
            return Response(200, json={"exchange_status": {"trading_active": True, "exchange_active": True}})

        mock.get("/trade-api/v2/exchange/status").mock(side_effect=handler)

        async with HttpKalshiClient(signer, "demo") as client:
            status = await client.get_exchange_status()

    assert call_count == 3
    assert status.trading_active is True


# ---------------------------------------------------------------------------
# test_http_client_honors_429_retry_after
# ---------------------------------------------------------------------------


async def test_http_client_honors_429_retry_after(tmp_path: Path) -> None:
    signer = _make_signer(tmp_path)
    call_count = 0

    with respx.mock(base_url="https://demo-api.kalshi.co") as mock:
        def handler(request: httpx.Request) -> Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return Response(429, headers={"Retry-After": "0"}, json={"error": "rate limited"})
            return Response(200, json={"exchange_status": {"trading_active": True, "exchange_active": True}})

        mock.get("/trade-api/v2/exchange/status").mock(side_effect=handler)

        async with HttpKalshiClient(signer, "demo") as client:
            status = await client.get_exchange_status()

    assert call_count == 2
    assert status.trading_active is True


# ---------------------------------------------------------------------------
# test_factory_returns_mock_in_dry_run
# ---------------------------------------------------------------------------


def test_factory_returns_mock_in_dry_run(tmp_path: Path) -> None:
    key_path, _ = _make_temp_rsa_key(tmp_path)
    env = {
        "KALSHI_API_KEY_ID": "test-key-id",
        "KALSHI_PRIVATE_KEY_PATH": str(key_path),
        "MODE": "dry_run",
        "KALSHI_ENV": "demo",
    }
    with patch.dict(os.environ, env, clear=False):
        import importlib
        from importlib import import_module

        import kalshi_botv3.config.settings as settings_mod

        settings_mod.get_settings.cache_clear()
        factory_mod = import_module("kalshi_botv3.kalshi.factory")
        importlib.reload(factory_mod)

        client = factory_mod.build_kalshi_client()
        assert isinstance(client, MockKalshiClient)
        settings_mod.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# test_factory_returns_http_in_live
# ---------------------------------------------------------------------------


def test_factory_returns_http_in_live(tmp_path: Path) -> None:
    key_path, _ = _make_temp_rsa_key(tmp_path)
    env = {
        "KALSHI_API_KEY_ID": "real-key-id",
        "KALSHI_PRIVATE_KEY_PATH": str(key_path),
        "MODE": "live",
        "KALSHI_ENV": "prod",
    }
    with patch.dict(os.environ, env, clear=False):
        import kalshi_botv3.config.settings as settings_mod
        import kalshi_botv3.kalshi.auth as auth_mod

        settings_mod.get_settings.cache_clear()
        auth_mod.get_signer.cache_clear()

        import importlib

        import kalshi_botv3.kalshi.factory as factory_mod
        importlib.reload(factory_mod)

        client = factory_mod.build_kalshi_client()
        assert isinstance(client, HttpKalshiClient)

        settings_mod.get_settings.cache_clear()
        auth_mod.get_signer.cache_clear()


# ---------------------------------------------------------------------------
# test_ticker_roundtrip
# ---------------------------------------------------------------------------


def test_ticker_roundtrip() -> None:
    # BTC window at 2026-04-17 21:00 UTC = 17:00 EDT (UTC-4)
    window_start = datetime(2026, 4, 17, 21, 0, 0, tzinfo=UTC)
    ticker = build_current_window_ticker("BTC", window_start)
    assert ticker == "KXBTC15M-26APR171700"

    market, parsed_utc = parse_ticker(ticker)
    assert market == "BTC"
    assert parsed_utc == window_start
