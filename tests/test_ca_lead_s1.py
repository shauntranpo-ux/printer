"""
Tests for the new S1 (CA-LEAD-SLOW: BTC-lead cross-asset residual dislocation),
the betas.json loader, and the _sigma_eff blend.
"""
import sys, os, time, json, collections
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
import asset_manager
import bot_strategy as bs
from bot_strategy import strategy_brain_s1, _S1_CA_CONFIG


@pytest.fixture(autouse=True)
def _clean_state():
    saved = {a: asset_manager._prices.get(a) for a in ("BTC", "SOL", "XRP", "DOGE")}
    bot_state._s1_pending_trades.clear()
    bot_state._s1_asset_trade_times.clear()
    bot_state._s1_cooldown_until.clear()
    bot_state._sigma_scale.clear()
    bot_state._implied_sigma.clear()
    bot_state._live_betas.clear()
    bot_state._contract_mid_history.clear()
    yield
    for a, dq in saved.items():
        if dq is not None:
            asset_manager._prices[a] = dq
    bot_state._s1_pending_trades.clear()
    bot_state._s1_asset_trade_times.clear()
    bot_state._s1_cooldown_until.clear()
    bot_state._sigma_scale.clear()
    bot_state._implied_sigma.clear()
    bot_state._live_betas.clear()
    bot_state._contract_mid_history.clear()


def _seed(asset, last, ret_over_window, n=40, span=78.0):
    """Seed a (ts, price) deque ending at `last`, having moved ret_over_window (log) across it."""
    now = time.time()
    start = last / (2.718281828 ** ret_over_window)
    dq = collections.deque(maxlen=2000)
    for i in range(n):
        t = now - (n - 1 - i) * (span / (n - 1))
        dq.append((t, start + (last - start) * (i / (n - 1))))
    asset_manager._prices[asset] = dq
    return dq


def _run_s1(asset, spot, strike, yes_ask, no_ask, secs_left=480.0, cfg_extra=None):
    config = {"mode": "paper", "quiet_hours_enabled": False}
    if cfg_extra:
        config.update(cfg_extra)
    # Pin the ToD multiplier so the sigma fallback (and every z-derived gate) is
    # deterministic regardless of the wall clock the suite runs at.
    with patch("bot_strategy.read_config", return_value=config), \
         patch("bot_strategy._time_of_day_vol_multiplier", return_value=1.0):
        return strategy_brain_s1(
            btc_price=spot, strike=strike, yes_ask=yes_ask, no_ask=no_ask,
            elapsed_seconds=900.0 - secs_left, secs_left=secs_left,
            ticker=f"KX{asset}-CA", asset=asset,
        )


# --------------------------------------------------------------------------- betas loader

def test_betas_loader_reads_file():
    betas = bs._load_betas()
    assert betas["BTC"] == pytest.approx(1.0)
    assert 0.2 < betas["SOL"] < 0.7
    assert 0.2 < betas["XRP"] < 0.7


def test_betas_loader_fallback_on_missing(monkeypatch):
    """A missing betas file must fall back to defaults, never raise."""
    monkeypatch.setattr(bs, "_BETA_PATH", "/nonexistent/does/not/exist.json")
    monkeypatch.setattr(bs, "_BETA_CACHE", {})
    monkeypatch.setattr(bs, "_BETA_MTIME", -1.0)
    betas = bs._load_betas()
    assert betas["SOL"] == pytest.approx(bs._BETA_DEFAULTS["SOL"])


def test_betas_loader_mtime_refresh(monkeypatch, tmp_path):
    """Editing the file (new mtime) must refresh the cache."""
    p = tmp_path / "betas.json"
    p.write_text(json.dumps({"SOL": {"beta": 0.30}}))
    monkeypatch.setattr(bs, "_BETA_PATH", str(p))
    monkeypatch.setattr(bs, "_BETA_CACHE", {})
    monkeypatch.setattr(bs, "_BETA_MTIME", -1.0)
    assert bs._asset_beta("SOL") == pytest.approx(0.30)
    # Rewrite with a new value and bump mtime -> loader must pick it up.
    p.write_text(json.dumps({"SOL": {"beta": 0.50}}))
    os.utime(str(p), (time.time() + 10, time.time() + 10))
    assert bs._asset_beta("SOL") == pytest.approx(0.50)


# --------------------------------------------------------------------------- sigma_eff

def test_sigma_eff_within_static_clamp():
    base = bs._ASSET_VOL_15M["SOL"]
    s = bs._sigma_eff("SOL")
    assert bs._FLOOR_MULT * base <= s <= bs._CEIL_MULT * base


# --------------------------------------------------------------------------- S1 fires / skips

def test_s1_fires_on_btc_lead_dislocation():
    """BTC jumped, SOL lagged -> positive residual -> predicted SOL up -> YES cheap -> trade.

    Asks sit near fair (gap under the TGTBT cap): fair ~0.86 vs de-vigged mid 0.77.
    """
    _seed("BTC", last=60000.0, ret_over_window=0.008)   # BTC +~0.8%
    _seed("SOL", last=150.03, ret_over_window=0.0001)    # SOL nearly flat
    r = _run_s1("SOL", spot=150.03, strike=149.9, yes_ask=78.0, no_ask=24.0)
    assert r["action"] == "trade", f"S1 should fire: {r['reasoning']}"
    assert r["side"] == "yes"
    assert r["signals"]["residual"] > 0
    assert r["signals"]["market_edge"] >= 0.04
    assert r["signals"]["gap"] <= 0.15
    assert "model_raw_p_yes" in r["signals"]
    assert "sigma_eff" in r["signals"] and "z" in r["signals"]


def test_s1_skips_when_btc_flat():
    """No BTC lead -> no residual signal -> skip."""
    _seed("BTC", last=60000.0, ret_over_window=0.00001)  # BTC flat
    _seed("SOL", last=150.03, ret_over_window=0.00001)
    r = _run_s1("SOL", spot=150.03, strike=149.9, yes_ask=46.0, no_ask=54.0)
    assert r["action"] == "skip"
    assert "s1_ca_btc_flat" in r["reasoning"] or "s1_ca_resid_flat" in r["reasoning"]


def test_s1_skips_when_alt_already_followed():
    """BTC moved AND SOL already followed by beta*btc_ret -> residual ~0 -> skip."""
    _seed("BTC", last=60000.0, ret_over_window=0.008)
    _seed("SOL", last=150.0, ret_over_window=0.008 * bs._asset_beta("SOL"))  # already caught up
    r = _run_s1("SOL", spot=150.0, strike=149.5, yes_ask=46.0, no_ask=54.0)
    assert r["action"] == "skip"
    assert "s1_ca_resid_flat" in r["reasoning"]


def test_s1_fade_mode_is_gated():
    """SOL OVERSHOT BTC's lead (residual sign opposite the BTC move) -> s1_ca_fade skip
    with full signals so the shadow measurement still reaches the harness."""
    _seed("BTC", last=60000.0, ret_over_window=0.008)
    _seed("SOL", last=150.03, ret_over_window=0.008)   # alt moved MORE than beta*btc
    r = _run_s1("SOL", spot=150.03, strike=149.9, yes_ask=46.0, no_ask=54.0)
    assert r["action"] == "skip"
    assert "s1_ca_fade" in r["reasoning"], f"expected fade gate: {r['reasoning']}"
    assert "model_raw_p_yes" in r["signals"]


def test_s1_tgtbt_rejects_extreme_gap():
    """Same dislocation but asks far below fair (gap ~0.40) -> reject, don't clamp."""
    _seed("BTC", last=60000.0, ret_over_window=0.008)
    _seed("SOL", last=150.03, ret_over_window=0.0001)
    r = _run_s1("SOL", spot=150.03, strike=149.9, yes_ask=46.0, no_ask=54.0)
    assert r["action"] == "skip"
    assert "s1_tgtbt" in r["reasoning"], f"expected tgtbt gate: {r['reasoning']}"
    assert r["signals"]["gap"] > 0.15


def test_s1_tail_ban_blocks_longshot_side():
    """Side priced under 25c on the de-vigged mid -> tail ban."""
    _seed("BTC", last=60000.0, ret_over_window=0.008)
    _seed("SOL", last=150.03, ret_over_window=0.0001)
    r = _run_s1("SOL", spot=150.03, strike=149.9, yes_ask=22.0, no_ask=80.0)
    assert r["action"] == "skip"
    assert "s1_tail_ban" in r["reasoning"], f"expected tail ban: {r['reasoning']}"


def test_s1_min_btc_ret_raised():
    """BTC moved 0.05% over the window (below the 0.10% floor) -> btc_flat skip."""
    _seed("BTC", last=60000.0, ret_over_window=0.0008)   # ~0.06% over the 60s lookback
    _seed("SOL", last=150.03, ret_over_window=0.0001)
    r = _run_s1("SOL", spot=150.03, strike=149.9, yes_ask=78.0, no_ask=24.0)
    assert r["action"] == "skip"
    assert "s1_ca_btc_flat" in r["reasoning"]


def test_asset_beta_shrinks_live_toward_static():
    """Live lead beta accepted only near the static value, then shrunk halfway to it."""
    static = bs._load_betas()["SOL"]
    bot_state._live_betas["SOL"] = 1.4   # contemporaneous-style overwrite: rejected
    assert bs._asset_beta("SOL") == pytest.approx(static)
    bot_state._live_betas["SOL"] = 0.6   # inside [0.5x, 1.5x] of static: shrunk
    assert bs._asset_beta("SOL") == pytest.approx(0.5 * static + 0.5 * 0.6)


def test_s1_btc_disabled_by_default():
    _seed("BTC", last=60000.0, ret_over_window=0.008)
    r = _run_s1("BTC", spot=60000.0, strike=59900.0, yes_ask=46.0, no_ask=54.0)
    assert r["action"] == "skip"
    assert "s1_ca_disabled:BTC" in r["reasoning"]


def test_s1_eth_disabled_by_default():
    _seed("BTC", last=60000.0, ret_over_window=0.008)
    _seed("ETH", last=3000.0, ret_over_window=0.0001)
    r = _run_s1("ETH", spot=3000.0, strike=2990.0, yes_ask=46.0, no_ask=54.0)
    assert r["action"] == "skip"
    assert "s1_ca_disabled:ETH" in r["reasoning"]


def test_s1_time_gate_final_90s():
    _seed("BTC", last=60000.0, ret_over_window=0.008)
    _seed("SOL", last=150.03, ret_over_window=0.0001)
    r = _run_s1("SOL", spot=150.03, strike=149.9, yes_ask=46.0, no_ask=54.0, secs_left=60.0)
    assert r["action"] == "skip"
    assert "s1_time_gate" in r["reasoning"]


def test_all_ca_assets_configured():
    for a in ("SOL", "XRP", "DOGE"):
        assert a in _S1_CA_CONFIG
        assert _S1_CA_CONFIG[a]["lookback"] > 0
