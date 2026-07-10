"""Verify the S1/S2 same-ticker dedup exists and is gated by strategy_duel_mode."""
import inspect
import bot_loops


def test_s2_dedup_check_exists_after_s1_execute():
    """handle_ready_phase source must still carry the _s1_pending_trades dedup path."""
    src = inspect.getsource(bot_loops.handle_ready_phase)
    assert "_s1_pending_trades" in src, (
        "_s1_pending_trades check missing from handle_ready_phase"
    )
    assert "s2_dedup" in src, (
        "s2_dedup skip reason not found in handle_ready_phase"
    )


def test_dedup_fires_only_when_do_trade_true():
    """The dedup gate must only fire when S2 wants to trade (do_trade=True)."""
    src = inspect.getsource(bot_loops.handle_ready_phase)
    assert "do_trade and ticker in bot_state._s1_pending_trades" in src, (
        "Dedup must be gated on do_trade=True to avoid false positives when S2 already skipping"
    )


def test_dedup_is_gated_by_duel_mode():
    """In duel mode (default) both brains may hold the same ticker; the one-way S1-blocks-S2
    dedup must be behind the strategy_duel_mode flag so it does not poison the head-to-head."""
    src = inspect.getsource(bot_loops.handle_ready_phase)
    assert 'strategy_duel_mode' in src, (
        "S1/S2 dedup must be gated on config['strategy_duel_mode']"
    )
    # The dedup must sit inside the `not ... strategy_duel_mode` branch.
    assert 'not config.get("strategy_duel_mode"' in src, (
        "dedup should only apply when strategy_duel_mode is False"
    )
