"""Edge-measurement harness: decision_log write/backfill, $25 clamp, edge_report math."""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
import bot_infra


def _use_tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_state, "_DB_FILE", str(tmp_path / "t.db"))
    bot_infra.init_db()


async def test_decision_log_write_and_backfill(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    await bot_infra.db_write_decision({
        "ts": "2026-06-29T00:00:00+00:00", "ticker": "KXETH-X", "asset": "ETH",
        "strategy": "strategy1", "mode": "paper", "side": "yes",
        "model_p_yes": 0.61, "market_mid_p_yes": 0.55, "market_edge": 0.06,
        "entry_price_cents": 56.0, "secs_left": 300.0, "would_trade": True,
    })
    await bot_infra.db_write_decision({
        "ts": "2026-06-29T00:00:01+00:00", "ticker": "KXETH-X", "asset": "ETH",
        "strategy": "strategy2", "mode": "paper", "side": "no",
        "model_p_yes": 0.40, "market_mid_p_yes": 0.45, "market_edge": 0.05,
        "entry_price_cents": 53.0, "secs_left": 290.0, "would_trade": False,
    })
    conn = sqlite3.connect(bot_state._DB_FILE)
    rows = conn.execute("SELECT outcome, would_trade FROM decision_log ORDER BY id").fetchall()
    assert len(rows) == 2
    assert all(r[0] == "pending" for r in rows)
    assert rows[0][1] == 1 and rows[1][1] == 0
    conn.close()

    await bot_infra.db_backfill_decision_outcome("KXETH-X", "yes")
    conn = sqlite3.connect(bot_state._DB_FILE)
    outcomes = [r[0] for r in conn.execute("SELECT outcome FROM decision_log").fetchall()]
    conn.close()
    assert outcomes == ["yes", "yes"], "backfill must stamp all pending rows for the ticker"


async def test_pending_tickers_respects_age_cutoff(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    await bot_infra.db_write_decision({
        "ts": "2026-06-01T00:00:00+00:00", "ticker": "OLD", "asset": "ETH",
        "strategy": "strategy1", "mode": "paper", "side": "yes",
        "model_p_yes": 0.6, "market_mid_p_yes": 0.5, "market_edge": 0.1,
        "entry_price_cents": 50.0, "secs_left": 100.0, "would_trade": True,
    })
    await bot_infra.db_write_decision({
        "ts": "2026-12-31T00:00:00+00:00", "ticker": "NEW", "asset": "ETH",
        "strategy": "strategy1", "mode": "paper", "side": "yes",
        "model_p_yes": 0.6, "market_mid_p_yes": 0.5, "market_edge": 0.1,
        "entry_price_cents": 50.0, "secs_left": 100.0, "would_trade": True,
    })
    pend = await bot_infra.db_pending_decision_tickers("2026-07-01T00:00:00+00:00", limit=10)
    assert "OLD" in pend and "NEW" not in pend, "only windows past the cutoff are eligible"


def test_trade_amount_clamped_to_25(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_state, "_DB_FILE", str(tmp_path / "t.db"))
    monkeypatch.setattr(bot_state, "_CONFIG_FILE", str(tmp_path / "config.json"))
    with open(bot_state._CONFIG_FILE, "w") as fh:
        json.dump({"trade_amount_dollars": 100}, fh)
    bot_infra._init_config()
    cfg = bot_infra.read_config()
    assert cfg["trade_amount_dollars"] == 25.0, "per-trade clip must be hard-capped at $25"


async def test_measurement_flag_disables_decision_logging(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    import bot_loops
    bot_loops._logged_decisions.clear()
    brain = {"action": "skip", "side": "yes", "above": True,
             "signals": {"model_raw_p_yes": 0.6, "mkt_p": 0.5, "market_edge": 0.1}}
    # measurement_enabled=False -> nothing logged
    await bot_loops._log_decision(brain, "KX-OFF", "ETH", 300, 45, 55,
                                  {"measurement_enabled": False}, "strategy1")
    conn = sqlite3.connect(bot_state._DB_FILE)
    assert conn.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0] == 0
    conn.close()
    # measurement_enabled=True -> one row
    await bot_loops._log_decision(brain, "KX-ON", "ETH", 300, 45, 55,
                                  {"measurement_enabled": True}, "strategy1")
    conn = sqlite3.connect(bot_state._DB_FILE)
    assert conn.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0] == 1
    conn.close()


def test_measurement_enabled_defaults_on():
    import inspect
    src = inspect.getsource(bot_infra)
    assert '"measurement_enabled": True' in src, "measurement must default on"


def test_edge_report_math():
    from scripts.edge_report import wilson_lower, _win, _p_side, _auc, _kalshi_fee
    assert _win("yes", "yes") == 1 and _win("yes", "no") == 0
    assert _win("no", "no") == 1 and _win("no", "yes") == 0
    assert abs(_p_side(0.6, "yes") - 0.6) < 1e-9
    assert abs(_p_side(0.6, "no") - 0.4) < 1e-9
    assert 0.0 < wilson_lower(60, 100) < 0.60  # LB below the point estimate
    assert wilson_lower(0, 0) == 0.0
    # perfect-ranking AUC = 1.0
    assert abs(_auc([(0.9, 1), (0.8, 1), (0.2, 0), (0.1, 0)]) - 1.0) < 1e-9
    # random/constant score -> 0.5
    assert abs(_auc([(0.5, 1), (0.5, 0)]) - 0.5) < 1e-9
    assert abs(_kalshi_fee(0.30) - 0.0147) < 1e-9
