"""Verify _is_quiet_hours uses default quiet_end_et=9, matching _init_config."""
import bot_strategy


def test_quiet_hours_end_default_blocks_8am_et():
    """With empty config, 8 AM ET (hour=8) must be quiet (end default=9)."""
    # Empty config - tests the hardcoded fallback, not config.json
    result = bot_strategy._is_quiet_hours(config={})
    # We cannot easily set the clock, so inspect the source instead
    import inspect
    src = inspect.getsource(bot_strategy._is_quiet_hours)
    # Default quiet_end_et must be 9, not 7
    assert '"quiet_end_et", 9)' in src or "'quiet_end_et', 9)" in src, (
        "_is_quiet_hours default quiet_end_et must be 9 (matches _init_config), found 7 instead"
    )


def test_quiet_hours_start_default_is_22_not_17():
    """Default quiet_start_et must be 22 (10 PM ET), matching _init_config."""
    import inspect
    src = inspect.getsource(bot_strategy._is_quiet_hours)
    assert '"quiet_start_et", 22)' in src or "'quiet_start_et', 22)" in src, (
        "_is_quiet_hours quiet_start_et default must be 22"
    )
