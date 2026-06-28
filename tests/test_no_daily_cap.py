"""Daily-loss cap is OFF by default (0) and disabled for any non-positive limit."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
import bot_infra
from bot_risk import check_daily_limits


def _reset_limit_state():
    bot_state.limit_triggered = False
    bot_state.limit_reason = ""
    bot_state.pre_limit_mode = None


async def test_zero_limit_never_triggers_even_on_big_loss(monkeypatch):
    """daily_loss_limit_dollars=0 must NOT halt — a 0 cap would otherwise make
    abs(pnl) >= 0 always true and stop on the first cent of loss."""
    _reset_limit_state()

    async def _fake_pnl(mode):
        return -250.0  # huge loss

    monkeypatch.setattr("bot_risk.db_get_today_pnl", _fake_pnl)
    config = {"mode": "live", "daily_loss_limit_dollars": 0, "daily_profit_target_dollars": 0}

    triggered, reason = await check_daily_limits(config)
    assert triggered is False, "0 limit must mean NO daily cap, even on a large loss"
    assert not bot_state.limit_triggered


async def test_positive_limit_still_triggers(monkeypatch):
    """A positive cap re-enables the halt (opt-in)."""
    _reset_limit_state()

    async def _fake_pnl(mode):
        return -60.0

    monkeypatch.setattr("bot_risk.db_get_today_pnl", _fake_pnl)
    config = {"mode": "live", "daily_loss_limit_dollars": 50, "daily_profit_target_dollars": 0}

    triggered, _ = await check_daily_limits(config)
    assert triggered is True, "a positive limit must still trip when exceeded"


def test_config_default_loss_limit_is_zero():
    """Fresh config normalizes daily_loss_limit_dollars to 0 (no cap) when absent."""
    cfg = bot_infra._apply_config_migrations({}) if hasattr(bot_infra, "_apply_config_migrations") else None
    if cfg is None:
        # Fall back: read_config-style normalization path may differ; assert the literal default
        import inspect
        src = inspect.getsource(bot_infra)
        assert '"daily_loss_limit_dollars": 0' in src, "default loss limit should be 0 (no cap)"
    else:
        assert cfg["daily_loss_limit_dollars"] == 0
