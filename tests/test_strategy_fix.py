"""Tests for strategy gate fixes: min_dist lowering, entry price caps."""


def test_s1_min_dist_lowered():
    """S1 min_dist must be <= 0.0015 for BTC/ETH/XRP (was 0.0025-0.004)."""
    from bot_strategy import _S1_ASSET_CONFIG
    assert _S1_ASSET_CONFIG["BTC"]["min_dist"]  <= 0.0015, \
        f"BTC S1 min_dist {_S1_ASSET_CONFIG['BTC']['min_dist']} too high"
    assert _S1_ASSET_CONFIG["ETH"]["min_dist"]  <= 0.0015, \
        f"ETH S1 min_dist {_S1_ASSET_CONFIG['ETH']['min_dist']} too high"
    assert _S1_ASSET_CONFIG["XRP"]["min_dist"]  <= 0.0015, \
        f"XRP S1 min_dist {_S1_ASSET_CONFIG['XRP']['min_dist']} too high"


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
    """S1 max_entry_price default must be <= 62 to be profitable at 66.7% WR."""
    import re
    with open('bot_strategy.py', encoding='utf-8') as f:
        src = f.read()
    s1_section = src[src.index('def strategy_brain_s1'):src.index('def strategy_brain_s2')]
    defaults = re.findall(r'max_entry_price_cents",\s*([\d.]+)', s1_section)
    assert defaults, "max_entry_price_cents default not found in strategy_brain_s1"
    for d in defaults:
        assert float(d) <= 62.0, \
            f"S1 max_entry_price {d} too high — at 66.7% WR need <=62c to be profitable"


def test_s2_max_entry_price_capped_for_profitability():
    """S2 max_entry_price default must be <= 65 to be profitable at 69.2% WR."""
    import re
    with open('bot_strategy.py', encoding='utf-8') as f:
        src = f.read()
    s2_section = src[src.index('def strategy_brain_s2'):]
    defaults = re.findall(r'max_entry_price_cents",\s*([\d.]+)', s2_section)
    assert defaults, "max_entry_price_cents default not found in strategy_brain_s2"
    for d in defaults:
        assert float(d) <= 65.0, \
            f"S2 max_entry_price {d} too high — at 69.2% WR need <=65c to be profitable"


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
