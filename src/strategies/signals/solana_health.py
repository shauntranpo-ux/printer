"""
Solana RPC health check.

Queries a Solana RPC endpoint for getHealth. If RPC is unreachable,
times out, or returns non-"ok" status, we treat the network as UNHEALTHY
and skip SOL trades. Fail-safe: silence = unhealthy.

Results are cached for 30 seconds to avoid hammering the RPC endpoint.

Uses urllib from the stdlib to avoid an httpx runtime dependency.
"""

from __future__ import annotations
import json
import os
import time
import urllib.error
import urllib.request


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

    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getHealth"}).encode("utf-8")
    req = urllib.request.Request(
        SOLANA_RPC_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_SECONDS) as resp:
            if resp.status != 200:
                reason = f"http_{resp.status}"
                _cache.update({"ts": now, "healthy": False, "reason": reason})
                return False, reason
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("result") != "ok":
            reason = f"unhealthy_{data.get('result')}"
            _cache.update({"ts": now, "healthy": False, "reason": reason})
            return False, reason
    except urllib.error.HTTPError as e:
        reason = f"http_{e.code}"
        _cache.update({"ts": now, "healthy": False, "reason": reason})
        return False, reason
    except (urllib.error.URLError, TimeoutError) as e:
        reason = f"rpc_error_{type(e).__name__}"
        _cache.update({"ts": now, "healthy": False, "reason": reason})
        return False, reason
    except (ValueError, KeyError):
        _cache.update({"ts": now, "healthy": False, "reason": "rpc_parse_error"})
        return False, "rpc_parse_error"

    _cache.update({"ts": now, "healthy": True, "reason": "ok"})
    return True, "ok"
