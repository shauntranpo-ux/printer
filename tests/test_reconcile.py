"""tests/test_reconcile.py — Unit tests for reconcile.py crash-recovery helpers."""
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── helpers ───────────────────────────────────────────────────────────────────

class _Row:
    """Minimal sqlite3.Row substitute for testing classify_pending_trade."""
    def __init__(self, **kwargs):
        self._d = kwargs

    def __getitem__(self, key):
        return self._d.get(key)


def _make_row(**overrides):
    defaults = dict(
        market_id="KXBTC15M-24Nov21-T93499",
        side="yes",
        contracts=3,
        entry_price_cents=60,
        ts="2024-01-01T00:00:00Z",
        order_id="order-123",
        mode="live",
        asset="BTC",
    )
    defaults.update(overrides)
    return _Row(**defaults)


# ── 1. Paper mode → mark_expired_unfilled, no Kalshi calls ───────────────────

async def test_classify_paper_mode_no_kalshi_calls():
    """Paper mode returns mark_expired_unfilled with PnL=0; Kalshi must not be called."""
    from reconcile import classify_pending_trade

    # session.get raises if called — proves no Kalshi call made
    session = MagicMock()
    session.get = MagicMock(side_effect=AssertionError("Kalshi called in paper mode"))

    row = _make_row()
    result = await classify_pending_trade(session, row, "paper")

    assert result["action"] == "mark_expired_unfilled"
    assert result["pnl_dollars"] == 0.0
    session.get.assert_not_called()


# ── 2. Live mode with matching fill → mark_filled with realized PnL ───────────

async def test_classify_live_with_fill_win():
    """Live: matching YES fill on a resolved_yes market → win with correct PnL."""
    from reconcile import classify_pending_trade

    session = AsyncMock()
    row = _make_row(side="yes", contracts=3, entry_price_cents=60)

    with patch("reconcile.fetch_market_resolution", new=AsyncMock(return_value="resolved_yes")):
        with patch("reconcile.fetch_fills_for_ticker", new=AsyncMock(return_value=[
            {"side": "yes", "yes_price": 60, "count": 3, "created_time": "2024-01-01T00:01:00Z"},
        ])):
            result = await classify_pending_trade(session, row, "live")

    assert result["action"] == "mark_filled"
    assert result["outcome"] == "win"
    assert result["exit_price_cents"] == 100
    # PnL = (100 - 60) * 3 / 100 = 1.20
    assert abs(result["pnl_dollars"] - 1.20) < 0.01
    assert result["fill_confirmed"] is True


async def test_classify_live_with_fill_loss():
    """Live: YES fill on resolved_no market → loss with negative PnL."""
    from reconcile import classify_pending_trade

    session = AsyncMock()
    row = _make_row(side="yes", contracts=2, entry_price_cents=70)

    with patch("reconcile.fetch_market_resolution", new=AsyncMock(return_value="resolved_no")):
        with patch("reconcile.fetch_fills_for_ticker", new=AsyncMock(return_value=[
            {"side": "yes", "yes_price": 70, "count": 2, "created_time": "2024-01-01T00:01:00Z"},
        ])):
            result = await classify_pending_trade(session, row, "live")

    assert result["action"] == "mark_filled"
    assert result["outcome"] == "loss"
    assert result["exit_price_cents"] == 0
    # PnL = (0 - 70) * 2 / 100 = -1.40
    assert abs(result["pnl_dollars"] - (-1.40)) < 0.01


# ── 3. No fill + resolved market → mark_phantom ──────────────────────────────

async def test_classify_no_fill_resolved_market():
    """Live: no fill, resolved market, no open position → mark_phantom."""
    from reconcile import classify_pending_trade

    session = AsyncMock()
    row = _make_row()

    with patch("reconcile.fetch_market_resolution", new=AsyncMock(return_value="resolved_no")):
        with patch("reconcile.fetch_fills_for_ticker", new=AsyncMock(return_value=[])):
            with patch("reconcile.fetch_open_positions", new=AsyncMock(return_value={})):
                result = await classify_pending_trade(session, row, "live")

    assert result["action"] == "mark_phantom"
    assert "reason" in result


# ── 4. Open market → leave_pending; fills not queried ────────────────────────

async def test_classify_open_market_leaves_pending():
    """Live: market still open → leave_pending; fills endpoint not called."""
    from reconcile import classify_pending_trade

    session = AsyncMock()
    row = _make_row()

    with patch("reconcile.fetch_market_resolution", new=AsyncMock(return_value="open")):
        with patch("reconcile.fetch_fills_for_ticker") as mock_fills:
            result = await classify_pending_trade(session, row, "live")

    assert result["action"] == "leave_pending"
    mock_fills.assert_not_called()


# ── 5. Position verification: empty Kalshi → clears current_position ──────────

async def test_verify_positions_clears_when_kalshi_empty():
    """
    _verify_and_restore_positions with fetch_open_positions={} must clear
    current_position and current_phase when state file claims a LOCKED position.
    """
    import bot_state
    import bot_loops

    saved_pos = {
        "trade_id": 99,
        "ticker": "KXBTC15M-24Nov21-T93499",
        "side": "yes",
        "contracts": 3,
        "market_close_time": "",
    }

    old_pos = bot_state.current_position
    old_phase = bot_state.current_phase
    bot_state.current_position = None
    bot_state.current_phase = ""

    session = AsyncMock()
    try:
        with patch("bot_loops.fetch_open_positions", new=AsyncMock(return_value={})):
            with patch("bot_loops.send_telegram", new=AsyncMock(return_value=None)):
                await bot_loops._verify_and_restore_positions(
                    session, saved_pos, "LOCKED", {}, "live"
                )
        assert bot_state.current_position is None
        assert bot_state.current_phase == ""
    finally:
        bot_state.current_position = old_pos
        bot_state.current_phase = old_phase
