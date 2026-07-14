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


def test_dedup_is_gated_by_duel_mode_and_paper_only():
    """In duel mode both brains may hold the same ticker - but ONLY on paper. With real
    capital the dedup must always apply, so the bypass requires duel mode AND both
    strategies' effective modes to be paper."""
    src = inspect.getsource(bot_loops.handle_ready_phase)
    assert 'strategy_duel_mode' in src, (
        "S1/S2 dedup must be gated on config['strategy_duel_mode']"
    )
    assert 'mode == "paper" and mode_s1 == "paper"' in src, (
        "duel bypass must require both S1 and S2 modes to be paper"
    )
    assert "if not _duel_paper:" in src, (
        "dedup must apply whenever the paper-duel condition does not hold"
    )
