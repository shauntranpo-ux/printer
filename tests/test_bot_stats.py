"""Unit tests for bot_stats.py - query_stats and format functions."""
import datetime
import sqlite3
import sys
import os
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bot_stats


def _make_db(rows):
    """Create in-memory SQLite DB with trades table and given rows."""
    db = sqlite3.connect(":memory:")
    db.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            ts TEXT,
            strategy_variant TEXT,
            asset TEXT,
            outcome TEXT,
            pnl_dollars REAL
        )
    """)
    for row in rows:
        db.execute(
            "INSERT INTO trades (ts, strategy_variant, asset, outcome, pnl_dollars) VALUES (?,?,?,?,?)",
            row,
        )
    db.commit()
    return db


# query_stats

def test_query_stats_today_counts(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            ts TEXT,
            strategy_variant TEXT,
            asset TEXT,
            outcome TEXT,
            pnl_dollars REAL
        )
    """)
    today = "2026-05-07"
    conn.executemany(
        "INSERT INTO trades (ts, strategy_variant, asset, outcome, pnl_dollars) VALUES (?,?,?,?,?)",
        [
            (f"{today}T10:00:00Z", "strategy1", "BTC", "win",  10.0),
            (f"{today}T11:00:00Z", "strategy1", "BTC", "loss", -5.0),
            (f"{today}T12:00:00Z", "strategy2", "ETH", "win",  20.0),
            # yesterday - should not appear in today counts
            ("2026-05-06T10:00:00Z", "strategy1", "BTC", "win", 99.0),
        ],
    )
    conn.commit()
    conn.close()

    stats = bot_stats.query_stats(db_path, today_date=today)

    assert stats["date"] == today
    assert stats["today_trades"] == 3
    assert stats["today_wins"] == 2
    assert stats["today_losses"] == 1
    assert abs(stats["today_pnl"] - 25.0) < 0.01
    assert stats["alltime_trades"] == 4
    assert stats["alltime_wins"] == 3
    assert abs(stats["alltime_pnl"] - 124.0) < 0.01


def test_query_stats_by_strategy_asset(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            ts TEXT,
            strategy_variant TEXT,
            asset TEXT,
            outcome TEXT,
            pnl_dollars REAL
        )
    """)
    today = "2026-05-07"
    conn.executemany(
        "INSERT INTO trades (ts, strategy_variant, asset, outcome, pnl_dollars) VALUES (?,?,?,?,?)",
        [
            (f"{today}T10:00:00Z", "strategy1", "BTC", "win",  12.50),
            (f"{today}T10:01:00Z", "strategy1", "BTC", "loss", -3.00),
            (f"{today}T10:02:00Z", "strategy2", "BTC", "win",  8.00),
        ],
    )
    conn.commit()
    conn.close()

    stats = bot_stats.query_stats(db_path, today_date=today)
    sa = stats["by_strategy_asset"]

    assert ("strategy1", "BTC") in sa
    assert sa[("strategy1", "BTC")]["wins"] == 1
    assert sa[("strategy1", "BTC")]["losses"] == 1
    assert abs(sa[("strategy1", "BTC")]["pnl"] - 9.50) < 0.01

    assert ("strategy2", "BTC") in sa
    assert sa[("strategy2", "BTC")]["wins"] == 1
    assert sa[("strategy2", "BTC")]["losses"] == 0


def test_query_stats_no_trades(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            ts TEXT,
            strategy_variant TEXT,
            asset TEXT,
            outcome TEXT,
            pnl_dollars REAL
        )
    """)
    conn.commit()
    conn.close()

    stats = bot_stats.query_stats(db_path, today_date="2026-05-07")

    assert stats["today_trades"] == 0
    assert stats["today_wins"] == 0
    assert stats["today_losses"] == 0
    assert stats["alltime_trades"] == 0
    assert stats["last_trade_ts"] is None


def test_query_stats_last_trade_ts(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            ts TEXT,
            strategy_variant TEXT,
            asset TEXT,
            outcome TEXT,
            pnl_dollars REAL
        )
    """)
    conn.execute(
        "INSERT INTO trades (ts, strategy_variant, asset, outcome, pnl_dollars) VALUES (?,?,?,?,?)",
        ("2026-05-07T14:30:00Z", "strategy1", "BTC", "win", 5.0),
    )
    conn.commit()
    conn.close()

    stats = bot_stats.query_stats(db_path, today_date="2026-05-07")
    assert stats["last_trade_ts"] == "2026-05-07T14:30:00Z"


def test_query_stats_db_unavailable():
    # Should log warning and return zero-filled dict, not raise.
    stats = bot_stats.query_stats("/nonexistent/path/db.sqlite", today_date="2026-05-07")
    assert stats["today_trades"] == 0
    assert stats["alltime_trades"] == 0
    assert stats["last_trade_ts"] is None


# format_telegram

def _base_stats(**overrides):
    s = {
        "date": "2026-05-07",
        "today_trades": 0,
        "today_wins": 0,
        "today_losses": 0,
        "today_pnl": 0.0,
        "alltime_trades": 0,
        "alltime_wins": 0,
        "alltime_pnl": 0.0,
        "by_strategy_asset": {},
        "last_trade_ts": None,
        "consecutive_losses": 0,
        "mode": "PAPER",
    }
    s.update(overrides)
    return s


def test_format_telegram_no_trades():
    msg = bot_stats.format_telegram(_base_stats())
    assert "No trades today" in msg
    assert "2026-05-07" in msg
    assert "PAPER" in msg


def test_format_telegram_with_trades():
    stats = _base_stats(
        today_trades=5,
        today_wins=3,
        today_losses=2,
        today_pnl=25.0,
        alltime_trades=50,
        alltime_wins=30,
        alltime_pnl=100.0,
        by_strategy_asset={
            ("strategy1", "BTC"): {"wins": 3, "losses": 2, "pnl": 25.0},
        },
        last_trade_ts="2026-05-07T14:30:00Z",
        consecutive_losses=1,
    )
    msg = bot_stats.format_telegram(stats)
    assert "S1" in msg
    assert "BTC" in msg
    assert "3W/2L" in msg
    assert "+$25.00" in msg
    assert "60.0%" in msg


def test_format_telegram_hides_zero_strategy_sections():
    stats = _base_stats(
        today_trades=2,
        today_wins=2,
        today_losses=0,
        today_pnl=10.0,
        alltime_trades=2,
        alltime_wins=2,
        alltime_pnl=10.0,
        by_strategy_asset={
            ("strategy1", "BTC"): {"wins": 2, "losses": 0, "pnl": 10.0},
            # strategy2 has zero trades -> section should be hidden
        },
        last_trade_ts="2026-05-07T10:00:00Z",
    )
    msg = bot_stats.format_telegram(stats)
    assert "S1" in msg
    assert "S2" not in msg


# midnight trigger

def test_daily_summary_fires_once_per_day():
    """_maybe_send_daily_summary sends exactly once per ET day (dedup + rollover)."""
    import asyncio
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from unittest.mock import AsyncMock, patch
    import bot_loops

    _et = ZoneInfo("America/New_York")
    sent = []

    async def fake_send(msg):
        sent.append(msg)

    def run_at(now_et):
        class _FrozenDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return now_et if tz else now_et.replace(tzinfo=None)
        with patch.object(bot_loops, "send_telegram", AsyncMock(side_effect=fake_send)), \
             patch.object(bot_loops, "read_config", return_value={"mode": "paper"}), \
             patch.object(bot_loops, "write_config"), \
             patch.object(bot_loops, "datetime", _FrozenDT):
            asyncio.run(bot_loops._maybe_send_daily_summary())

    bot_loops._last_summary_sent_for = ""
    run_at(datetime(2026, 5, 8, 0, 25, tzinfo=_et))  # fires for May 7
    run_at(datetime(2026, 5, 8, 12, 0, tzinfo=_et))  # same day - no-op
    run_at(datetime(2026, 5, 9, 0, 25, tzinfo=_et))  # new day - fires for May 8
    bot_loops._last_summary_sent_for = ""
    assert len(sent) == 2  # once for each completed ET day


def test_format_terminal_no_html():
    stats = _base_stats(
        today_trades=3,
        today_wins=2,
        today_losses=1,
        today_pnl=15.0,
        alltime_trades=10,
        alltime_wins=6,
        alltime_pnl=40.0,
        by_strategy_asset={
            ("strategy2", "ETH"): {"wins": 2, "losses": 1, "pnl": 15.0},
        },
        last_trade_ts="2026-05-07T08:00:00Z",
    )
    msg = bot_stats.format_terminal(stats)
    assert "<b>" not in msg
    assert "<code>" not in msg
    assert "S2" in msg
    assert "ETH" in msg
