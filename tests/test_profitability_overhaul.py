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
