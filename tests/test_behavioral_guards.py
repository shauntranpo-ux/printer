"""
Behavioral replacements for source-text tests that mutation testing showed were
toothless: the READY-phase lab dispatch (an ask-swap at the call site passed the
whole suite), the settle-before-maker-pop ordering in handle_locked_phase (making
the settle unreachable passed), the aged-pending sweep (an inverted age comparison
passed), S7's traded direction (a sign flip passed), and S1's model_raw_p_yes
value (setting it to None passed). Each test here drives the REAL production code
path and fails under those exact mutations.
"""
import sys
import os
import time
import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import asset_manager
import bot_state
import bot_infra
import bot_risk
import bot_loops
import bot_strategy as bs


_CFG = {"mode": "paper", "kalshi_fee_per_contract_cents": 7,
        "quiet_hours_enabled": False, "auto_gate_enabled": False,
        "calibration_enabled": False, "staleness_gate_enabled": False,
        "measurement_enabled": False, "trade_amount_dollars": 25,
        "strategy_duel_mode": True, "notify_on_settle": False,
        "ladder_max_strikes": 1}


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    def __init__(self, results=None):
        self.results = results or {}

    def get(self, url, **kw):
        return _FakeResp({"market": {"result": self.results.get(url.rsplit("/", 1)[-1])}})


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_state, "_DB_FILE", str(tmp_path / "bg.db"))
    bot_infra.init_db()
    saved = asset_manager._prices.get("SOL")
    for d in (bot_state._slot_state, bot_state._prev_window_outcome,
              bot_state._maker_track, bot_state._s1_pending_trades,
              bot_state._slot_activity):
        d.clear()
    yield
    if saved is not None:
        asset_manager._prices["SOL"] = saved
    for d in (bot_state._slot_state, bot_state._prev_window_outcome,
              bot_state._maker_track, bot_state._s1_pending_trades,
              bot_state._slot_activity):
        d.clear()


def _flat_tape(asset="SOL", base=150.0, n=120, span=600.0):
    now = time.time()
    dq = deque(maxlen=2000)
    for i in range(n):
        dq.append((now - span * (1 - i / (n - 1)), base * (1 + 0.00001 * (i % 3))))
    asset_manager._prices[asset] = dq


# ------------------------------------------------ READY dispatch passes the real book

def test_ready_phase_dispatch_hands_slots_the_actual_book(monkeypatch):
    """Drives the REAL handle_ready_phase: a recorder brain in the registry must
    receive the orderbook's yes/no asks in the right argument slots. Kills the
    ask-swap mutation at the dispatch call site."""
    _flat_tape("SOL")
    seen = []

    def _recorder(spot, strike, yes_ask, no_ask, elapsed, secs_left, ticker, asset="SOL"):
        seen.append({"spot": spot, "strike": strike, "yes_ask": yes_ask,
                     "no_ask": no_ask, "secs_left": secs_left, "ticker": ticker})
        return {"action": "skip", "side": "yes", "reasoning": "recorded",
                "signals": {}, "strategy_variant": "strategy4"}

    async def _ob(session, ticker, market=None):
        return {"best_yes_ask": 41, "best_no_ask": 59,
                "yes_liquidity": 500, "no_liquidity": 500, "obi": 0.0}

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(bot_loops, "fetch_orderbook", _ob)
    monkeypatch.setattr(bot_loops, "write_state_file", _noop)
    monkeypatch.setattr(bot_loops, "STRATEGY_REGISTRY",
                        {"strategy4": {"brain": _recorder, "tag": "s4",
                                       "version": "test", "max_pending_per_asset": 1}})
    monkeypatch.setattr(bot_loops, "enabled_slots", lambda cfg: ["strategy4"])
    monkeypatch.setattr(bot_risk, "place_order", _noop)

    now = datetime.now(timezone.utc)
    market = {"ticker": "SOLR-DISPATCH", "strike_price": 150.0,
              "open_time": _iso(now - timedelta(seconds=420)),
              "close_time": _iso(now + timedelta(seconds=480))}
    state = {"phase": "READY", "position": None, "order_attempted": set(), "eval": {}}
    with patch("bot_strategy.read_config", return_value=dict(_CFG)), \
         patch("bot_strategy._time_of_day_vol_multiplier", return_value=1.0):
        asyncio.run(bot_loops.handle_ready_phase(
            None, dict(_CFG), market, "SOLR-DISPATCH",
            asset_manager._prices["SOL"][-1][1], 480.0, 150.0, 420.0,
            asset="SOL", state=state))
    assert seen, "the lab dispatch never ran the registry brain"
    got = seen[0]
    assert got["yes_ask"] == 41 and got["no_ask"] == 59, got
    assert got["strike"] == 150.0 and got["ticker"] == "SOLR-DISPATCH"


# ------------------------------------------------ settle runs BEFORE maker-track pop

def test_locked_expiry_settles_s5_before_maker_evidence_is_destroyed(monkeypatch):
    """Drives the REAL handle_locked_phase on an expired S2 position with an S5
    maker quote pending on the same ticker: the quote's fill evidence lives in
    _maker_track, and the maker counterfactual pops it - the S5 row must settle as
    a real win, not void as unfilled. Kills the settle-unreachable mutation."""
    _flat_tape("SOL")
    updates = {}

    async def _upd(tid, fields, **kw):
        updates[tid] = fields

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(bot_risk, "db_update_trade", _upd)
    monkeypatch.setattr(bot_loops, "db_update_trade", _upd)
    monkeypatch.setattr(bot_loops, "write_state_file", _noop)
    monkeypatch.setattr(bot_loops, "send_telegram", _noop)

    entry_ts = time.time() - 300
    st5 = bot_risk._slot("strategy5")
    st5["pending"]["SOLR-EXP"] = {
        "slot": "strategy5", "asset": "SOL", "mode": "paper", "entry_ts": entry_ts,
        "market_close_time": "", "strike": 150.0, "maker_quote_cents": 45.0,
        "side": "yes", "contracts": 10, "entry_price_cents": 45, "trade_id": 55,
    }
    # Book crossed the quote after entry -> the quote DID fill.
    bot_state._maker_track["SOLR-EXP"] = [(entry_ts + 30, 44.0, 58.0)]

    now = datetime.now(timezone.utc)
    pos = {"ticker": "SOLR-EXP", "strike": 150.0, "side": "yes", "contracts": 5,
           "entry_price_cents": 50, "trade_id": 54, "mode": "paper",
           "market_close_time": _iso(now - timedelta(seconds=5)),
           "entry_ts": entry_ts}
    state = {"phase": "LOCKED", "position": pos, "order_attempted": set(), "eval": {}}
    cfg = dict(_CFG, maker_execution_enabled=False, measurement_enabled=True)
    fake = _FakeSession({"SOLR-EXP": "yes"})
    with patch("bot_strategy.read_config", return_value=cfg), \
         patch("bot_strategy._time_of_day_vol_multiplier", return_value=1.0), \
         patch.object(bot_loops, "kalshi_headers", lambda *a, **k: {}), \
         patch.object(bot_risk, "kalshi_headers", lambda *a, **k: {}):
        asyncio.run(bot_loops.handle_locked_phase(
            fake, 150.4, 0.0, cfg, asset="SOL", state=state))
    assert 55 in updates, "the S5 pending never settled at expiry"
    assert updates[55]["outcome"] == "win", updates[55]
    assert updates[54]["outcome"] == "win"          # S2's own settle still ran


# ------------------------------------------------ aged-pending sweep behavior

def test_periodic_maintenance_settles_aged_pendings_only():
    """Fresh pendings stay; pendings older than a window+grace settle via the
    official result; stale maker-track entries are pruned. Kills the inverted
    age-comparison mutation the old source-grep test let through."""
    _flat_tape("SOL")
    now = time.time()
    st = bot_risk._slot("strategy4")
    st["pending"]["SOLR-FRESH"] = {
        "slot": "strategy4", "asset": "SOL", "mode": "paper", "entry_ts": now - 120,
        "strike": 150.0, "side": "yes", "contracts": 5,
        "entry_price_cents": 50, "trade_id": 71}
    st["pending"]["SOLR-AGED"] = {
        "slot": "strategy4", "asset": "SOL", "mode": "paper", "entry_ts": now - 20 * 60,
        "strike": 150.0, "side": "yes", "contracts": 5,
        "entry_price_cents": 50, "trade_id": 72}
    bot_state._maker_track["SOLR-OLD"] = [(now - 3600, 50.0, 52.0)]
    bot_state._maker_track["SOLR-NEW"] = [(now - 30, 50.0, 52.0)]

    updates = {}

    async def _upd(tid, fields, **kw):
        updates[tid] = fields

    fake = _FakeSession({"SOLR-AGED": "yes"})
    with patch.object(bot_risk, "db_update_trade", _upd), \
         patch.object(bot_risk, "kalshi_headers", lambda *a, **k: {}):
        asyncio.run(bot_loops._periodic_slot_maintenance(fake, dict(_CFG), now))
    assert "SOLR-FRESH" in st["pending"], "fresh pending must NOT be swept mid-window"
    assert "SOLR-AGED" not in st["pending"], "aged pending must settle"
    assert updates.get(72, {}).get("outcome") == "win"
    assert 71 not in updates
    assert "SOLR-OLD" not in bot_state._maker_track
    assert "SOLR-NEW" in bot_state._maker_track


# ------------------------------------------------ S7 trades the move's direction

def test_s7_trades_in_the_direction_of_the_move():
    """A spiked tape whose last leg is a clear UP move must produce a YES trade.
    Kills the direction sign-flip mutation the regime-gate-only test let through."""
    now = time.time()
    dq = deque(maxlen=2000)
    base = 150.0
    for i in range(108):                      # spiked, direction-neutral body
        p = base * (1 + (0.006 if i % 2 else -0.006))
        dq.append((now - 600 + i * 5.0, p))
    for j in range(12):                       # decisive up-leg in the final 60s
        dq.append((now - 60 + j * 5.0, base * (1 + 0.004 + 0.0012 * j)))
    asset_manager._prices["SOL"] = dq
    spot = dq[-1][1]
    cfg = dict(_CFG, s7_min_edge=-1.0)        # isolate direction from the EV gate
    with patch("bot_strategy.read_config", return_value=cfg), \
         patch("bot_strategy._time_of_day_vol_multiplier", return_value=1.0):
        out = bs.strategy_brain_s7_volspike(spot, spot * 0.999, 55, 48, 480, 420,
                                            "T", asset="SOL")
    assert out["action"] == "trade", out["reasoning"]
    assert out["side"] == "yes", f"up-move must trade YES, got {out['side']}"


# ------------------------------------------------ S1 emits a real model_raw_p_yes

def test_s1_model_stage_emits_numeric_model_raw_p_yes():
    """The edge harness scores S1 through signals.model_raw_p_yes; a None (or
    missing) value silently blanks weeks of measurement while the key-presence
    gate still passes. Must be a genuine probability."""
    _flat_tape("SOL")
    # Strong recent up-move so the momentum gate passes and S1 reaches the model.
    now = time.time()
    dq = asset_manager._prices["SOL"]
    last = dq[-1][1]
    for j in range(24):
        dq.append((now + j * 2.5, last * (1 + 0.0009 * (j + 1))))
    spot = dq[-1][1]
    with patch("bot_strategy.read_config", return_value=dict(_CFG)), \
         patch("bot_strategy._time_of_day_vol_multiplier", return_value=1.0):
        out = bs.strategy_brain_s1(spot, spot * 0.9995, 50, 52, 480.0, 420.0,
                                   "KXSOL-M", asset="SOL")
    sig = out.get("signals") or {}
    assert "model_raw_p_yes" in sig, f"S1 never reached the model stage: {out['reasoning']}"
    p = sig["model_raw_p_yes"]
    assert isinstance(p, float) and 0.0 < p < 1.0, p
