"""Tests for strategy gate fixes: min_dist lowering, entry price caps."""


def test_s1_min_dist_in_profitable_range():
    """S1 min_dist must be 0.002-0.010 — avoids coin-flip zone while still firing."""
    from bot_strategy import _S1_ASSET_CONFIG
    for asset in ["BTC", "ETH", "XRP"]:
        d = _S1_ASSET_CONFIG[asset]["min_dist"]
        assert 0.002 <= d <= 0.010, \
            f"{asset} S1 min_dist {d} outside profitable range 0.002-0.010"


def test_s2_min_dist_lowered():
    """S2 min_dist must be <= 0.002 for BTC/ETH/XRP."""
    from bot_strategy import _S2_ASSET_CONFIG
    assert _S2_ASSET_CONFIG["BTC"]["min_dist"] <= 0.002, \
        f"BTC S2 min_dist {_S2_ASSET_CONFIG['BTC']['min_dist']} too high"
    assert _S2_ASSET_CONFIG["ETH"]["min_dist"] <= 0.002, \
        f"ETH S2 min_dist {_S2_ASSET_CONFIG['ETH']['min_dist']} too high"
    assert _S2_ASSET_CONFIG["XRP"]["min_dist"] <= 0.002, \
        f"XRP S2 min_dist {_S2_ASSET_CONFIG['XRP']['min_dist']} too high"


def test_s1_max_entry_price_capped_for_profitability():
    """S1 max_entry_price default must be 50-60c — uncertainty zone at realistic WR."""
    import re
    with open('bot_strategy.py', encoding='utf-8') as f:
        src = f.read()
    s1_section = src[src.index('def strategy_brain_s1'):src.index('def strategy_brain_s2')]
    defaults = re.findall(r'max_entry_price_cents",\s*([\d.]+)', s1_section)
    assert defaults, "max_entry_price_cents default not found in strategy_brain_s1"
    for d in defaults:
        assert 50.0 <= float(d) <= 60.0, \
            f"S1 max_entry_price {d} outside 50-60c range"


def test_s2_max_entry_price_capped_for_profitability():
    """S2 max_entry_price default must be 50-60c."""
    import re
    with open('bot_strategy.py', encoding='utf-8') as f:
        src = f.read()
    s2_section = src[src.index('def strategy_brain_s2'):]
    defaults = re.findall(r'max_entry_price_cents",\s*([\d.]+)', s2_section)
    assert defaults, "max_entry_price_cents default not found in strategy_brain_s2"
    for d in defaults:
        assert 50.0 <= float(d) <= 60.0, \
            f"S2 max_entry_price {d} outside 50-60c range"


def test_s1_time_bounds_in_range():
    """S1 time_min/time_max must keep trades away from settlement chaos and very-early noise."""
    from bot_strategy import _S1_ASSET_CONFIG
    for asset, cfg in _S1_ASSET_CONFIG.items():
        assert cfg["time_min"] >= 0.5, f"{asset} time_min {cfg['time_min']} too small"
        assert cfg["time_min"] <= 2.0, f"{asset} time_min {cfg['time_min']} too large"
        assert cfg["time_max"] >= 10.0, f"{asset} time_max {cfg['time_max']} too small"
        assert cfg["time_max"] <= 14.0, f"{asset} time_max {cfg['time_max']} too large"


def test_debug_gates_endpoint_exists():
    """GET /api/debug/gates must exist."""
    import server
    rules = [str(r) for r in server.app.url_map.iter_rules()]
    assert "/api/debug/gates" in rules, "/api/debug/gates route not registered"


def test_debug_gates_returns_assets():
    """GET /api/debug/gates must return 200 with assets key containing s1/s2 per entry."""
    import sys
    if 'server' in sys.modules:
        del sys.modules['server']
    import server
    client = server.app.test_client()
    resp = client.get("/api/debug/gates")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "assets" in data, "response missing 'assets' key"
    assert len(data["assets"]) > 0, "assets dict is empty"
    for asset, v in data["assets"].items():
        assert "s1" in v and "s2" in v, f"{asset} missing s1/s2 keys"
        assert "action" in v["s1"] and "action" in v["s2"]
