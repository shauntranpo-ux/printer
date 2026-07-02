"""Verify S2 skips when S1 has an active trade on the same ticker."""
import inspect
import bot_loops


def test_s2_dedup_check_exists_after_s1_execute():
    """handle_ready_phase source must check _s1_pending_trades before S2 fires."""
    src = inspect.getsource(bot_loops.handle_ready_phase)
    assert "_s1_pending_trades" in src, (
        "_s1_pending_trades check missing from handle_ready_phase"
    )
    # Check that the dedup block sets do_trade=False
    assert "s2_dedup" in src, (
        "s2_dedup skip reason not found in handle_ready_phase - S1+S2 dedup not implemented"
    )


def test_dedup_fires_only_when_do_trade_true():
    """The dedup gate must only fire when S2 wants to trade (do_trade=True)."""
    src = inspect.getsource(bot_loops.handle_ready_phase)
    # Must guard with 'if do_trade and ticker in bot_state._s1_pending_trades'
    assert "do_trade and ticker in bot_state._s1_pending_trades" in src, (
        "Dedup must be gated on do_trade=True to avoid false positives when S2 already skipping"
    )
