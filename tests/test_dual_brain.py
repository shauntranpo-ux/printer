"""Tests for dual brain isolation and strategy fixes."""
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
