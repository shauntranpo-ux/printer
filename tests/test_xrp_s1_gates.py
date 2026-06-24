# tests/test_xrp_s1_gates.py
"""Regression tests: XRP must be disabled in S1 by default; re-enable only via explicit config."""
import sys, os, time
from collections import deque
from unittest.mock import patch
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
import asset_manager
from bot_strategy import strategy_brain_s1


def _make_prices(base: float = 0.60, n: int = 63):
    now = time.time()
    return [(now - (n - i) * 10, base) for i in range(n)]


def _run_s1_xrp(config_overrides: dict | None = None) -> dict:
    config = {"mode": "paper", "bot_enabled": True, **(config_overrides or {})}
    prices = _make_prices(base=0.60)
    patches = [
        patch("bot_strategy.read_config", return_value=config),
        patch("bot_strategy._is_quiet_hours", return_value=False),
        patch.object(bot_state, "_s1_pending_trades", {}),
        patch.object(bot_state, "_s1_asset_trade_times", {}),
        patch.object(bot_state, "_s1_cooldown_until", {}),
        patch.object(bot_state, "_s1_consec_losses_by_asset", {}),
        patch.dict(asset_manager._prices, {"XRP": deque(prices)}),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return strategy_brain_s1(
            btc_price=0.60,
            strike=0.603,
            yes_ask=38.0,
            no_ask=57.0,
            elapsed_seconds=200.0,
            secs_left=400.0,
            ticker="KXXRP15M-TEST",
            asset="XRP",
        )


def test_xrp_disabled_by_default():
    """XRP S1 must return skip with 's1_xrp_disabled' reason when no config override."""
    result = _run_s1_xrp()
    assert result["action"] == "skip", (
        f"XRP S1 must be disabled by default. Got action={result['action']}"
    )
    assert result.get("reasoning") == "s1_xrp_disabled", (
        f"Expected reasoning='s1_xrp_disabled', got: {result.get('reasoning')}"
    )


def test_xrp_skip_fires_before_quiet_hours():
    """XRP disable gate fires before quiet hours check — even during quiet hours."""
    result = _run_s1_xrp()
    assert result.get("reasoning") == "s1_xrp_disabled", (
        f"XRP disable must fire first (not s1_quiet_hours). Got: {result.get('reasoning')}"
    )


def test_xrp_enabled_via_config_bypasses_disable():
    """When s1_xrp_enabled=True in config, XRP disable gate must not fire."""
    result = _run_s1_xrp(config_overrides={"s1_xrp_enabled": True})
    assert result.get("reasoning") != "s1_xrp_disabled", (
        f"With s1_xrp_enabled=True, XRP should not be blocked by disable gate. "
        f"Got: {result.get('reasoning')}"
    )


def test_xrp_disable_gate_exists_in_source():
    """Source-level: XRP disable gate must be in strategy_brain_s1 source."""
    import inspect
    src = inspect.getsource(strategy_brain_s1)
    assert "s1_xrp_enabled" in src, (
        "s1_xrp_enabled config check missing from strategy_brain_s1 source"
    )
    assert "s1_xrp_disabled" in src, (
        "s1_xrp_disabled skip reason missing from strategy_brain_s1 source"
    )
