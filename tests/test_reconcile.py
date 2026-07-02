"""tests/test_reconcile.py - Unit tests for reconcile.py crash-recovery helpers."""
import os
import sqlite3
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# helpers

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


# 1. Paper mode -> mark_expired_unfilled, no Kalshi calls

async def test_classify_paper_mode_no_kalshi_calls():
    """Paper mode returns mark_expired_unfilled with PnL=0; Kalshi must not be called."""
    from reconcile import classify_pending_trade

    # session.get raises if called - proves no Kalshi call made
    session = MagicMock()
    session.get = MagicMock(side_effect=AssertionError("Kalshi called in paper mode"))

    row = _make_row()
    result = await classify_pending_trade(session, row, "paper")

    assert result["action"] == "mark_expired_unfilled"
    assert result["pnl_dollars"] == 0.0
    session.get.assert_not_called()


# 2. Live mode with matching fill -> mark_filled with realized PnL

async def test_classify_live_with_fill_win():
    """Live: matching YES fill on a resolved_yes market -> win with correct PnL."""
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
    """Live: YES fill on resolved_no market -> loss with negative PnL."""
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


# 3. No fill + resolved market -> mark_phantom

async def test_classify_no_fill_resolved_market():
    """Live: no fill, resolved market, no open position -> mark_phantom."""
    from reconcile import classify_pending_trade

    session = AsyncMock()
    row = _make_row()

    with patch("reconcile.fetch_market_resolution", new=AsyncMock(return_value="resolved_no")):
        with patch("reconcile.fetch_fills_for_ticker", new=AsyncMock(return_value=[])):
            with patch("reconcile.fetch_open_positions", new=AsyncMock(return_value={})):
                result = await classify_pending_trade(session, row, "live")

    assert result["action"] == "mark_phantom"
    assert "reason" in result


# 4. Open market -> leave_pending; fills not queried

async def test_classify_open_market_leaves_pending():
    """Live: market still open -> leave_pending; fills endpoint not called."""
    from reconcile import classify_pending_trade

    session = AsyncMock()
    row = _make_row()

    with patch("reconcile.fetch_market_resolution", new=AsyncMock(return_value="open")):
        with patch("reconcile.fetch_fills_for_ticker") as mock_fills:
            result = await classify_pending_trade(session, row, "live")

    assert result["action"] == "leave_pending"
    mock_fills.assert_not_called()


# 5. Position verification: empty Kalshi -> clears current_position

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


# 6. Specific PnL: fill at 42c, 10 contracts, resolved_yes -> 5.80

async def test_classify_resolved_with_fills():
    """entry_fill=42c, 10 contracts, resolved_yes -> pnl=(100-42)*10/100=5.80."""
    from reconcile import classify_pending_trade

    session = AsyncMock()
    row = _make_row(side="yes", contracts=10, entry_price_cents=42)

    with patch("reconcile.fetch_market_resolution", new=AsyncMock(return_value="resolved_yes")):
        with patch("reconcile.fetch_fills_for_ticker", new=AsyncMock(return_value=[
            {"side": "yes", "yes_price": 42, "created_time": "2024-01-01T00:01:00Z"},
        ])):
            result = await classify_pending_trade(session, row, "live")

    assert result["action"] == "mark_filled"
    assert result["outcome"] == "win"
    assert result["exit_price_cents"] == 100
    assert abs(result["pnl_dollars"] - 5.80) < 0.01


# 7. Position still open after market close -> leave_pending

async def test_classify_position_still_held():
    """Market settled, no fills, but position still on Kalshi -> leave_pending."""
    from reconcile import classify_pending_trade

    session = AsyncMock()
    row = _make_row(market_id="KXBTC15M-24Nov21-T93499")

    with patch("reconcile.fetch_market_resolution", new=AsyncMock(return_value="settled")):
        with patch("reconcile.fetch_fills_for_ticker", new=AsyncMock(return_value=[])):
            with patch("reconcile.fetch_open_positions", new=AsyncMock(return_value={
                "KXBTC15M-24Nov21-T93499": {"side": "yes", "count": 3},
            })):
                result = await classify_pending_trade(session, row, "live")

    assert result["action"] == "leave_pending"


# 8. Pagination: cursor followed, both pages aggregated

async def test_classify_handles_fills_pagination():
    """fetch_fills_for_ticker follows cursor and returns fills from all pages."""
    from reconcile import fetch_fills_for_ticker

    page1 = {
        "fills": [{"side": "yes", "yes_price": 60, "created_time": "2024-01-01T01:00:00Z"}],
        "cursor": "tok-page2",
    }
    page2 = {
        "fills": [{"side": "yes", "yes_price": 58, "created_time": "2024-01-01T02:00:00Z"}],
    }

    def _ctx(data):
        r = MagicMock()
        r.status = 200
        r.json = AsyncMock(return_value=data)
        r.__aenter__ = AsyncMock(return_value=r)
        r.__aexit__ = AsyncMock(return_value=False)
        return r

    session = MagicMock()
    session.get = MagicMock(side_effect=[_ctx(page1), _ctx(page2)])

    with patch("reconcile.kalshi_headers", return_value={}):
        fills = await fetch_fills_for_ticker(session, "KXBTC15M-test", 0)

    assert len(fills) == 2
    prices = {f.get("yes_price") for f in fills}
    assert prices == {60, 58}
    assert session.get.call_count == 2


# 9. All Kalshi calls error -> leave_pending, no exception bubbles

async def test_classify_all_kalshi_errors():
    """All Kalshi helpers raise internally -> classify returns leave_pending."""
    from reconcile import classify_pending_trade

    def _raising_ctx(*_a, **_kw):
        raise ConnectionError("network down")

    session = MagicMock()
    session.get = MagicMock(side_effect=_raising_ctx)

    row = _make_row()

    with patch("reconcile.kalshi_headers", return_value={}):
        result = await classify_pending_trade(session, row, "live")

    # fetch_market_resolution -> "unknown"; fills -> []; positions -> None -> leave_pending
    assert result["action"] == "leave_pending"


# 10. Count mismatch: saved=5, kalshi=3 -> contracts=3, telegram called

async def test_count_mismatch_warning():
    """Position count mismatch: saved=5, kalshi=3 -> contracts updated, telegram fired."""
    import bot_state
    import bot_loops

    saved_pos = {
        "trade_id": 55,
        "ticker": "KXBTC15M-24Nov21-T93499",
        "side": "yes",
        "contracts": 5,
        "market_close_time": "",
    }

    old_pos   = bot_state.current_position
    old_phase = bot_state.current_phase
    bot_state.current_position = None
    bot_state.current_phase    = ""

    session = AsyncMock()
    try:
        with patch("bot_loops.fetch_open_positions", new=AsyncMock(return_value={
            "KXBTC15M-24Nov21-T93499": {"side": "yes", "count": 3},
        })):
            with patch("bot_loops.send_telegram", new=AsyncMock()) as mock_tg:
                await bot_loops._verify_and_restore_positions(
                    session, saved_pos, "LOCKED", {}, "live"
                )

        assert bot_state.current_position is not None
        assert bot_state.current_position["contracts"] == 3
        assert bot_state.current_phase == "LOCKED"
        mock_tg.assert_called_once()
    finally:
        bot_state.current_position = old_pos
        bot_state.current_phase    = old_phase


# 11. Ticker missing from Kalshi -> position cleared

async def test_position_missing_from_kalshi():
    """Saved ticker absent in Kalshi positions -> current_position cleared."""
    import bot_state
    import bot_loops

    saved_pos = {
        "trade_id": 77,
        "ticker": "KXBTC15M-24Nov21-T99999",
        "side": "yes",
        "contracts": 2,
        "market_close_time": "",
    }

    old_pos   = bot_state.current_position
    old_phase = bot_state.current_phase
    bot_state.current_position = None
    bot_state.current_phase    = ""

    session = AsyncMock()
    try:
        with patch("bot_loops.fetch_open_positions", new=AsyncMock(return_value={
            "KXBTC15M-OTHER": {"side": "yes", "count": 2},  # different ticker
        })):
            with patch("bot_loops.send_telegram", new=AsyncMock()):
                await bot_loops._verify_and_restore_positions(
                    session, saved_pos, "LOCKED", {}, "live"
                )

        assert bot_state.current_position is None
        assert bot_state.current_phase == ""
    finally:
        bot_state.current_position = old_pos
        bot_state.current_phase    = old_phase


# 12. Idempotency: second reconcile pass touches zero rows

async def test_idempotency():
    """Running startup reconcile twice: second pass produces zero DB updates."""
    import bot_state
    from bot import _startup_reconcile

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                market_id TEXT, side TEXT, contracts INTEGER,
                entry_price_cents INTEGER, ts TEXT, order_id TEXT,
                mode TEXT, asset TEXT,
                outcome TEXT, exit_price_cents INTEGER,
                pnl_dollars REAL, fill_confirmed INTEGER
            )
        """)
        conn.execute(
            "INSERT INTO trades (market_id, side, contracts, entry_price_cents, ts, "
            "order_id, mode, asset, outcome) VALUES (?,?,?,?,?,?,?,?,?)",
            ("KXBTC15M-24Nov21-T93499", "yes", 2, 60,
             "2024-01-01T00:00:00Z", "order-idem", "live", "BTC", "pending"),
        )
        conn.commit()
        conn.close()

        old_db = bot_state._DB_FILE
        bot_state._DB_FILE = db_path
        session = AsyncMock()

        try:
            phantom = {"action": "mark_phantom", "reason": "idem test"}
            with patch("bot.classify_pending_trade", new=AsyncMock(return_value=phantom)):
                with patch("bot.send_telegram", new=AsyncMock()):
                    await _startup_reconcile(session, "live")

            conn = sqlite3.connect(db_path)
            outcome = conn.execute("SELECT outcome FROM trades WHERE id=1").fetchone()[0]
            conn.close()
            assert outcome == "phantom"

            # Second pass - no pending rows remain, classify must never be called.
            with patch("bot.classify_pending_trade") as mock_classify:
                with patch("bot.send_telegram", new=AsyncMock()):
                    await _startup_reconcile(session, "live")
            mock_classify.assert_not_called()

        finally:
            bot_state._DB_FILE = old_db

    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass
