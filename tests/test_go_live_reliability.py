"""Tests for go-live reliability fixes."""
import ast
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


def test_write_state_called_on_locked_transition():
    """
    After handle_ready_phase transitions to LOCKED, the state file must be
    written in the same function call — not deferred to the next loop tick.
    """
    import ast
    src = (ROOT / "bot_loops.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_ready_phase":
            fn_src = ast.get_source_segment(src, node)
            assert fn_src is not None
            lines = fn_src.splitlines()
            # Target the else-branch assignment specifically (not the _use_state branch)
            locked_line = next(
                (i for i, line in enumerate(lines) if 'current_phase = "LOCKED"' in line),
                None,
            )
            assert locked_line is not None, (
                'Could not find `current_phase = "LOCKED"` in handle_ready_phase'
            )
            write_lines = [
                i for i, line in enumerate(lines) if "await write_state_file" in line
            ]
            assert write_lines, "write_state_file must be called inside handle_ready_phase"
            assert max(write_lines) > locked_line, (
                "write_state_file must be called AFTER setting current_phase='LOCKED'"
            )
            return
    pytest.fail("handle_ready_phase not found in bot_loops.py")


def test_portfolio_fallback_wired_in_handle_ready_phase():
    """
    When fill_confirmed=False, handle_ready_phase must call _portfolio_has_position
    in live/demo mode before setting phase=DONE.
    """
    import ast
    src = (ROOT / "bot_loops.py").read_text(encoding="utf-8")
    assert "_portfolio_has_position" in src, (
        "_portfolio_has_position must be imported in bot_loops.py"
    )
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_ready_phase":
            fn_src = ast.get_source_segment(src, node)
            assert "_portfolio_has_position" in fn_src, (
                "_portfolio_has_position must be called inside handle_ready_phase"
            )
            return
    pytest.fail("handle_ready_phase not found")


def test_non_btc_positions_persisted_in_state_file():
    """write_state_file must include a non_btc_positions key so non-BTC LOCKED
    positions survive a restart."""
    src = (ROOT / "bot_risk.py").read_text(encoding="utf-8")
    assert '"non_btc_positions"' in src, (
        'write_state_file must write a "non_btc_positions" key to the state JSON'
    )


def test_non_btc_positions_recovered_on_startup():
    """main_loop startup must read non_btc_positions from the state file and
    restore them into bot_state._asset_states."""
    src = (ROOT / "bot_loops.py").read_text(encoding="utf-8")
    assert "non_btc_positions" in src, (
        "main_loop must read non_btc_positions from the state file on startup"
    )


def test_s1_orphan_auto_settlement_wired():
    """_settle_s1_orphans must be imported and called in main_loop — not just warn."""
    src = (ROOT / "bot_loops.py").read_text(encoding="utf-8")
    assert "_settle_s1_orphans" in src, (
        "_settle_s1_orphans must be imported and called in bot_loops.py main_loop"
    )
    # The warning-only orphan block must be gone
    assert "check Kalshi fills manually" not in src, (
        "The warning-only S1 orphan block must be replaced with _settle_s1_orphans"
    )
