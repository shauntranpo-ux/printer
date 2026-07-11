"""
Multi-window integration replay for the strategy lab.

Unit tests cover each piece in isolation; this drives the WHOLE lab lifecycle -
registry dispatch, activity counters, decision log, paper executor, held-book
tracking, official + rollover settlement, window memory and streak tracking -
across 36 consecutive simulated 15-min windows on a deterministic tape, against
a real (temp) sqlite DB and a fake Kalshi session. The bugs this hunts live in
the seams between windows: pending leaks, settle ordering, streak drift,
non-paper rows, rows stuck pending.
"""
import sys
import os
import time
import math
import random
import sqlite3
import asyncio
from collections import deque
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import asset_manager
import bot_state
import bot_infra
import bot_risk
import bot_loops
import bot_strategies


_CFG = {"mode": "paper", "kalshi_fee_per_contract_cents": 7,
        "quiet_hours_enabled": False, "auto_gate_enabled": False,
        "calibration_enabled": False, "staleness_gate_enabled": False,
        "measurement_enabled": True, "trade_amount_dollars": 25}

WINDOW_SECS = 900
TICK_SECS_LEFT = (810, 660, 480, 300, 180)   # five evaluation ticks per window
N_WINDOWS = 36


def _phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


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
    """Answers the rollover settler's official-result fetch per ticker."""
    def __init__(self):
        self.results = {}

    def get(self, url, **kw):
        return _FakeResp({"market": {"result": self.results.get(url.rsplit("/", 1)[-1])}})


@pytest.fixture(autouse=True)
def _isolate():
    saved = asset_manager._prices.get("SOL")
    dicts = (bot_state._slot_state, bot_state._prev_window_outcome,
             bot_state._maker_track, bot_state._last_window_strike,
             bot_state._slot_activity, bot_state._implied_sigma)
    for d in dicts:
        d.clear()
    bot_loops._logged_decisions.clear()
    bot_loops._logged_trade_decisions.clear()
    yield
    if saved is not None:
        asset_manager._prices["SOL"] = saved
    for d in dicts:
        d.clear()
    bot_loops._logged_decisions.clear()
    bot_loops._logged_trade_decisions.clear()


def test_lab_survives_36_window_replay(tmp_path, monkeypatch):
    db = str(tmp_path / "replay.db")
    monkeypatch.setattr(bot_state, "_DB_FILE", db)
    bot_infra.init_db()

    async def _paper_fill(session, ticker, side, contracts, price, mode,
                          market=None, **kw):
        assert mode == "paper"   # nothing dispatched by the lab may reach live
        return {"fill_confirmed": True, "fill_price_cents": price,
                "filled_contracts": contracts, "order_id": None}

    monkeypatch.setattr(bot_risk, "place_order", _paper_fill)
    monkeypatch.setattr(bot_risk, "kalshi_headers", lambda *a, **k: {})

    fake = _FakeSession()
    rng = random.Random(20260711)
    cfg = dict(_CFG)
    tape = deque(maxlen=100_000)
    asset_manager._prices["SOL"] = tape
    t0 = time.time() - N_WINDOWS * WINDOW_SECS
    price = 150.0
    sigma_5s = 0.0050 / math.sqrt(180.0)     # ~SOL-scale 15-min vol per 5s print
    results = []
    streak_expect = 0

    async def _run():
        nonlocal price, streak_expect
        for w in range(N_WINDOWS):
            vol_mult = (0.4, 1.0, 2.5)[w % 3]    # calm / normal / spiked tapes
            open_ts = t0 + w * WINDOW_SECS
            close_ts = open_ts + WINDOW_SECS
            ticker = f"SOLR-{w:03d}"
            strike = price
            market = {"ticker": ticker, "strike_price": strike, "close_time": ""}
            tick_idx = 0
            for step in range(180):              # one print every 5s
                price *= math.exp(rng.gauss(0.0, sigma_5s * vol_mult))
                ts = open_ts + (step + 1) * 5.0
                tape.append((ts, price))
                secs_left = close_ts - ts
                if tick_idx >= len(TICK_SECS_LEFT) or secs_left > TICK_SECS_LEFT[tick_idx]:
                    continue
                tick_idx += 1
                bot_loops._remember_window_strike("SOL", market)
                z = (price - strike) / (strike * 0.005 * math.sqrt(max(secs_left, 1.0) / 900.0))
                mid = max(1, min(99, round(100 * _phi(z))))
                yes_ask = min(99, mid + 2)
                no_ask = min(99, (100 - mid) + 2)
                if w % 7 == 2 and tick_idx == 3:
                    yes_ask = max(1, yes_ask - 8)   # occasional dislocated book
                    no_ask = max(1, no_ask - 8)     # (combined < 93c) so S3 fires
                ob = {"yes_liquidity": 500, "no_liquidity": 500,
                      "best_yes_ask": yes_ask, "best_no_ask": no_ask, "obi": 0.0}
                # Held-book path uses wall-clock like the live loop; the executor
                # stamps entry_ts from time.time(), so the fill scan needs the same clock.
                bot_state._maker_track.setdefault(ticker, []).append(
                    (time.time(), yes_ask, no_ask))
                elapsed = WINDOW_SECS - secs_left
                for slot_id in bot_strategies.enabled_slots(cfg):
                    brain = bot_strategies.STRATEGY_REGISTRY[slot_id]["brain"](
                        price, strike, yes_ask, no_ask, elapsed, secs_left,
                        ticker, asset="SOL")
                    bot_loops._bump_slot_activity(slot_id, brain)
                    await bot_loops._log_decision(brain, ticker, "SOL", secs_left,
                                                  yes_ask, no_ask, cfg, slot_id)
                    await bot_risk._execute_slot_trade(
                        fake, slot_id, brain, ticker, price, strike,
                        yes_ask, no_ask, secs_left, "SOL", cfg, ob, market)
            result = "yes" if price > strike else "no"
            fake.results[ticker] = result
            streak_expect = streak_expect + 1 if results and results[-1] == result else 1
            results.append(result)
            if w % 4 == 3:
                # Official path first (what handle_locked_phase runs when S2 traded
                # the same ticker); the rollover below must then no-op cleanly.
                await bot_risk._settle_slot_trades(ticker, result, price, cfg)
            await bot_risk._settle_slot_rollover(fake, ticker, price, cfg)
            bot_loops._record_prev_window_estimate("SOL", ticker, price)

    with patch("bot_strategy.read_config", return_value=cfg), \
         patch("bot_strategy._time_of_day_vol_multiplier", return_value=1.0):
        asyncio.run(_run())

    # 1. No pending leaks in memory - every position settled at its window boundary.
    for slot, st in bot_state._slot_state.items():
        assert not st["pending"], f"{slot} leaked pendings: {list(st['pending'])}"

    # 2. DB: every row settled, every row paper, every row on a current version.
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT strategy_variant, strategy_version, mode, outcome, pnl_dollars,"
        " market_id FROM trades").fetchall()
    assert rows, "36 windows across 6 strategies should produce trades"
    assert all(r[2] == "paper" for r in rows)
    stuck = [r for r in rows if r[3] == "pending"]
    assert not stuck, f"rows stuck pending: {stuck}"
    for sv, ver, _mode, _outcome, _pnl, _tk in rows:
        assert ver == bot_state._SLOT_VERSIONS[sv], (sv, ver)

    # 3. P&L accounting is coherent per outcome, and arb pairs net a profit
    #    regardless of which side settled (that is the whole S3 thesis).
    arb_net = {}
    for sv, _ver, _mode, outcome, pnl, tk in rows:
        if outcome == "win":
            assert pnl > 0
        elif outcome == "loss":
            assert pnl < 0
        else:
            assert outcome == "unfilled" and pnl == 0
        if sv == "strategy3":
            arb_net[tk] = arb_net.get(tk, 0.0) + pnl
    for tk, net in arb_net.items():
        assert net > 0, f"arb pair on {tk} settled at a loss: {net}"

    # 4. Window memory: result and streak match the hand-computed sequence.
    prev = bot_state._prev_window_outcome["SOL"]
    assert prev["result"] == results[-1]
    assert prev["streak"] == streak_expect
    assert bot_state._last_window_strike["SOL"][0] == f"SOLR-{N_WINDOWS - 1:03d}"

    # 5. Every enabled slot evaluated every tick of every window - dispatch never
    #    silently dropped a strategy.
    for slot_id in bot_strategies.enabled_slots(cfg):
        assert bot_state._slot_activity[slot_id]["evals"] == N_WINDOWS * len(TICK_SECS_LEFT)

    # 6. The replay is not vacuous: several distinct strategies actually traded,
    #    and the decision log captured model-stage evaluations.
    variants = {r[0] for r in rows}
    assert len(variants) >= 3, f"only {variants} traded - tape too tame"
    ndec = conn.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0]
    assert ndec > 0
    conn.close()
