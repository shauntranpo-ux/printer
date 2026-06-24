"""_is_quiet_hours and _time_of_day_vol_multiplier must use DST-aware timezone."""
import pathlib


def test_is_quiet_hours_uses_zoneinfo():
    """_is_quiet_hours must not use hardcoded -4 offset; must use ZoneInfo."""
    src = pathlib.Path("bot_strategy.py").read_text()
    assert 'timedelta(hours=-4)' not in src, (
        "_is_quiet_hours still uses hardcoded -4 (EDT only). Use ZoneInfo('America/New_York')."
    )
    assert 'ZoneInfo' in src and 'America/New_York' in src, (
        "_is_quiet_hours must use ZoneInfo('America/New_York') for DST-correct Eastern Time."
    )


def test_time_of_day_vol_multiplier_uses_zoneinfo():
    """_time_of_day_vol_multiplier must not use hardcoded -4 offset."""
    src = pathlib.Path("bot_strategy.py").read_text()
    assert 'timedelta(hours=-4)' not in src, (
        "_time_of_day_vol_multiplier still uses hardcoded -4 (EDT only). Use ZoneInfo."
    )
    assert 'ZoneInfo' in src and 'America/New_York' in src, (
        "_time_of_day_vol_multiplier must use ZoneInfo('America/New_York')."
    )


def test_zoneinfo_imported_in_bot_strategy():
    """ZoneInfo must be importable from bot_strategy module."""
    src = pathlib.Path("bot_strategy.py").read_text()
    assert 'from zoneinfo import ZoneInfo' in src, (
        "ZoneInfo not found in bot_strategy.py — add 'from zoneinfo import ZoneInfo' to imports."
    )
