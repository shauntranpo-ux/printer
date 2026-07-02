"""Tests for strategy gate fixes: min_dist lowering, entry price caps."""


def test_s1_min_dist_in_profitable_range():
    """S1 min_dist must be 0.002-0.010 - avoids coin-flip zone while still firing."""
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


def test_fair_value_entry_band_is_wide():
    """
    The fair-value brains deliberately use a WIDE entry band (the anchored-EV gate does the
    selectivity, so a stale-cheap 65c ask vs an 0.80 fair value is a valid trade). The old
    50-60c cap was calibrated for the momentum strategy and does not apply here.
    """
    from bot_strategy import _FV_MIN_ENTRY_CENTS, _FV_MAX_ENTRY_CENTS
    assert _FV_MIN_ENTRY_CENTS <= 15.0, f"fair-value min entry {_FV_MIN_ENTRY_CENTS} too high"
    assert 80.0 <= _FV_MAX_ENTRY_CENTS <= 95.0, \
        f"fair-value max entry {_FV_MAX_ENTRY_CENTS} outside expected wide band"


def test_both_brains_use_per_asset_entry_config():
    """Both brains must source the entry band via get_asset_config (per-asset override)."""
    import inspect
    import bot_strategy as bs
    for fn in (bs.strategy_brain_s1, bs.strategy_brain_s2):
        src = inspect.getsource(fn)
        assert "get_asset_config" in src, f"{fn.__name__} must use get_asset_config for entry band"
        assert "fv_max_entry_price_cents" in src, f"{fn.__name__} missing fv_max_entry_price_cents key"


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
