"""
Solana RPC health check.

Queries a Solana RPC endpoint for getHealth. If RPC is unreachable,
times out, or returns non-"ok" status, we treat the network as UNHEALTHY
and skip SOL trades. Fail-safe: silence = unhealthy.

Results are cached for 30 seconds to avoid hammering the RPC endpoint.
"""

from __future__ import annotations
import os
import time

import httpx


SOLANA_RPC_URL = os.environ.get(
    "SOLANA_RPC_URL",
    "https://api.mainnet-beta.solana.com",
)
_CACHE_TTL_SECONDS = 30
_REQUEST_TIMEOUT_SECONDS = 2.0

_cache: dict = {"ts": 0.0, "healthy": False, "reason": "not_yet_checked"}


def check_solana_health(force: bool = False) -> tuple[bool, str]:
    """
    Returns (is_healthy, reason).

    Fail-safe: returns (False, reason) on any error or timeout.
    Cached for _CACHE_TTL_SECONDS.
    """
    now = time.time()
    if not force and (now - _cache["ts"]) < _CACHE_TTL_SECONDS:
        return _cache["healthy"], _cache["reason"]

    try:
        resp = httpx.post(
            SOLANA_RPC_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": "getHealth"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        if resp.status_code != 200:
            reason = f"http_{resp.status_code}"
            _cache.update({"ts": now, "healthy": False, "reason": reason})
            return False, reason
        data = resp.json()
        if data.get("result") != "ok":
            reason = f"unhealthy_{data.get('result')}"
            _cache.update({"ts": now, "healthy": False, "reason": reason})
            return False, reason
    except httpx.TimeoutException:
        _cache.update({"ts": now, "healthy": False, "reason": "rpc_timeout"})
        return False, "rpc_timeout"
    except httpx.HTTPError as e:
        reason = f"rpc_error_{type(e).__name__}"
        _cache.update({"ts": now, "healthy": False, "reason": reason})
        return False, reason
    except (ValueError, KeyError):
        _cache.update({"ts": now, "healthy": False, "reason": "rpc_parse_error"})
        return False, "rpc_parse_error"

    _cache.update({"ts": now, "healthy": True, "reason": "ok"})
    return True, "ok"

