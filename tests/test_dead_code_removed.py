def test_all_markets_cache_ts_removed():
    """_all_markets_cache_ts must not exist — set but never read."""
    import bot_state
    assert not hasattr(bot_state, '_all_markets_cache_ts'), (
        "_all_markets_cache_ts still exists in bot_state"
    )
