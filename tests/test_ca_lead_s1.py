"""
Tests for S1 MOMENTUM (buy continuation of a fresh, confirmed spot move), plus the
betas.json loader and the _sigma_eff blend that S1 still uses for its window sigma.
"""
import sys, os, time, json, collections
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
import asset_manager
import bot_strategy as bs
from bot_strategy import strategy_brain_s1, _S1_MOM_CONFIG


@pytest.fixture(autouse=True)
def _clean_state():
    saved = {a: asset_manager._prices.get(a) for a in ("BTC", "SOL", "XRP", "DOGE", "ETH")}
    for _clear in (bot_state._s1_pending_trades, bot_state._s1_asset_trade_times,
                   bot_state._s1_cooldown_until, bot_state._sigma_scale,
                   bot_state._implied_sigma, bot_state._live_betas,
                   bot_state._contract_mid_history):
        _clear.clear()
    yield
    for a, dq in saved.items():
        if dq is not None:
            asset_manager._prices[a] = dq
    for _clear in (bot_state._s1_pending_trades, bot_state._s1_asset_trade_times,
                   bot_state._s1_cooldown_until, bot_state._sigma_scale,
                   bot_state._implied_sigma, bot_state._live_betas,
                   bot_state._contract_mid_history):
        _clear.clear()


def _seed(asset, last, ret_over_window, n=60, span=90.0):
    """Monotonic (ts, price) ramp ending at `last`, having moved ret_over_window (log)
    across `span` seconds. Monotonic => the momentum micro-confirmation agrees in sign."""
    now = time.time()
    start = last / (2.718281828 ** ret_over_window)
    dq = collections.deque(maxlen=2000)
    for i in range(n):
        t = now - (n - 1 - i) * (span / (n - 1))
        dq.append((t, start + (last - start) * (i / (n - 1))))
    asset_manager._prices[asset] = dq
    return dq


def _seed_reversal(asset, last, up_then_down=True, n=90, span=90.0):
    """Rise over the full window but reverse in the final third (fails confirmation)."""
    now = time.time()
    dq = collections.deque(maxlen=2000)
    peak = last * (1.006 if up_then_down else 0.994)      # extreme near 2/3 in
    base = last * (0.994 if up_then_down else 1.006)       # opposite extreme at the start
    for i in range(n):
        t = now - (n - 1 - i) * (span / (n - 1))
        frac = i / (n - 1)
        if frac <= 0.66:
            p = base + (peak - base) * (frac / 0.66)
        else:
            p = peak + (last - peak) * ((frac - 0.66) / 0.34)
        dq.append((t, p))
    asset_manager._prices[asset] = dq
    return dq


def _run_s1(asset, strike, yes_ask, no_ask, secs_left=420.0, cfg_extra=None, spot=None):
    config = {"mode": "paper", "quiet_hours_enabled": False, "calibration_enabled": False,
              "auto_gate_enabled": False, "staleness_gate_enabled": False}
    if cfg_extra:
        config.update(cfg_extra)
    if spot is None:
        dq = asset_manager._prices.get(asset)
        spot = dq[-1][1] if dq else strike
    with patch("bot_strategy.read_config", return_value=config), \
         patch("bot_strategy._time_of_day_vol_multiplier", return_value=1.0):
        return strategy_brain_s1(
            btc_price=spot, strike=strike, yes_ask=yes_ask, no_ask=no_ask,
            elapsed_seconds=900.0 - secs_left, secs_left=secs_left,
            ticker=f"KX{asset}-MOM", asset=asset,
        )


# --------------------------------------------------------------------------- betas loader

def test_betas_loader_reads_file():
    betas = bs._load_betas()
    assert betas["BTC"] == pytest.approx(1.0)
    assert 0.2 < betas["SOL"] < 0.7
    assert 0.2 < betas["XRP"] < 0.7


def test_betas_loader_fallback_on_missing(monkeypatch):
    monkeypatch.setattr(bs, "_BETA_PATH", "/nonexistent/does/not/exist.json")
    monkeypatch.setattr(bs, "_BETA_CACHE", {})
    monkeypatch.setattr(bs, "_BETA_MTIME", -1.0)
    betas = bs._load_betas()
    assert betas["SOL"] == pytest.approx(bs._BETA_DEFAULTS["SOL"])


def test_betas_loader_mtime_refresh(monkeypatch, tmp_path):
    p = tmp_path / "betas.json"
    p.write_text(json.dumps({"SOL": {"beta": 0.30}}))
    monkeypatch.setattr(bs, "_BETA_PATH", str(p))
    monkeypatch.setattr(bs, "_BETA_CACHE", {})
    monkeypatch.setattr(bs, "_BETA_MTIME", -1.0)
    assert bs._asset_beta("SOL") == pytest.approx(0.30)
    p.write_text(json.dumps({"SOL": {"beta": 0.50}}))
    os.utime(str(p), (time.time() + 10, time.time() + 10))
    assert bs._asset_beta("SOL") == pytest.approx(0.50)


def test_sigma_eff_within_static_clamp():
    base = bs._ASSET_VOL_15M["SOL"]
    s = bs._sigma_eff("SOL")
    assert bs._FLOOR_MULT * base <= s <= bs._CEIL_MULT * base


def test_asset_beta_shrinks_live_toward_static():
    static = bs._load_betas()["SOL"]
    bot_state._live_betas["SOL"] = 1.4   # contemporaneous-style overwrite: rejected
    assert bs._asset_beta("SOL") == pytest.approx(static)
    bot_state._live_betas["SOL"] = 0.6   # inside [0.5x, 1.5x] of static: shrunk halfway
    assert bs._asset_beta("SOL") == pytest.approx(0.5 * static + 0.5 * 0.6)


# --------------------------------------------------------------------------- S1 momentum

def test_s1_fires_on_up_move_buys_yes():
    """A fresh +0.4% move with the strike just below spot -> mid-priced YES -> trade YES."""
    _seed("SOL", last=150.6, ret_over_window=0.004)
    r = _run_s1("SOL", strike=150.55, yes_ask=56.0, no_ask=47.0)
    assert r["action"] == "trade", f"S1 should fire: {r['reasoning']}"
    assert r["side"] == "yes"
    assert r["signals"]["r"] > 0
    assert r["signals"]["ev"] >= 0.03
    assert "model_raw_p_yes" in r["signals"]
    assert "sigma_eff" in r["signals"] and "z" in r["signals"]


def test_s1_fires_on_down_move_buys_no():
    """A fresh -0.4% move with the strike just above spot -> mid-priced NO -> trade NO."""
    _seed("SOL", last=150.0, ret_over_window=-0.004)
    r = _run_s1("SOL", strike=150.05, yes_ask=45.0, no_ask=58.0)
    assert r["action"] == "trade", f"S1 should fire NO: {r['reasoning']}"
    assert r["side"] == "no"
    assert r["signals"]["r"] < 0


def test_s1_skips_when_flat():
    """No real move -> below the min-sigma floor -> s1_mom_flat skip."""
    _seed("SOL", last=150.0, ret_over_window=0.00002)
    r = _run_s1("SOL", strike=149.9, yes_ask=56.0, no_ask=47.0)
    assert r["action"] == "skip"
    assert "s1_mom_flat" in r["reasoning"], r["reasoning"]


def test_s1_skips_on_reversal():
    """Full-window up but reversing in the final third -> confirmation fails."""
    _seed_reversal("SOL", last=150.0, up_then_down=True)
    r = _run_s1("SOL", strike=149.9, yes_ask=56.0, no_ask=47.0)
    assert r["action"] == "skip"
    assert "s1_no_confirm" in r["reasoning"], r["reasoning"]


def test_s1_price_filter_band():
    """Real move but the moving side's ask is above the 75c band -> price filter."""
    _seed("SOL", last=150.6, ret_over_window=0.004)
    r = _run_s1("SOL", strike=150.55, yes_ask=85.0, no_ask=18.0)
    assert r["action"] == "skip"
    assert "s1_price_filter" in r["reasoning"], r["reasoning"]
    assert r.get("price_filter_skip") is True


def test_s1_ev_gate_blocks_thin_edge():
    """A real move but the ask is rich enough that EV < min_edge -> ev gate (full signals)."""
    _seed("SOL", last=150.15, ret_over_window=0.0016)   # just over the min-sigma floor
    r = _run_s1("SOL", strike=150.10, yes_ask=74.0, no_ask=29.0)
    assert r["action"] == "skip"
    assert "s1_ev_gate" in r["reasoning"], r["reasoning"]
    assert "model_raw_p_yes" in r["signals"]


def test_s1_time_gate_final_stretch():
    _seed("SOL", last=150.6, ret_over_window=0.004)
    r = _run_s1("SOL", strike=150.55, yes_ask=56.0, no_ask=47.0, secs_left=60.0)
    assert r["action"] == "skip"
    assert "s1_time_gate" in r["reasoning"]


def test_s1_require_btc_confirm_blocks_disagreement():
    """With the hard BTC-confirm flag on, a SOL up-move while BTC fell is blocked."""
    _seed("SOL", last=150.6, ret_over_window=0.004)
    _seed("BTC", last=60000.0, ret_over_window=-0.006)   # BTC down => disagrees with SOL up
    r = _run_s1("SOL", strike=150.55, yes_ask=56.0, no_ask=47.0,
                cfg_extra={"s1_require_btc_confirm": True})
    assert r["action"] == "skip"
    assert "s1_btc_disagree" in r["reasoning"], r["reasoning"]


def test_s1_btc_disabled_by_default():
    _seed("BTC", last=60000.0, ret_over_window=0.006)
    r = _run_s1("BTC", strike=59900.0, yes_ask=56.0, no_ask=47.0, spot=60000.0)
    assert r["action"] == "skip"
    assert "s1_disabled:BTC" in r["reasoning"]


def test_s1_eth_disabled_by_default():
    _seed("ETH", last=3000.0, ret_over_window=0.004)
    r = _run_s1("ETH", strike=2995.0, yes_ask=56.0, no_ask=47.0)
    assert r["action"] == "skip"
    assert "s1_disabled:ETH" in r["reasoning"]


def test_all_momentum_assets_configured():
    for a in ("SOL", "XRP", "DOGE"):
        assert a in _S1_MOM_CONFIG
        assert _S1_MOM_CONFIG[a]["lookback"] > 0
        assert _S1_MOM_CONFIG[a]["min_sigma"] > 0
