"""
Tests for the model upgrades: websocket feed tick handling, self-calibration
(prob_scale), and settlement-basis corrections (effective time + level offset).
"""
import sys, os, time, math, asyncio, sqlite3, tempfile
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
import asset_manager
import bot_strategy as bs
from scripts.calibration import (
    fit_prob_scale, fit_basis_offset, load_calibration, save_calibration,
    MIN_PROB_SAMPLES, MIN_BASIS_SAMPLES, BASIS_OFFSET_CAP,
)


@pytest.fixture(autouse=True)
def _neutral_calibration():
    """Reset calibration state and the BTC deque around each test."""
    saved_btc = list(asset_manager._prices["BTC"])
    bot_state._brain_cal_s1["prob_scale"] = 1.0
    bot_state._brain_cal_s2["prob_scale"] = 1.0
    bot_state._basis_offsets.clear()
    bot_state._sigma_scale.clear()
    bot_state._implied_sigma.clear()
    yield
    asset_manager._prices["BTC"].clear()
    asset_manager._prices["BTC"].extend(saved_btc)
    bot_state._brain_cal_s1["prob_scale"] = 1.0
    bot_state._brain_cal_s2["prob_scale"] = 1.0
    bot_state._basis_offsets.clear()
    bot_state._live_betas.clear()
    bot_state._auto_blocked_sessions.clear()
    bot_state._auto_blocked_assets.clear()
    bot_state._sigma_scale.clear()
    bot_state._implied_sigma.clear()
    bot_state._contract_mid_history.clear()


# websocket tick handler

class TestWsTickHandler:
    def test_appends_valid_ticker(self):
        asset_manager._prices["BTC"].clear()
        out = asset_manager._handle_ticker_message(
            {"type": "ticker", "product_id": "BTC-USD", "price": "60000.5"}, now_ts=1000.0)
        assert out == "BTC"
        assert asset_manager._prices["BTC"][-1] == (1000.0, 60000.5)

    def test_decimates_sub_second_ticks(self):
        asset_manager._prices["BTC"].clear()
        m = {"type": "ticker", "product_id": "BTC-USD", "price": "60000"}
        assert asset_manager._handle_ticker_message(m, now_ts=1000.0) == "BTC"
        assert asset_manager._handle_ticker_message(m, now_ts=1000.4) is None
        assert asset_manager._handle_ticker_message(m, now_ts=1000.9) is None
        assert asset_manager._handle_ticker_message(m, now_ts=1001.1) == "BTC"
        assert len(asset_manager._prices["BTC"]) == 2

    def test_ignores_non_ticker_and_unknown_product(self):
        assert asset_manager._handle_ticker_message(
            {"type": "subscriptions"}, now_ts=1.0) is None
        assert asset_manager._handle_ticker_message(
            {"type": "ticker", "product_id": "SHIB-USD", "price": "1"}, now_ts=1.0) is None

    def test_ignores_bad_price(self):
        assert asset_manager._handle_ticker_message(
            {"type": "ticker", "product_id": "BTC-USD", "price": "nope"}, now_ts=1.0) is None
        assert asset_manager._handle_ticker_message(
            {"type": "ticker", "product_id": "BTC-USD", "price": "-5"}, now_ts=1.0) is None
        assert asset_manager._handle_ticker_message(
            {"type": "ticker", "product_id": "BTC-USD"}, now_ts=1.0) is None


# calibration fitters

class TestFitters:
    def test_prob_scale_recovers_known_miscalibration(self):
        import random
        random.seed(7)
        rows = []
        for _ in range(3000):
            p = random.uniform(0.2, 0.8)
            true_p = 0.5 + 0.6 * (p - 0.5)   # model overconfident by 1/0.6
            rows.append((p, "yes" if random.random() < true_p else "no"))
        w = fit_prob_scale(rows)
        assert abs(w - 0.6) < 0.1, f"expected ~0.6, got {w}"

    def test_prob_scale_gate_and_neutral(self):
        rows = [(0.6, "yes")] * (MIN_PROB_SAMPLES - 1)
        assert fit_prob_scale(rows) == 1.0
        assert fit_prob_scale([]) == 1.0

    def test_prob_scale_clamps(self):
        # A perfectly informative model would fit w >> 1.2: clamp at the ceiling.
        rows = [(0.6, "yes"), (0.4, "no")] * 200
        assert fit_prob_scale(rows) == 1.2

    def test_basis_offset_recovers_threshold(self):
        import random
        random.seed(3)
        rows = []
        for _ in range(500):
            d = random.uniform(-0.003, 0.003)
            rows.append((d, "yes" if d > 0.0004 else "no"))
        off = fit_basis_offset(rows)
        assert abs(off - 0.0004) < 0.0002

    def test_basis_offset_gate_and_clamp(self):
        assert fit_basis_offset([]) == 0.0
        thin = [(0.001, "yes")] * (MIN_BASIS_SAMPLES - 1)
        assert fit_basis_offset(thin) == 0.0
        import random
        random.seed(5)
        dists = [random.uniform(-0.02, 0.02) for _ in range(400)]
        wide = [(d, "yes" if d > 0.008 else "no") for d in dists]
        assert fit_basis_offset(wide) == BASIS_OFFSET_CAP

    def test_save_load_roundtrip(self, tmp_path):
        p = str(tmp_path / "calibration.json")
        data = {"prob_scale": {"strategy1": 0.9}, "basis_offset": {"SOL": 0.0002}}
        assert save_calibration(data, path=p)
        assert load_calibration(path=p) == data
        assert load_calibration(path=str(tmp_path / "missing.json")) == {}


# strategy-side application

class TestBrainApplication:
    def test_effective_secs(self):
        assert bs._effective_secs(600.0, {}) == 570.0                       # default 60 -> -30
        assert bs._effective_secs(600.0, {"settlement_avg_seconds": 0}) == 600.0
        assert bs._effective_secs(10.0, {"settlement_avg_seconds": 120}) == 1.0   # floor
        assert bs._effective_secs(600.0, {"settlement_avg_seconds": 9999}) == 450.0  # avg clamp 300

    def test_effective_secs_raises_p_above_when_itm(self):
        # Spot above strike: less effective time = higher P(stay above).
        p_raw = bs._bachelier_p_above(150.7, 150.0, 600.0, 0.012)
        p_eff = bs._bachelier_p_above(150.7, 150.0, bs._effective_secs(600.0, {}), 0.012)
        assert p_eff > p_raw

    def test_calibrated_p_neutral_and_scaled(self):
        cfg = {"calibration_enabled": True}
        assert bs._calibrated_p(0.7, "strategy2", cfg) == pytest.approx(0.7)
        bot_state._brain_cal_s2["prob_scale"] = 0.5
        assert bs._calibrated_p(0.7, "strategy2", cfg) == pytest.approx(0.6)
        bot_state._brain_cal_s1["prob_scale"] = 0.5
        assert bs._calibrated_p(0.7, "strategy1", cfg) == pytest.approx(0.6)

    def test_calibrated_p_kill_switch_and_clamp(self):
        bot_state._brain_cal_s2["prob_scale"] = 0.5
        assert bs._calibrated_p(0.7, "strategy2", {"calibration_enabled": False}) == 0.7
        bot_state._brain_cal_s2["prob_scale"] = 99.0   # corrupt slot: clamped to 1.2
        assert bs._calibrated_p(0.7, "strategy2", {}) == pytest.approx(0.5 + 1.2 * 0.2)

    def test_basis_adjusted_spot(self):
        assert bs._basis_adjusted_spot(150.0, "SOL") == 150.0
        bot_state._basis_offsets["SOL"] = 0.0005
        adj = bs._basis_adjusted_spot(150.0, "SOL")
        assert adj == pytest.approx(150.0 * math.exp(-0.0005))
        bot_state._basis_offsets["SOL"] = 5.0   # absurd: clamped to 10bp
        assert bs._basis_adjusted_spot(150.0, "SOL") >= 150.0 * math.exp(-0.0011)


# recalibration job integration

def test_recalibrate_model_updates_state_and_persists(tmp_path):
    import bot_infra
    import bot_loops
    import scripts.calibration as calmod

    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    old_db = bot_state._DB_FILE
    bot_state._DB_FILE = f.name
    saved = {}
    try:
        bot_infra.init_db()
        conn = sqlite3.connect(f.name)
        # Seed decision_log: strategy2 model overconfident (true scale ~0.6).
        import random
        random.seed(11)
        rows = []
        for i in range(600):
            p = random.uniform(0.2, 0.8)
            true_p = 0.5 + 0.6 * (p - 0.5)
            rows.append(("2026-07-01T14:00:00+00:00", f"KX{i}", "SOL", "strategy2", "paper",
                         "yes", p, 0.5, 0.05, 45.0, 300.0, 1,
                         "yes" if random.random() < true_p else "no"))
        conn.executemany(
            "INSERT INTO decision_log (ts,ticker,asset,strategy,mode,side,model_p_yes,"
            "market_mid_p_yes,market_edge,entry_price_cents,secs_left,would_trade,outcome) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        # Seed settlement_basis: SOL threshold at +0.0003.
        brows = []
        for i in range(300):
            d = random.uniform(-0.002, 0.002)
            brows.append(("2026-07-01T14:00:00+00:00", f"KXB{i}", "SOL", 150.0,
                          150.0 * (1 + d), "yes" if d > 0.0003 else "no",
                          "yes" if d > 0 else "no", 1, d))
        conn.executemany(
            "INSERT INTO settlement_basis (ts,ticker,asset,strike,our_spot,kalshi,ours,"
            "agree,signed_dist) VALUES (?,?,?,?,?,?,?,?,?)", brows)
        conn.commit()
        conn.close()

        with patch.object(calmod, "save_calibration",
                          side_effect=lambda data, path=None: saved.update(data) or True):
            asyncio.run(bot_loops._recalibrate_model({}))

        s2 = bot_state._brain_cal_s2["prob_scale"]
        assert abs(s2 - 0.6) < 0.15, f"expected ~0.6 fit, got {s2}"
        assert bot_state._brain_cal_s1["prob_scale"] == 1.0   # no strategy1 rows: neutral
        assert abs(bot_state._basis_offsets.get("SOL", 0.0) - 0.0003) < 0.0002
        assert saved["prob_scale"]["strategy2"] == s2
    finally:
        bot_state._DB_FILE = old_db
        os.unlink(f.name)


# rolling betas + auto-gates + maker model

def test_fit_rolling_beta_recovers_and_gates():
    import math, random
    from scripts.calibration import fit_rolling_beta
    random.seed(2)
    now = time.time()
    btc, alt = [], []
    p_b, p_a = 60000.0, 150.0
    for i in range(1800):
        r = random.gauss(0, 0.0002)
        p_b *= math.exp(r)
        p_a *= math.exp(0.5 * r + random.gauss(0, 0.00005))
        t = now - (1800 - i)
        btc.append((t, p_b))
        alt.append((t, p_a))
    beta, n = fit_rolling_beta(btc, alt)
    assert beta is not None and abs(beta - 0.5) < 0.15
    thin_beta, _ = fit_rolling_beta(btc[:60], alt[:60])
    assert thin_beta is None
    assert fit_rolling_beta([], alt) == (None, 0)


def test_asset_beta_prefers_live_fit():
    static = bs._load_betas()["SOL"]
    bot_state._live_betas["SOL"] = 0.5    # inside [0.5x, 1.5x] of static: shrunk halfway
    assert bs._asset_beta("SOL") == pytest.approx(0.5 * static + 0.5 * 0.5)
    bot_state._live_betas["SOL"] = 99.0   # out of range: ignored
    assert bs._asset_beta("SOL") == pytest.approx(static)
    bot_state._live_betas["SOL"] = 1.4    # contemporaneous-scale value: rejected too
    assert bs._asset_beta("SOL") == pytest.approx(static)


def test_compute_auto_blocks_targets_losing_buckets():
    import sessions
    from scripts.calibration import compute_auto_blocks, AUTOGATE_MIN_N
    from scripts.edge_report import _pnl_stats
    losing = [{"ts": "2026-07-01T04:00:00+00:00", "strategy": "strategy2", "asset": "SOL",
               "side": "yes", "outcome": "no", "entry_price_cents": 55.0}] * AUTOGATE_MIN_N
    winning = [{"ts": "2026-07-01T14:00:00+00:00", "strategy": "strategy2", "asset": "XRP",
                "side": "yes", "outcome": "yes", "entry_price_cents": 45.0}] * AUTOGATE_MIN_N
    thin = [{"ts": "2026-07-01T20:00:00+00:00", "strategy": "strategy1", "asset": "DOGE",
             "side": "yes", "outcome": "no", "entry_price_cents": 55.0}] * 10
    b = compute_auto_blocks(losing + winning + thin, sessions.session_for_iso, _pnl_stats)
    assert b["sessions"] == ["overnight"]
    assert b["strategy_assets"] == [["strategy2", "SOL"]]   # DOGE under the n-gate


def test_auto_gate_blocks_brain_after_ev_pass():
    from collections import deque
    now = time.time()
    saved = asset_manager._prices.get("SOL")
    try:
        # Late window + favorite mid ~0.77: S2 passes every gate and reaches the auto-gate.
        asset_manager._prices["SOL"] = deque(
            [(now - (40 - i) * 2, 150.30 + 0.14 * (i / 39.0)) for i in range(40)], maxlen=2000)
        bot_state._auto_blocked_assets.add(("strategy2", "SOL"))
        with patch("bot_strategy.read_config",
                   return_value={"mode": "paper", "quiet_hours_enabled": False,
                                 "calibration_enabled": False}), \
             patch("bot_strategy._time_of_day_vol_multiplier", return_value=1.0):
            r = bs.strategy_brain_s2(150.44, 150.0, 78.0, 24.0, 660.0, 240.0, "KXSOL-AG", asset="SOL")
        assert r["action"] == "skip"
        assert r["reasoning"] == "s2_auto_gate:SOL"
        assert "model_raw_p_yes" in r["signals"]   # still visible to the harness
        # kill switch restores trading
        with patch("bot_strategy.read_config",
                   return_value={"mode": "paper", "quiet_hours_enabled": False,
                                 "calibration_enabled": False, "auto_gate_enabled": False}), \
             patch("bot_strategy._time_of_day_vol_multiplier", return_value=1.0):
            r2 = bs.strategy_brain_s2(150.44, 150.0, 78.0, 24.0, 660.0, 240.0, "KXSOL-AG2", asset="SOL")
        assert r2["action"] == "trade"
    finally:
        if saved is not None:
            asset_manager._prices["SOL"] = saved


def test_session_allowed_includes_auto_blocked():
    import sessions
    cur = sessions.now_session()
    bot_state._auto_blocked_sessions.add(cur)
    ok, label = bs._session_allowed({})
    assert ok is False and label == cur
    ok2, _ = bs._session_allowed({"auto_gate_enabled": False})
    assert ok2 is True


def test_maker_counterfactual_returns_sample():
    import bot_loops
    now = time.time()
    ticker = "KXSOL-MKCF"
    pos = {"ticker": ticker, "side": "yes", "entry_price_cents": 45.0,
           "entry_ts": now - 300, "contracts": 50}
    # Ask path touches 44c (= entry 45 - 1) -> maker fill.
    bot_state._maker_track[ticker] = [(now - 200, 44.0, 58.0)]
    sample = asyncio.run(bot_loops._record_maker_counterfactual(pos, "SOL", "win", {"mode": "paper"}))
    assert sample is not None and sample["filled"] is True
    assert sample["maker_price_cents"] == 44.0
    # No touch -> unfilled sample.
    bot_state._maker_track[ticker] = [(now - 200, 47.0, 58.0)]
    pos2 = dict(pos)
    sample2 = asyncio.run(bot_loops._record_maker_counterfactual(pos2, "SOL", "loss", {"mode": "paper"}))
    assert sample2 is not None and sample2["filled"] is False
    assert sample2["maker_pnl"] is None


def test_record_settlement_basis_persists_to_db():
    import bot_infra
    import bot_loops

    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    old_db = bot_state._DB_FILE
    bot_state._DB_FILE = f.name
    try:
        bot_infra.init_db()

        async def run():
            bot_loops._record_settlement_basis(
                "KXSOL-TEST", "SOL", 150.0, 150.4, "yes", settled_official=True)
            await asyncio.sleep(0.1)   # let the fire-and-forget DB write land

        asyncio.run(run())
        conn = sqlite3.connect(f.name)
        row = conn.execute(
            "SELECT asset, kalshi, ours, agree, signed_dist FROM settlement_basis").fetchone()
        conn.close()
        assert row is not None, "basis sample was not persisted"
        assert row[0] == "SOL" and row[1] == "yes" and row[2] == "yes" and row[3] == 1
        assert abs(row[4] - (150.4 - 150.0) / 150.0) < 1e-9
    finally:
        bot_state._DB_FILE = old_db
        os.unlink(f.name)
