"""Verify the drawdown check uses async db_get_today_pnl, not sync get_today_pnl."""
import inspect
import bot_loops


def test_drawdown_uses_async_pnl():
    """handle_ready_phase must NOT import or call sync get_today_pnl."""
    src = inspect.getsource(bot_loops.handle_ready_phase)
    assert "get_today_pnl" not in src or "await db_get_today_pnl" in src, (
        "handle_ready_phase calls sync get_today_pnl — use await db_get_today_pnl instead"
    )
    assert "await db_get_today_pnl" in src, (
        "handle_ready_phase must call await db_get_today_pnl for non-blocking drawdown check"
    )


def test_no_local_sync_import_in_drawdown():
    """The local 'from bot_infra import get_today_pnl' must not exist inside handle_ready_phase."""
    src = inspect.getsource(bot_loops.handle_ready_phase)
    assert "from bot_infra import get_today_pnl" not in src, (
        "Sync local import found inside handle_ready_phase — remove it"
    )
