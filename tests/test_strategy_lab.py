"""
The strategy lab: S3-S6 brains fire/skip correctly, the generic slot executor is
paper-only, settlement handles all three trade shapes, no aggregation drops the new
strategy ids, and the /api/edge leaderboard ranks + projects honestly.
"""
import sys
import os
import time
import sqlite3
import asyncio
from collections import deque
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import asset_manager
import bot_state
import bot_stats
import bot_strategy as bs
import bot_strategies
import bot_risk


_CFG = {"mode": "paper", "kalshi_fee_per_contract_cents": 7,
        "quiet_hours_enabled": False, "auto_gate_enabled": False,
        "calibration_enabled": False, "staleness_gate_enabled": False}


@pytest.fixture(autouse=True)
def _clean():
    saved = asset_manager._prices.get("SOL")
    bot_state._slot_state.clear()
    bot_state._prev_window_outcome.clear()
    bot_state._maker_track.clear()
    bot_state._last_window_strike.clear()
    yield
    if saved is not None:
        asset_manager._prices["SOL"] = saved
    bot_state._slot_state.clear()
    bot_state._prev_window_outcome.clear()
    bot_state._maker_track.clear()
    bot_state._last_window_strike.clear()


def _ramp(asset, start, end, n=90, span=150.0):
    now = time.time()
    dq = deque(maxlen=2000)
    for i in range(n):
        frac = i / (n - 1)
        dq.append((now - span * (1 - frac), start + (end - start) * frac))
    asset_manager._prices[asset] = dq


def _ramp_stall(asset, start, peak, n=90, span=150.0):
    now = time.time()
    dq = deque(maxlen=2000)
    for i in range(n):
        frac = i / (n - 1)
        if frac <= 0.66:
            p = start + (peak - start) * (frac / 0.66)
        else:
            p = peak - (peak - start) * 0.02 * ((frac - 0.66) / 0.34)
        dq.append((now - span * (1 - frac), p))
    asset_manager._prices[asset] = dq


def _cfg_patch(extra=None):
    cfg = dict(_CFG)
    if extra:
        cfg.update(extra)
    return patch("bot_strategy.read_config", return_value=cfg)


# ------------------------------------------------------------------ brains fire / skip

def test_s3_arb_fires_on_dislocated_book_and_skips_normal():
    _ramp("SOL", 150.0, 150.0)
    with _cfg_patch(), patch("bot_strategy._time_of_day_vol_multiplier", return_value=1.0):
        hit = bs.strategy_brain_s3_arb(150.0, 150.0, 45, 43, 300, 600, "T", asset="SOL")
        miss = bs.strategy_brain_s3_arb(150.0, 150.0, 52, 51, 300, 600, "T", asset="SOL")
    assert hit["action"] == "trade" and hit.get("arb_both_sides") is True
    assert hit["strategy_variant"] == "strategy3"
    assert miss["action"] == "skip" and "s3_no_arb" in miss["reasoning"]


def test_s4_fades_stalled_run_and_skips_running_move():
    with _cfg_patch(), patch("bot_strategy._time_of_day_vol_multiplier", return_value=1.0):
        _ramp_stall("SOL", 150.0, 150.95)     # 2-sigma run that stalled
        fade = bs.strategy_brain_s4_revert(150.93, 150.85, 55, 48, 480, 420, "T", asset="SOL")
        _ramp("SOL", 150.0, 150.65)           # same-size move still running
        running = bs.strategy_brain_s4_revert(150.65, 150.55, 56, 47, 480, 420, "T", asset="SOL")
    assert fade["action"] == "trade" and fade["side"] == "no", fade["reasoning"]
    assert fade["strategy_variant"] == "strategy4"
    assert running["action"] == "skip"
    assert "s4_still_running" in running["reasoning"] or "s4_not_extended" in running["reasoning"]


def test_s1_and_s4_are_opposite_bets_on_same_tape():
    """On a still-running move S1 buys continuation while S4 stands aside; the two
    setups are mutually exclusive by design."""
    _ramp("SOL", 150.0, 150.65)
    with _cfg_patch(), patch("bot_strategy._time_of_day_vol_multiplier", return_value=1.0):
        s1 = bs.strategy_brain_s1(150.65, 150.55, 56, 47, 480, 420, "T", asset="SOL")
        s4 = bs.strategy_brain_s4_revert(150.65, 150.55, 56, 47, 480, 420, "T", asset="SOL")
    assert s1["action"] == "trade" and s1["side"] == "yes"
    assert s4["action"] == "skip"


def test_s5_quotes_inside_ask_on_favorite():
    _ramp("SOL", 150.2, 150.45)
    with _cfg_patch(), patch("bot_strategy._time_of_day_vol_multiplier", return_value=1.0):
        r = bs.strategy_brain_s5_maker(150.45, 150.0, 78, 24, 480, 420, "T", asset="SOL")
        toss = bs.strategy_brain_s5_maker(150.45, 150.44, 52, 51, 480, 420, "T", asset="SOL")
    assert r["action"] == "trade" and r["maker_quote_cents"] == 77.0
    assert r["strategy_variant"] == "strategy5"
    assert toss["action"] == "skip" and "s5_band" in toss["reasoning"]


def test_s6_carries_prev_window_direction_early_only():
    bot_state._prev_window_outcome["SOL"] = {
        "result": "yes", "strike": 150.0, "spot_at_close": 150.35, "ts": time.time() - 60}
    _ramp("SOL", 150.30, 150.42)
    with _cfg_patch(), patch("bot_strategy._time_of_day_vol_multiplier", return_value=1.0):
        early = bs.strategy_brain_s6_carry(150.42, 150.40, 52, 51, 90, 810, "T", asset="SOL")
        late = bs.strategy_brain_s6_carry(150.42, 150.40, 52, 51, 400, 500, "T", asset="SOL")
        bot_state._prev_window_outcome.pop("SOL")
        no_prev = bs.strategy_brain_s6_carry(150.42, 150.40, 52, 51, 90, 810, "T", asset="SOL")
    assert early["action"] == "trade" and early["side"] == "yes"
    assert early["strategy_variant"] == "strategy6"
    assert late["action"] == "skip" and "s6_too_late" in late["reasoning"]
    assert no_prev["action"] == "skip" and "s6_no_prev_window" in no_prev["reasoning"]


def test_prev_window_estimate_written_at_rollover_and_defers_to_official():
    """S6 must get window memory even when S2 never traded: the rollover estimate fills
    it from the remembered strike, and never clobbers an official entry for the ticker."""
    import bot_loops
    bot_state._last_window_strike["SOL"] = ("SOL-W1", 150.0)
    # No official entry yet -> estimate written.
    bot_loops._record_prev_window_estimate("SOL", "SOL-W1", 150.4)
    got = bot_state._prev_window_outcome["SOL"]
    assert got["result"] == "yes" and got["estimated"] is True and got["ticker"] == "SOL-W1"
    # Official settlement for the SAME window already present -> estimate defers.
    bot_state._prev_window_outcome["SOL"] = {
        "result": "no", "strike": 150.0, "spot_at_close": 149.9, "ts": time.time(),
        "ticker": "SOL-W1", "estimated": False}
    bot_loops._record_prev_window_estimate("SOL", "SOL-W1", 150.4)
    assert bot_state._prev_window_outcome["SOL"]["result"] == "no"   # untouched
    # Strike remembered for a DIFFERENT ticker -> no bogus estimate.
    bot_loops._record_prev_window_estimate("SOL", "SOL-W2", 150.4)
    assert bot_state._prev_window_outcome["SOL"]["ticker"] == "SOL-W1"


def test_remember_window_strike_pairs_from_market_object():
    import bot_loops
    bot_loops._remember_window_strike("SOL", {"ticker": "SOL-W9", "floor_strike": 150.25})
    tick, strike = bot_state._last_window_strike["SOL"]
    assert tick == "SOL-W9" and strike == 150.25
    # Unparseable market leaves the memory unchanged.
    bot_loops._remember_window_strike("SOL", {"ticker": "SOL-W10", "yes_sub_title": "TBD"})
    assert bot_state._last_window_strike["SOL"][0] == "SOL-W9"


# ------------------------------------------------------------------ executor: paper-only

class _FakeOB(dict):
    pass


def _ob():
    return {"yes_liquidity": 500, "no_liquidity": 500,
            "best_yes_ask": 50, "best_no_ask": 52}


def test_executor_forces_paper_even_when_global_mode_live(monkeypatch):
    """The lab can NEVER touch live capital: rows are mode=paper and place_order is
    called with mode='paper' even when config mode=live."""
    rows, orders = [], []

    async def _fake_write(row):
        rows.append(row); return len(rows)

    async def _fake_place(session, ticker, side, contracts, price, mode, market, **kw):
        orders.append(mode)
        return {"fill_confirmed": True, "fill_price_cents": price,
                "order_id": "x", "filled_contracts": contracts}

    monkeypatch.setattr(bot_risk, "db_write_trade", _fake_write)
    monkeypatch.setattr(bot_risk, "place_order", _fake_place)
    brain = {"action": "trade", "side": "yes", "win_prob": 0.6, "confidence": 60,
             "raw_p_yes": 0.6, "signals": {}, "strategy_variant": "strategy4"}
    cfg = {"mode": "live", "trade_amount_dollars": 25}
    asyncio.run(bot_risk._execute_slot_trade(
        None, "strategy4", brain, "TK1", 150.0, 149.9, 50, 52, 400, "SOL", cfg, _ob()))
    assert orders == ["paper"]
    assert rows and all(r["mode"] == "paper" for r in rows)
    assert rows[0]["strategy_variant"] == "strategy4"
    assert "TK1" in bot_state._slot_state["strategy4"]["pending"]


def test_executor_arb_writes_two_legs(monkeypatch):
    rows = []

    async def _fake_write(row):
        rows.append(row); return len(rows)

    monkeypatch.setattr(bot_risk, "db_write_trade", _fake_write)
    brain = {"action": "trade", "side": "yes", "win_prob": 1.0, "confidence": 99,
             "raw_p_yes": 0.5, "signals": {}, "arb_both_sides": True,
             "strategy_variant": "strategy3"}
    cfg = {"mode": "paper", "trade_amount_dollars": 25}
    asyncio.run(bot_risk._execute_slot_trade(
        None, "strategy3", brain, "TK2", 150.0, 150.0, 45, 43, 400, "SOL", cfg, _ob()))
    assert len(rows) == 2
    assert {r["side"] for r in rows} == {"yes", "no"}
    legs = bot_state._slot_state["strategy3"]["pending"]["TK2"]["legs"]
    assert legs[0]["contracts"] == legs[1]["contracts"] > 0
    # pair outlay respects the $25 clip
    combined = (45 + 43) / 100.0
    assert legs[0]["contracts"] * combined <= 25.0 + 1e-9


def test_executor_maker_records_quote_without_order(monkeypatch):
    rows, orders = [], []

    async def _fake_write(row):
        rows.append(row); return len(rows)

    async def _fake_place(*a, **k):
        orders.append(1)
        return {"fill_confirmed": True}

    monkeypatch.setattr(bot_risk, "db_write_trade", _fake_write)
    monkeypatch.setattr(bot_risk, "place_order", _fake_place)
    brain = {"action": "trade", "side": "yes", "win_prob": 0.8, "confidence": 80,
             "raw_p_yes": 0.8, "signals": {}, "maker_quote_cents": 77.0,
             "strategy_variant": "strategy5"}
    cfg = {"mode": "paper", "trade_amount_dollars": 25}
    asyncio.run(bot_risk._execute_slot_trade(
        None, "strategy5", brain, "TK3", 150.4, 150.0, 78, 24, 400, "SOL", cfg, _ob()))
    assert orders == []                      # no order placed - it is a resting quote
    assert rows[0]["entry_price_cents"] == 77
    assert bot_state._slot_state["strategy5"]["pending"]["TK3"]["maker_quote_cents"] == 77.0


# ------------------------------------------------------------------ settlement shapes

def test_settle_arb_pair_nets_guaranteed_profit(monkeypatch):
    updates = {}

    async def _fake_update(tid, fields):
        updates[tid] = fields

    monkeypatch.setattr(bot_risk, "db_update_trade", _fake_update)
    st = bot_risk._slot("strategy3")
    st["pending"]["TKA"] = {
        "slot": "strategy3", "asset": "SOL", "mode": "paper", "entry_ts": time.time(),
        "market_close_time": "", "strike": 150.0, "arb": True,
        "legs": [
            {"trade_id": 1, "side": "yes", "entry_price_cents": 45, "contracts": 10},
            {"trade_id": 2, "side": "no", "entry_price_cents": 43, "contracts": 10},
        ],
    }
    asyncio.run(bot_risk._settle_slot_trades("TKA", "yes", 150.5, {"kalshi_fee_per_contract_cents": 7}))
    assert updates[1]["outcome"] == "win" and updates[2]["outcome"] == "loss"
    net = updates[1]["pnl_dollars"] + updates[2]["pnl_dollars"]
    assert net > 0                            # guaranteed profit either way
    assert "TKA" not in st["pending"]


def test_settle_maker_filled_and_unfilled(monkeypatch):
    updates = {}

    async def _fake_update(tid, fields):
        updates[tid] = fields

    monkeypatch.setattr(bot_risk, "db_update_trade", _fake_update)
    now = time.time()
    # Filled: the book crossed the 77c quote after entry.
    bot_state._maker_track["TKM"] = deque([(now - 30, 76.0, 26.0)], maxlen=120)
    st = bot_risk._slot("strategy5")
    st["pending"]["TKM"] = {
        "slot": "strategy5", "asset": "SOL", "mode": "paper", "entry_ts": now - 60,
        "market_close_time": "", "strike": 150.0, "maker_quote_cents": 77.0,
        "side": "yes", "contracts": 10, "entry_price_cents": 77, "trade_id": 5,
    }
    # Unfilled: book never crossed.
    bot_state._maker_track["TKU"] = deque([(now - 30, 79.0, 23.0)], maxlen=120)
    st["pending"]["TKU"] = {
        "slot": "strategy5", "asset": "SOL", "mode": "paper", "entry_ts": now - 60,
        "market_close_time": "", "strike": 150.0, "maker_quote_cents": 77.0,
        "side": "yes", "contracts": 10, "entry_price_cents": 77, "trade_id": 6,
    }
    asyncio.run(bot_risk._settle_slot_trades("TKM", "yes", 150.5, {}))
    asyncio.run(bot_risk._settle_slot_trades("TKU", "yes", 150.5, {}))
    assert updates[5]["outcome"] == "win" and updates[5]["pnl_dollars"] > 0
    assert updates[6]["outcome"] == "unfilled" and updates[6]["pnl_dollars"] == 0.0


def test_settle_normal_slot_and_spot_fallback(monkeypatch):
    updates = {}

    async def _fake_update(tid, fields):
        updates[tid] = fields

    monkeypatch.setattr(bot_risk, "db_update_trade", _fake_update)
    st = bot_risk._slot("strategy4")
    st["pending"]["TKN"] = {
        "slot": "strategy4", "asset": "SOL", "mode": "paper", "entry_ts": time.time(),
        "market_close_time": "", "strike": 150.0, "side": "no", "contracts": 10,
        "entry_price_cents": 48, "trade_id": 9,
    }
    # No official result -> spot 149.5 < strike 150 -> result no -> NO side wins.
    asyncio.run(bot_risk._settle_slot_trades("TKN", None, 149.5, {}))
    assert updates[9]["outcome"] == "win"
    assert updates[9]["pnl_dollars"] > 0


# ------------------------------------------------------------------ aggregation survival

def test_bot_stats_accepts_all_lab_variants(tmp_path):
    db = str(tmp_path / "t.db")
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE trades (id INTEGER PRIMARY KEY, ts TEXT,
        mode TEXT DEFAULT 'paper', strategy_variant TEXT, asset TEXT,
        outcome TEXT, pnl_dollars REAL)""")
    for i, sv in enumerate(bot_strategies.STRATEGY_LABELS):
        con.execute("INSERT INTO trades (ts, strategy_variant, asset, outcome, pnl_dollars) "
                    "VALUES (?,?,?,?,?)",
                    (f"2026-07-04T1{i}:00:00Z", sv, "SOL", "win", 1.0))
    con.commit(); con.close()
    bounds = ("2026-07-04T00:00:00", "2026-07-05T00:00:00")
    stats = bot_stats.query_stats(db, today_date="2026-07-04", day_bounds=bounds)
    # every lab variant survives the label filter - nothing dropped
    assert stats["today_trades"] == len(bot_strategies.STRATEGY_LABELS)
    variants = {sv for sv, _ in stats["by_strategy_asset"]}
    assert variants == set(bot_strategies.STRATEGY_LABELS)


def test_label_dicts_stay_in_sync():
    assert set(bot_stats._STRATEGY_LABELS) == set(bot_strategies.STRATEGY_LABELS)
    import server
    assert set(server._ALL_STRATEGIES) == set(bot_strategies.STRATEGY_LABELS)


def test_registry_covers_s3_to_s6():
    assert set(bot_strategies.STRATEGY_REGISTRY) == {
        "strategy3", "strategy4", "strategy5", "strategy6"}
    for slot, meta in bot_strategies.STRATEGY_REGISTRY.items():
        assert callable(meta["brain"])
        assert meta["enabled_key"]
    assert bot_strategies.enabled_slots({}) == list(bot_strategies.STRATEGY_REGISTRY)
    assert bot_strategies.enabled_slots({"s4_revert_enabled": False}) == [
        "strategy3", "strategy5", "strategy6"]


# ------------------------------------------------------------------ leaderboard API

def _seed_trade(conn, ts, sv, version, outcome, pnl, contracts, entry_cents):
    conn.execute(
        "INSERT INTO trades (ts, market_id, mode, side, contracts, entry_price_cents, "
        "outcome, pnl_dollars, strategy_variant, strategy_version, fill_confirmed) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,1)",
        (ts, "TK", "paper", "yes", contracts, entry_cents, outcome, pnl, sv, version))


def test_api_edge_leaderboard_ranks_and_projects(tmp_path, monkeypatch):
    """Leaderboard reads EXECUTED paper trades (current versions only): voided maker
    quotes excluded, retired-version rows excluded, span-day rate, honest projections."""
    db = str(tmp_path / "t.db")
    monkeypatch.setattr(bot_state, "_DB_FILE", db)
    monkeypatch.setenv("BOT_DB_FILE", db)
    import bot_infra
    bot_infra.init_db()
    import server
    client = server.app.test_client()
    conn = sqlite3.connect(db)
    v4 = bot_state._SLOT_VERSIONS["strategy4"]
    v5 = bot_state._SLOT_VERSIONS["strategy5"]
    v1 = bot_state._S1_VERSION
    # strategy4: 30 trades over a 3-day span, 80% wins at 50c, +$5 winners/-$5 losers
    for i in range(30):
        won = bool(i % 5)
        _seed_trade(conn, f"2026-07-0{1 + i % 3}T14:00:00Z", "strategy4", v4,
                    "win" if won else "loss", 5.0 if won else -5.0, 10, 50)
    # strategy1: 10 losing trades, all on day 1 of the same 3-day window
    for i in range(10):
        _seed_trade(conn, "2026-07-01T15:00:00Z", "strategy1", v1,
                    "loss", -6.0, 10, 60)
    # strategy5: 2 wins + 3 VOIDED quotes (unfilled) - voids must not count as trades
    for i in range(2):
        _seed_trade(conn, "2026-07-01T16:00:00Z", "strategy5", v5, "win", 2.0, 10, 77)
    for i in range(3):
        _seed_trade(conn, "2026-07-01T17:00:00Z", "strategy5", v5, "unfilled", 0.0, 10, 77)
    # retired-version rows must be ignored entirely
    for i in range(50):
        _seed_trade(conn, "2026-06-20T14:00:00Z", "strategy1", "old-version",
                    "win", 99.0, 10, 50)
    conn.commit(); conn.close()
    d = client.get("/api/edge").get_json()
    board = d["decisions"]["leaderboard"]
    assert [r["strategy"] for r in board] == ["strategy4", "strategy5", "strategy1"]
    top = board[0]
    # net/ct = sum(pnl)/sum(contracts) = (24*5 - 6*5)/300 = 0.30
    assert abs(top["net_per_contract"] - 0.30) < 1e-6
    assert "collecting (30/200)" in top["verdict"]
    # projections scale linearly and match hand math at the span-day rate (30/3=10/day)
    p = top["projected_weekly_at_clip"]
    assert p and abs(p["250"] / p["25"] - 10.0) < 0.01
    expect_25 = 0.30 * (25 / 0.50) * 10 * 7
    assert abs(p["25"] - expect_25) < 0.5
    # S5: only the 2 filled trades count; win rate 100% of the filled
    s5 = board[1]
    assert s5["n"] == 2 and s5["win_rate"] == 1.0
    # losing strategy: no fantasy projections; the old-version windfall is invisible
    s1 = board[2]
    assert s1["projected_weekly_at_clip"] == {}
    assert s1["n"] == 10 and s1["net_per_contract"] < 0


def test_api_pnl_by_strategy_includes_lab_slots(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    monkeypatch.setattr(bot_state, "_DB_FILE", db)
    monkeypatch.setenv("BOT_DB_FILE", db)
    import bot_infra
    bot_infra.init_db()
    import server
    client = server.app.test_client()
    d = client.get("/api/pnl").get_json()
    assert set(d["by_strategy"]) == set(bot_strategies.STRATEGY_LABELS)


# ------------------------------------------------------------------ review-fix regressions

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.status = 200

    async def json(self):
        return self._payload

    async def text(self):
        return ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    def __init__(self, payload):
        self._payload = payload

    def get(self, *a, **k):
        return _FakeResp(self._payload)


def _ob_payload(yes_cents, no_cents):
    return {"orderbook": {"yes": [[yes_cents, 200]], "no": [[no_cents, 200]]}}


def test_fetch_orderbook_passes_arb_book_rejects_broken(monkeypatch):
    """An arb-qualifying book (combined < $1.00) must reach the brains - the old
    blanket <100 rejection made S3 permanently unreachable. Truly broken books
    (combined < 70c) stay rejected."""
    import bot_market
    monkeypatch.setattr(bot_state, "api_key", "k")
    monkeypatch.setattr(bot_market, "kalshi_headers", lambda *a, **k: {})
    arb = asyncio.run(bot_market.fetch_orderbook(_FakeSession(_ob_payload(45, 43)), "TK"))
    assert arb is not None and arb["best_yes_ask"] == 45 and arb["best_no_ask"] == 43
    broken = asyncio.run(bot_market.fetch_orderbook(_FakeSession(_ob_payload(30, 25)), "TK"))
    assert broken is None
    normal = asyncio.run(bot_market.fetch_orderbook(_FakeSession(_ob_payload(56, 47)), "TK"))
    assert normal is not None


def test_slot_settle_runs_before_maker_track_pop():
    """S5's fill evidence lives in _maker_track; _record_maker_counterfactual pops it.
    The slot settle call must therefore come BEFORE the counterfactual in the expiry
    block, or every S5 quote on an S2-traded ticker voids as unfilled."""
    import inspect
    import bot_loops
    src = inspect.getsource(bot_loops.handle_locked_phase)
    i_settle = src.index("_settle_slot_trades")
    i_cf = src.index("_record_maker_counterfactual")
    assert i_settle < i_cf, "slot settlement must run before the maker-track pop"
    assert src.count("_settle_slot_trades") == 1, "single settle call (no late duplicate)"


def test_periodic_tick_sweeps_aged_pendings_and_prunes_maker_track():
    """Ladder-picked tickers never become prev_ticker, so aged in-memory pendings must
    be swept by the periodic tick (else they deadlock against the orphan-sweep guard);
    stale _maker_track entries must be pruned."""
    import inspect
    import bot_loops
    src = inspect.getsource(bot_loops.main_loop)
    assert "_settle_slot_rollover" in src and "18 * 60" in src, "aged pending sweep missing"
    assert "_maker_track.pop" in src and "1800" in src, "maker_track prune missing"


# ------------------------------------------------------------------ lab activity telemetry

def test_bump_slot_activity_counts_and_buckets():
    """Every eval is counted; skip reasons bucket on the prefix before ':'; the skip
    dict is bounded so a runaway reason set cannot grow without limit."""
    import bot_loops
    bot_state._slot_activity.clear()
    bot_loops._bump_slot_activity("strategy6", {"action": "skip", "reasoning": "s6_no_prev_window"})
    bot_loops._bump_slot_activity("strategy6", {"action": "skip", "reasoning": "s6_too_late:400s"})
    bot_loops._bump_slot_activity("strategy6", {"action": "skip", "reasoning": "s6_too_late:501s"})
    bot_loops._bump_slot_activity("strategy6", {"action": "trade", "reasoning": "s6_carry ..."})
    act = bot_state._slot_activity["strategy6"]
    assert act["evals"] == 4 and act["trades"] == 1
    assert act["skips"] == {"s6_no_prev_window": 1, "s6_too_late": 2}
    # bounded: 30 distinct reasons -> only 24 keys retained, evals still counted
    for i in range(30):
        bot_loops._bump_slot_activity("strategy3", {"action": "skip", "reasoning": f"r{i}:x"})
    a3 = bot_state._slot_activity["strategy3"]
    assert a3["evals"] == 30 and len(a3["skips"]) == 24
    bot_state._slot_activity.clear()


def test_api_edge_exposes_lab_activity(tmp_path, monkeypatch):
    """/api/edge reads the bot-process dump fail-soft: present -> per-slot counters with
    top-3 skips; absent -> empty dict, never a 500."""
    import json as _json
    db = str(tmp_path / "t.db")
    monkeypatch.setattr(bot_state, "_DB_FILE", db)
    monkeypatch.setattr(bot_state, "_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BOT_DB_FILE", db)
    import bot_infra
    bot_infra.init_db()
    import server
    client = server.app.test_client()
    # absent file -> empty, 200
    d = client.get("/api/edge").get_json()
    assert d["lab_activity"] == {}
    # seeded file -> counters + top-3 skips sorted by count
    (tmp_path / "lab_activity.json").write_text(_json.dumps({
        "strategy6": {"evals": 412, "trades": 3, "since": 1e9,
                      "skips": {"a": 5, "b": 210, "c": 120, "d": 64, "e": 2}},
    }))
    d = client.get("/api/edge").get_json()
    a = d["lab_activity"]["strategy6"]
    assert a["evals"] == 412 and a["trades"] == 3
    assert a["top_skips"] == [["b", 210], ["c", 120], ["d", 64]]
