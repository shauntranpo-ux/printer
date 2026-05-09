"""Tests for dual brain isolation and strategy fixes."""


def test_s2_winprob_no_inflation():
    """win_prob must equal base_p — no vel_adj or obi_adj added."""
    import bot_strategy as bs
    import inspect
    src = inspect.getsource(bs.strategy_brain_s2)
    assert "vel_adj" not in src or ("win_prob" in src and "base_p" in src), \
        "Strategy source check failed"
    # The key check: win_prob should not add vel_adj or obi_adj
    # Check that the line "win_prob = min(0.99, base_p + vel_adj" does NOT appear
    assert "base_p + vel_adj" not in src, \
        "win_prob still inflated with vel_adj"
    assert "base_p + obi_adj" not in src, \
        "win_prob still inflated with obi_adj"


def test_s2_fee_reads_from_config():
    """S2 fee must not be hardcoded 0.07."""
    import bot_strategy as bs
    import inspect
    src = inspect.getsource(bs.strategy_brain_s2)
    assert "0.07 * _ep_s2" not in src, \
        "S2 fee still hardcoded as 0.07"
    assert "kalshi_fee_per_contract_cents" in src, \
        "S2 fee not reading from config key kalshi_fee_per_contract_cents"


def test_s2_price_cap_per_asset():
    """S2 must use get_asset_config for max_entry_price_cents."""
    import bot_strategy as bs
    import inspect
    src = inspect.getsource(bs.strategy_brain_s2)
    assert "get_asset_config" in src, \
        "strategy_brain_s2 must call get_asset_config for per-asset price cap"
import os
import sys
import sqlite3
import asyncio
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot_state
import bot_infra


def _tmp_db():
    """Create a temp DB path and point bot_state at it."""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    bot_state._DB_FILE = f.name
    return f.name


def test_brain_column_exists_after_init_db():
    db_path = _tmp_db()
    try:
        bot_infra.init_db()
        conn = sqlite3.connect(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}
        conn.close()
        assert "brain" in cols, f"brain column missing from trades; found: {cols}"
    finally:
        os.unlink(db_path)


def test_db_write_trade_stores_brain():
    db_path = _tmp_db()
    try:
        bot_infra.init_db()
        trade = {
            "ts": "2026-01-01T00:00:00Z",
            "market_id": "TEST-123",
            "mode": "paper",
            "outcome": "pending",
            "brain": "s1",
        }
        trade_id = asyncio.run(bot_infra.db_write_trade(trade))
        assert trade_id is not None
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT brain FROM trades WHERE id = ?", (trade_id,)).fetchone()
        conn.close()
        assert row[0] == "s1", f"Expected brain='s1', got {row[0]}"
    finally:
        os.unlink(db_path)


def test_s2_attempted_tickers_exists():
    assert hasattr(bot_state, "_s2_attempted_tickers"), \
        "_s2_attempted_tickers missing from bot_state"
    assert isinstance(bot_state._s2_attempted_tickers, set)


def test_order_attempted_tickers_removed():
    assert not hasattr(bot_state, "_order_attempted_tickers"), \
        "_order_attempted_tickers should be removed; use _s2_attempted_tickers"


def test_s1_trade_data_has_brain_s1():
    """_execute_s1_trade must include brain='s1' in the trade_data dict."""
    import bot_risk
    import inspect
    src = inspect.getsource(bot_risk._execute_s1_trade)
    assert '"brain": "s1"' in src or "'brain': 's1'" in src, \
        "_execute_s1_trade trade_data missing 'brain': 's1'"


def test_s2_trade_data_has_brain_s2():
    """handle_ready_phase must include brain='s2' in the S2 trade_data dict."""
    import bot_loops
    import inspect
    src = inspect.getsource(bot_loops.handle_ready_phase)
    assert '"brain": "s2"' in src or "'brain': 's2'" in src, \
        "handle_ready_phase S2 trade_data missing 'brain': 's2'"


def test_scorecard_returns_per_brain_per_asset():
    import asyncio
    from datetime import datetime, timezone

    db_path = _tmp_db()
    try:
        bot_infra.init_db()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        async def _seed():
            await bot_infra.db_write_trade({
                "ts": f"{today}T01:00:00Z",
                "market_id": "T1",
                "mode": "paper",
                "outcome": "win",
                "asset": "BTC",
                "pnl_dollars": 2.50,
                "brain": "s1",
            })
            await bot_infra.db_write_trade({
                "ts": f"{today}T02:00:00Z",
                "market_id": "T2",
                "mode": "paper",
                "outcome": "loss",
                "asset": "BTC",
                "pnl_dollars": -1.00,
                "brain": "s1",
            })
            await bot_infra.db_write_trade({
                "ts": f"{today}T03:00:00Z",
                "market_id": "T3",
                "mode": "paper",
                "outcome": "win",
                "asset": "ETH",
                "pnl_dollars": 1.50,
                "brain": "s2",
            })
            await bot_infra.db_write_trade({
                "ts": f"{today}T04:00:00Z",
                "market_id": "T4",
                "mode": "paper",
                "outcome": "breakeven",
                "asset": "BTC",
                "pnl_dollars": 0.0,
                "brain": "s1",
            })
        asyncio.run(_seed())

        result = asyncio.run(bot_infra.db_brain_scorecard(today))

        s1_btc = result["daily"]["s1"].get("BTC", {})
        s2_eth = result["daily"]["s2"].get("ETH", {})

        assert abs(s1_btc.get("pnl", 0) - 1.50) < 0.01, \
            f"S1 BTC daily pnl wrong: {s1_btc}"
        assert s1_btc.get("wins") == 1, f"S1 BTC wins wrong: {s1_btc}"
        # break-even (pnl=0.0) must NOT count as a loss
        assert s1_btc.get("losses") == 1, \
            f"Break-even counted as loss; S1 BTC losses: {s1_btc}"
        assert abs(s2_eth.get("pnl", 0) - 1.50) < 0.01, \
            f"S2 ETH daily pnl wrong: {s2_eth}"
    finally:
        os.unlink(db_path)


def test_format_scorecard_message():
    """_format_scorecard_message returns expected Telegram text."""
    import bot_loops

    assert hasattr(bot_loops, "_format_scorecard_message"), \
        "_format_scorecard_message not found in bot_loops"

    data = {
        "daily": {
            "s1": {"BTC": {"pnl": 2.50, "wins": 3, "losses": 1, "trades": 4}},
            "s2": {"ETH": {"pnl": -1.00, "wins": 1, "losses": 2, "trades": 3}},
        },
        "alltime": {
            "s1": {"BTC": {"pnl": 12.50, "wins": 10, "losses": 3, "trades": 13}},
            "s2": {"ETH": {"pnl": 5.00, "wins": 6, "losses": 4, "trades": 10}},
        },
    }
    msg = bot_loops._format_scorecard_message(data)
    assert "S1" in msg
    assert "S2" in msg
    assert "BTC" in msg
    assert "ETH" in msg
    assert "All-time" in msg or "all-time" in msg.lower()
