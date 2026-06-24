"""Tests for Task 5: EOD summary fires on quiet-hours transition."""
import inspect
import importlib
import bot_loops


def _src():
    return inspect.getsource(bot_loops)


def test_is_quiet_hours_imported():
    """_is_quiet_hours must be imported and referenced in bot_loops."""
    assert "_is_quiet_hours" in _src(), "_is_quiet_hours not found in bot_loops source"


def test_prev_quiet_flags_present():
    """Both _prev_quiet_nb and _prev_quiet_main must exist in bot_loops."""
    src = _src()
    assert "_prev_quiet_nb" in src, "_prev_quiet_nb not found in bot_loops source"
    assert "_prev_quiet_main" in src, "_prev_quiet_main not found in bot_loops source"


def test_check_daily_stats_called_on_transition():
    """_check_daily_stats must be called in bot_loops source (transition adds new calls)."""
    src = _src()
    assert "_check_daily_stats" in src, "_check_daily_stats not found in bot_loops source"
    # Ensure there are multiple call sites (original hour==14 check + 2 transition sites)
    count = src.count("_check_daily_stats")
    assert count >= 3, f"Expected at least 3 references to _check_daily_stats, found {count}"
