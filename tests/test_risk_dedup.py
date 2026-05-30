"""Verify _execute_s1_trade dedup prevents double-entry on same ticker."""
import sys
import os
import inspect

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot_risk import _execute_s1_trade


def test_s1_pending_reserved_before_place_order():
    """After the fix: _s1_pending_trades[ticker] = ... appears BEFORE await place_order."""
    src = inspect.getsource(_execute_s1_trade)
    reserve_marker = "_s1_pending_trades[ticker] = "
    await_marker = "await place_order("

    assert reserve_marker in src, "_s1_pending_trades[ticker] assignment not found in _execute_s1_trade"
    assert await_marker in src, "await place_order not found in _execute_s1_trade"

    pre_idx = src.index(reserve_marker)
    post_idx = src.index(await_marker)
    assert pre_idx < post_idx, (
        f"BUG: _s1_pending_trades set at char {pre_idx} AFTER await place_order at "
        f"char {post_idx} — race condition present"
    )


def test_s1_pop_on_fill_failure():
    """After the fix: failed fill must pop the reservation (not leave stale entry)."""
    src = inspect.getsource(_execute_s1_trade)
    assert "_s1_pending_trades.pop(ticker" in src, (
        "_s1_pending_trades.pop(ticker, ...) not found — failed fills leave stale reservation"
    )
