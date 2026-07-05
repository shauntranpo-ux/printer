"""Tests for infrastructure hardening fixes."""
import os
import sys
import inspect

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bot_market
import bot_infra
import bot_stats


def test_bot_market_fstring_no_nested_comprehension():
    """parse_strike warning log must use a variable, not nested dict comprehension."""
    src = inspect.getsource(bot_market.parse_strike)
    assert "{ {k:" not in src, \
        "Nested dict comprehension still in f-string - extract to _diag variable"
    assert "_diag" in src, \
        "parse_strike warning log must use _diag variable"


def test_db_get_today_pnl_uses_et_day_bounds():
    """db_get_today_pnl must bucket by the ET trading day via explicit UTC bounds.

    DATE(ts) buckets by UTC date and misfiles every trade after 8pm ET; ts LIKE
    was the even older bug. The contract is now a half-open ts range from
    et_day_bounds_utc.
    """
    src = inspect.getsource(bot_infra.db_get_today_pnl)
    assert "et_day_bounds_utc" in src, "db_get_today_pnl must use et_day_bounds_utc"
    assert "ts >= ? AND ts < ?" in src, "db_get_today_pnl must filter by ts range"
    assert "DATE(ts)" not in src, "DATE(ts) buckets by UTC date - wrong trading day"
    assert "ts LIKE" not in src, "db_get_today_pnl must not use ts LIKE"


def test_db_update_trade_has_column_whitelist():
    """db_update_trade must validate column names against _VALID_TRADE_COLS."""
    src = inspect.getsource(bot_infra.db_update_trade)
    assert "_VALID_TRADE_COLS" in src, "db_update_trade missing _VALID_TRADE_COLS guard"
    assert hasattr(bot_infra, "_VALID_TRADE_COLS"), "_VALID_TRADE_COLS must be module-level"
    assert isinstance(bot_infra._VALID_TRADE_COLS, frozenset)
    assert "outcome" in bot_infra._VALID_TRADE_COLS
    assert "brain" in bot_infra._VALID_TRADE_COLS
    assert "exit_price_cents" in bot_infra._VALID_TRADE_COLS


def test_bot_stats_unknown_variant_warned():
    """_run_queries must check strategy_variant against _STRATEGY_LABELS and log warning."""
    src = inspect.getsource(bot_stats._run_queries)
    assert "_STRATEGY_LABELS" in src, \
        "_run_queries must check row strategy_variant against _STRATEGY_LABELS"
    assert "log.warning" in src, \
        "_run_queries must log warning for unknown strategy_variant"
