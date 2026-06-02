def test_dashboard_badge_uses_brain_field():
    """Dashboard trades badge must use brain field when present."""
    with open('handoff/Money Printer.html', encoding='utf-8') as f:
        src = f.read()
    # mapTrade must map the brain field
    assert "brain:" in src or "t.brain" in src, \
        "mapTrade does not map the brain field from the API response"
    # Badge must prefer brain over strategy_variant
    assert "t.brain==='s1'" in src or "brain==='s1'" in src, \
        "Badge does not use brain field for S1/S2 label"


def test_bot_infra_default_includes_all_5_assets():
    """bot_infra read_config default must include all 5 assets."""
    with open('bot_infra.py', encoding='utf-8') as f:
        src = f.read()
    assert '"BTC"' in src and '"DOGE"' in src, \
        "bot_infra.py enabled_assets default missing BTC or DOGE"
    idx = src.find('setdefault("enabled_assets"')
    assert idx != -1, "setdefault for enabled_assets not found"
    chunk = src[idx:idx+100]
    assert 'BTC' in chunk and 'DOGE' in chunk, \
        f"setdefault line does not include BTC and DOGE: {chunk!r}"


def test_server_full_config_default_includes_all_5_assets():
    """server._FULL_CONFIG_DEFAULT must include all 5 assets."""
    import sys
    if 'server' in sys.modules:
        del sys.modules['server']
    import server
    assert 'enabled_assets' in server._FULL_CONFIG_DEFAULT, \
        "_FULL_CONFIG_DEFAULT missing enabled_assets key"
    ea = server._FULL_CONFIG_DEFAULT['enabled_assets']
    for asset in ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE']:
        assert asset in ea, f"{asset} not in _FULL_CONFIG_DEFAULT enabled_assets: {ea}"


def test_quiet_hours_gate_exists_in_strategy():
    with open('bot_strategy.py', encoding='utf-8') as f:
        src = f.read()
    assert '_is_quiet_hours' in src
    s1 = src[src.index('def strategy_brain_s1'):src.index('def strategy_brain_s2')]
    s2 = src[src.index('def strategy_brain_s2'):]
    assert '_is_quiet_hours' in s1
    assert '_is_quiet_hours' in s2


def test_quiet_hours_disabled_returns_false():
    """When quiet_hours_enabled=False, gate must return False regardless of time."""
    import bot_strategy
    assert bot_strategy._is_quiet_hours({"quiet_hours_enabled": False}) is False


def test_quiet_hours_span_midnight_logic():
    """span-midnight (22→7): midnight/6am/10pm=quiet; noon/7am=not quiet."""
    import bot_strategy, datetime as dt_real
    from unittest.mock import patch

    config = {"quiet_hours_enabled": True, "quiet_start_et": 22, "quiet_end_et": 7}

    def _check_hour(hour):
        fake = dt_real.datetime(2026, 6, 2, hour, 0, 0,
                                tzinfo=dt_real.timezone(dt_real.timedelta(hours=-4)))
        with patch("bot_strategy.datetime") as m:
            m.datetime.now.return_value = fake
            m.timezone = dt_real.timezone
            m.timedelta = dt_real.timedelta
            return bot_strategy._is_quiet_hours(config)

    assert _check_hour(0)  is True,  "midnight (0) should be quiet"
    assert _check_hour(12) is False, "noon (12) should not be quiet"
    assert _check_hour(22) is True,  "10pm (22) should be quiet"
    assert _check_hour(6)  is True,  "6am (6) should be quiet"
    assert _check_hour(7)  is False, "7am (7) should not be quiet"
