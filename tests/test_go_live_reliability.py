"""Tests for go-live reliability fixes."""
import pathlib

import pytest

ROOT = pathlib.Path(__file__).parent.parent  # tests/ -> repo root


def test_session_ev_adjustment_removed():
    """_session_ev_adjustment must not exist in any bot module — it was a dead stub."""
    for fname in ["bot_strategy.py", "bot_loops.py", "bot_risk.py"]:
        src = (ROOT / fname).read_text(encoding="utf-8")
        assert "_session_ev_adjustment" not in src, (
            f"Found '_session_ev_adjustment' in {fname} — remove it"
        )


@pytest.fixture()
def _reset_limit_state():
    yield
    import bot_state as _bs
    _bs.limit_triggered = False
    _bs.limit_reason = ""
    _bs.pre_limit_mode = None


@pytest.mark.asyncio
async def test_daily_limit_resets_when_mode_changes(monkeypatch, _reset_limit_state):
    """
    If limit was triggered in demo mode but we're now checking in live mode,
    check_daily_limits must reset limit_triggered before evaluating live P&L.
    """
    import bot_state
    from bot_risk import check_daily_limits

    # Simulate: limit triggered during an earlier demo session
    bot_state.limit_triggered = True
    bot_state.limit_reason = "daily loss limit reached"
    bot_state.pre_limit_mode = "demo"

    # Mock db_get_today_pnl so no real DB is hit; live P&L is $0 (no new trigger)
    async def _fake_pnl(mode):
        return 0.0
    monkeypatch.setattr("bot_risk.db_get_today_pnl", _fake_pnl)

    config = {
        "mode": "live",
        "daily_loss_limit_dollars": 20,
        "daily_profit_target_dollars": 50,
    }

    triggered, reason = await check_daily_limits(config)

    assert not bot_state.limit_triggered, "limit_triggered must be reset when mode changed"
    assert triggered is False, "no pnl → no new trigger after reset"
    assert bot_state.limit_reason == "", "limit_reason must be cleared on mode-change reset"
