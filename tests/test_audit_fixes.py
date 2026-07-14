"""
Regression tests for the pre-merge audit round: timestamp-format cutoffs, settle
guards, conditional updates, the mode clamp, per-position restore, reprice
re-sizing, the reconcile fills error path, and the /api/risk meter population.
"""
import sys
import os
import time
import json
import sqlite3
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from collections import deque

import asset_manager
import bot_state
import bot_infra
import bot_risk
import bot_loops
import bot_market
import reconcile


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def _clean():
    saved_sol = asset_manager._prices.get("SOL")
    bot_state._slot_state.clear()
    bot_state._s1_pending_trades.clear()
    yield
    if saved_sol is not None:
        asset_manager._prices["SOL"] = saved_sol
    bot_state._slot_state.clear()
    bot_state._s1_pending_trades.clear()


# ------------------------------------------------------- startup reconcile cutoff

def test_startup_reconcile_selects_same_day_pending_rows(tmp_path, monkeypatch):
    """'T' > ' ' broke the SQL-side cutoff: a same-UTC-day pending row was never
    selected. The Python-built cutoff must select old rows and spare fresh ones."""
    import bot
    db = str(tmp_path / "r.db")
    monkeypatch.setattr(bot_state, "_DB_FILE", db)
    bot_infra.init_db()
    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(db)
    for ts, outcome in ((_iso(now - timedelta(hours=2)), "pending"),      # must match
                        (_iso(now - timedelta(minutes=5)), "pending"),    # too fresh
                        (_iso(now - timedelta(hours=3)), "win")):         # settled
        conn.execute(
            "INSERT INTO trades (ts, market_id, side, contracts, entry_price_cents,"
            " mode, asset, outcome) VALUES (?,?,?,?,?,?,?,?)",
            (ts, "TK-" + outcome + ts[-3:], "yes", 5, 50, "live", "SOL", outcome))
    conn.commit(); conn.close()

    classified = []

    async def _classify(session, row, mode):
        classified.append(row["ts"])
        return {"action": "leave_pending", "reason": "test"}

    async def _tg(msg):
        pass

    monkeypatch.setattr(bot, "classify_pending_trade", _classify)
    monkeypatch.setattr(bot, "send_telegram", _tg)
    asyncio.run(bot._startup_reconcile(None, "live"))
    assert len(classified) == 1
    assert classified[0] == _iso(now - timedelta(hours=2))


# ------------------------------------------------------- settle guards (no blind 0.0)

def test_slot_settle_defers_without_result_or_usable_spot(monkeypatch):
    updates = []

    async def _upd(tid, fields, **kw):
        updates.append(tid)

    monkeypatch.setattr(bot_risk, "db_update_trade", _upd)
    st = bot_risk._slot("strategy4")
    pos = {"slot": "strategy4", "asset": "SOL", "mode": "paper", "entry_ts": time.time(),
           "strike": 150.0, "side": "yes", "contracts": 10,
           "entry_price_cents": 50, "trade_id": 7}
    st["pending"]["TKD"] = pos
    cfg = {"kalshi_fee_per_contract_cents": 7}
    # No official result AND spot 0.0 (empty price deque case) -> deferred, retained.
    asyncio.run(bot_risk._settle_slot_trades("TKD", None, 0.0, cfg))
    assert st["pending"]["TKD"] is pos and not updates
    # Positive spot but a FROZEN feed (last print 10 min old) -> still deferred.
    asset_manager._prices["SOL"] = deque([(time.time() - 600, 149.0)])
    asyncio.run(bot_risk._settle_slot_trades("TKD", None, 149.0, cfg))
    assert st["pending"]["TKD"] is pos and not updates
    # Fresh feed + usable spot settles it (spot below strike -> YES loses).
    asset_manager._prices["SOL"] = deque([(time.time(), 149.0)])
    asyncio.run(bot_risk._settle_slot_trades("TKD", None, 149.0, cfg))
    assert "TKD" not in st["pending"] and updates == [7]


def test_s1_settle_defers_without_result_or_usable_spot(tmp_path, monkeypatch):
    updates = []

    async def _upd(tid, fields, **kw):
        updates.append(fields)

    monkeypatch.setattr(bot_risk, "db_update_trade", _upd)
    # The win path also updates wr_calibration - point it at a scratch DB so test
    # runs never write fabricated wins into a real kalshi_bot.db in the cwd.
    db = str(tmp_path / "wr.db")
    monkeypatch.setattr(bot_state, "_DB_FILE", db)
    bot_infra.init_db()
    cfg = {"kalshi_fee_per_contract_cents": 7, "notify_on_settle": False}
    pos = {"trade_id": 3, "side": "yes", "entry_price_cents": 50, "contracts": 10,
           "strike": 150.0, "asset": "SOL", "mode": "paper"}
    bot_state._s1_pending_trades["TKS"] = pos
    asyncio.run(bot_risk._settle_s1_trade("TKS", None, 0.0, cfg, "SOL"))
    assert bot_state._s1_pending_trades["TKS"] is pos and not updates
    # Positive spot from a frozen feed is not trustworthy either.
    asset_manager._prices["SOL"] = deque([(time.time() - 600, 151.0)])
    asyncio.run(bot_risk._settle_s1_trade("TKS", None, 151.0, cfg, "SOL"))
    assert bot_state._s1_pending_trades["TKS"] is pos and not updates
    asset_manager._prices["SOL"] = deque([(time.time(), 151.0)])
    asyncio.run(bot_risk._settle_s1_trade("TKS", None, 151.0, cfg, "SOL"))
    assert "TKS" not in bot_state._s1_pending_trades
    assert updates and updates[0]["outcome"] == "win"


# ------------------------------------------------------- conditional update guard

def test_db_update_trade_only_if_pending_never_overwrites_settled(tmp_path, monkeypatch):
    db = str(tmp_path / "u.db")
    monkeypatch.setattr(bot_state, "_DB_FILE", db)
    bot_infra.init_db()
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO trades (id, ts, market_id, outcome, pnl_dollars) "
                 "VALUES (1, '2026-07-13T10:00:00Z', 'A', 'win', 5.0)")
    conn.execute("INSERT INTO trades (id, ts, market_id, outcome, pnl_dollars) "
                 "VALUES (2, '2026-07-13T10:00:00Z', 'B', 'pending', NULL)")
    conn.commit(); conn.close()
    void = {"outcome": "unfilled", "pnl_dollars": 0.0}
    asyncio.run(bot_infra.db_update_trade(1, dict(void), only_if_pending=True))
    asyncio.run(bot_infra.db_update_trade(2, dict(void), only_if_pending=True))
    conn = sqlite3.connect(db)
    rows = dict(conn.execute("SELECT id, outcome FROM trades").fetchall())
    conn.close()
    assert rows[1] == "win"        # settled row untouched
    assert rows[2] == "unfilled"   # pending row updated


# ------------------------------------------------------- mode clamp

def test_strategy_mode_ignores_live_under_paper_global_without_persisting():
    bot_infra._mode_clamp_warned.clear()
    cfg = {"mode": "paper", "s1_mode": "live", "s2_mode": "demo"}
    assert bot_infra.strategy_mode(cfg, "s1") == "paper"   # live honored only under live global
    assert bot_infra.strategy_mode(cfg, "s2") == "demo"    # demo is not riskier - kept
    # Resolution must NOT mutate the dict: the bot's read-modify-write cycles
    # (midnight_reset, daily-limit trip) persist it back to config.json, and a
    # mutating clamp permanently erased the operator's stored live setting.
    assert cfg["s1_mode"] == "live"
    # Under a live global, per-strategy live passes through; absent key follows global.
    assert bot_infra.strategy_mode({"mode": "live", "s1_mode": "live"}, "s1") == "live"
    assert bot_infra.strategy_mode({"mode": "paper"}, "s1") == "paper"


# ------------------------------------------------------- restore respects pos mode

def test_restore_keeps_paper_position_absent_from_live_portfolio(monkeypatch):
    async def _open_pos(session, mode):
        return {}   # live portfolio shows nothing

    async def _tg(msg):
        pass

    monkeypatch.setattr(bot_loops, "fetch_open_positions", _open_pos)
    monkeypatch.setattr(bot_loops, "send_telegram", _tg)
    saved_btc = {"trade_id": 9, "ticker": "BTC-X", "contracts": 5, "mode": "paper"}
    non_btc = {"SOL": {"phase": "LOCKED", "position": {
        "trade_id": 10, "ticker": "SOL-X", "contracts": 3, "mode": "paper"}}}
    bot_state.current_position = None
    bot_state.current_phase = ""
    bot_state._asset_states.pop("SOL", None)
    asyncio.run(bot_loops._verify_and_restore_positions(
        None, saved_btc, "LOCKED", non_btc, "live"))
    # Paper positions restored, NOT voided against the (empty) live portfolio.
    assert bot_state.current_position is saved_btc
    assert bot_state.current_phase == "LOCKED"
    assert bot_state._asset_states["SOL"]["position"]["trade_id"] == 10
    bot_state.current_position = None
    bot_state.current_phase = "DONE"
    bot_state._asset_states.pop("SOL", None)


# ------------------------------------------------------- reprice re-size

def test_place_order_resizes_contracts_when_ask_moved_up(monkeypatch):
    async def _ob(session, ticker, market=None):
        return {"best_yes_ask": 80, "best_no_ask": 22,
                "yes_liquidity": 500, "no_liquidity": 500, "obi": 0.0}

    monkeypatch.setattr(bot_market, "fetch_orderbook", _ob)
    # Sized for 40c: 62 contracts = $24.80. At the fresh 80c ask the same count
    # would spend $49.60 - the re-size must halve it to stay inside the budget.
    res = asyncio.run(bot_market.place_order(None, "TK", "yes", 62, 40, "paper"))
    assert res["fill_confirmed"] is True
    assert res["fill_price_cents"] == 80
    assert res["filled_contracts"] == 31
    assert res["filled_contracts"] * 80 <= 62 * 40 + 1e-9


# ------------------------------------------------------- fills error != no fills

def test_classify_leaves_pending_when_fills_fetch_fails(monkeypatch):
    async def _res(session, ticker):
        return "resolved_yes"

    async def _fills(session, ticker, since):
        return None   # fetch FAILED (network) - not "no fills"

    monkeypatch.setattr(reconcile, "fetch_market_resolution", _res)
    monkeypatch.setattr(reconcile, "fetch_fills_for_ticker", _fills)
    row = {"id": 1, "market_id": "TK", "side": "yes", "contracts": 5,
           "entry_price_cents": 50, "ts": "2026-07-13T08:00:00Z",
           "order_id": "o1", "mode": "live", "asset": "SOL"}
    out = asyncio.run(reconcile.classify_pending_trade(None, row, "live"))
    assert out["action"] == "leave_pending"
    assert "fills fetch failed" in out["reason"]


# ------------------------------------------------------- /api/risk meter population

def test_api_risk_meters_use_enforcement_population(tmp_path, monkeypatch):
    """The Daily Loss meter must sum ONLY the configured mode's S1/S2 trades on the
    ET day - not lab-slot paper P&L, not other modes."""
    db = str(tmp_path / "risk.db")
    monkeypatch.setattr(bot_state, "_DB_FILE", db)
    monkeypatch.setenv("BOT_DB_FILE", db)
    bot_infra.init_db()
    import server
    monkeypatch.setattr(server, "read_config", lambda: {
        "mode": "paper", "daily_loss_limit_dollars": 0,
        "daily_profit_target_dollars": 200})
    now = _iso(datetime.now(timezone.utc))
    conn = sqlite3.connect(db)
    rows = [
        (now, "A", "paper", "strategy2", "loss", -10.0),   # counts
        (now, "B", "paper", "strategy1", "win", 4.0),      # counts
        (now, "C", "paper", "strategy6", "loss", -50.0),   # lab slot - excluded
        (now, "D", "live", "strategy2", "loss", -99.0),    # other mode - excluded
    ]
    for ts, tk, mode, sv, outcome, pnl in rows:
        conn.execute(
            "INSERT INTO trades (ts, market_id, mode, strategy_variant, outcome,"
            " pnl_dollars) VALUES (?,?,?,?,?,?)", (ts, tk, mode, sv, outcome, pnl))
    conn.commit(); conn.close()
    client = server.app.test_client()
    data = client.get("/api/risk").get_json()
    # Losses meter: only the -$10 S2 paper loss; the lab's -$50 and live's -$99
    # must not appear. Profit meter clamps the -$6 net to 0.
    assert data["daily_loss_limit"]["current"] == 10.0
    assert data["daily_profit_target"]["current"] == 0.0
