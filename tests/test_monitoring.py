"""Rolling win rate tracking - brain_log emits WR after each S1 settlement."""
import sys
import os
import inspect

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot_risk import _settle_s1_trade, _s1_rolling_outcomes


def test_settle_s1_logs_rolling_wr():
    """_settle_s1_trade must emit rolling WR to brain_log."""
    src = inspect.getsource(_settle_s1_trade)
    assert "rolling_wr" in src, (
        "_settle_s1_trade must emit rolling_wr to brain_log - add this instrumentation"
    )
    assert "_brain_log" in src, (
        "_settle_s1_trade must call _brain_log - import brain_log from bot_strategy"
    )


def test_rolling_outcomes_deque_exists():
    """_s1_rolling_outcomes module-level deque must exist in bot_risk."""
    import bot_risk
    assert hasattr(bot_risk, "_s1_rolling_outcomes"), (
        "_s1_rolling_outcomes deque not found in bot_risk"
    )
    assert bot_risk._s1_rolling_outcomes.maxlen == 50, (
        f"Expected maxlen=50, got {bot_risk._s1_rolling_outcomes.maxlen}"
    )



