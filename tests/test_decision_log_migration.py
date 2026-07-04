"""decision_log sigma-observability migration + the _log_decision second-slot dedup."""
import sys, os, asyncio, sqlite3, tempfile
from unittest.mock import patch, AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state


LEGACY_SCHEMA = """
CREATE TABLE decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, ticker TEXT, asset TEXT, strategy TEXT, mode TEXT, side TEXT,
    model_p_yes REAL, market_mid_p_yes REAL, market_edge REAL,
    entry_price_cents REAL, secs_left REAL,
    would_trade INTEGER DEFAULT 0, outcome TEXT DEFAULT 'pending'
)
"""


@pytest.fixture()
def legacy_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "bot.db")
    con = sqlite3.connect(db_path)
    con.execute(LEGACY_SCHEMA)
    con.execute(
        "INSERT INTO decision_log (ts, ticker, asset, strategy, mode, side, model_p_yes) "
        "VALUES ('2026-07-01T00:00:00Z','KX-OLD','SOL','strategy2','paper','yes',0.6)"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(bot_state, "_DB_FILE", db_path)
    return db_path


def test_init_db_adds_sigma_columns_and_keeps_old_rows(legacy_db):
    import bot_infra
    bot_infra.init_db()
    con = sqlite3.connect(legacy_db)
    cols = {row[1] for row in con.execute("PRAGMA table_info(decision_log)")}
    assert {"spot", "strike", "sigma_eff", "z"} <= cols
    row = con.execute(
        "SELECT model_p_yes, spot, strike, sigma_eff, z FROM decision_log WHERE ticker='KX-OLD'"
    ).fetchone()
    con.close()
    assert row[0] == pytest.approx(0.6)
    assert row[1] is None and row[2] is None and row[3] is None and row[4] is None


def test_db_write_decision_round_trips_new_fields(legacy_db):
    import bot_infra
    bot_infra.init_db()
    asyncio.run(bot_infra.db_write_decision({
        "ts": "2026-07-04T00:00:00Z", "ticker": "KX-NEW", "asset": "SOL",
        "strategy": "strategy2", "mode": "paper", "side": "yes",
        "model_p_yes": 0.8, "market_mid_p_yes": 0.72, "market_edge": 0.08,
        "entry_price_cents": 73.0, "secs_left": 480.0, "would_trade": True,
        "spot": 150.44, "strike": 150.0, "sigma_eff": 0.0046, "z": 0.9,
    }))
    con = sqlite3.connect(legacy_db)
    row = con.execute(
        "SELECT spot, strike, sigma_eff, z, would_trade FROM decision_log WHERE ticker='KX-NEW'"
    ).fetchone()
    con.close()
    assert row == (150.44, 150.0, 0.0046, 0.9, 1)


def _brain(action, reasoning="x", z_raw=None):
    sig = {"model_raw_p_yes": 0.8, "mkt_p": 0.7, "market_edge": 0.08,
           "spot": 150.4, "strike": 150.0, "sigma_eff": 0.0046, "z": 0.9}
    if z_raw is not None:
        sig["z_raw"] = z_raw
    return {"action": action, "side": "yes", "reasoning": reasoning, "signals": sig}


def test_log_decision_second_slot_records_the_eventual_trade():
    import bot_loops
    bot_loops._logged_decisions.clear()
    bot_loops._logged_trade_decisions.clear()
    calls = []

    async def _capture(payload):
        calls.append(payload)

    cfg = {"measurement_enabled": True, "mode": "paper"}
    with patch.object(bot_loops, "db_write_decision", AsyncMock(side_effect=_capture)):
        # A full-signal skip logs first (this is the common case with the new gates)...
        asyncio.run(bot_loops._log_decision(_brain("skip"), "KX-A", "SOL", 480, 73, 29, cfg, "strategy2"))
        # ...the eventual trade on the same ticker must STILL get one row...
        asyncio.run(bot_loops._log_decision(_brain("trade"), "KX-A", "SOL", 470, 73, 29, cfg, "strategy2"))
        # ...and repeats of either kind are deduped.
        asyncio.run(bot_loops._log_decision(_brain("trade"), "KX-A", "SOL", 460, 73, 29, cfg, "strategy2"))
        asyncio.run(bot_loops._log_decision(_brain("skip"), "KX-A", "SOL", 450, 73, 29, cfg, "strategy2"))
    assert len(calls) == 2
    assert calls[0]["would_trade"] is False
    assert calls[1]["would_trade"] is True
    assert calls[1]["z"] == pytest.approx(0.9)
    bot_loops._logged_decisions.clear()
    bot_loops._logged_trade_decisions.clear()


def test_log_decision_prefers_descaled_z():
    # The sigma_scale refit needs a target independent of the applied scale, so the
    # z column must carry z_raw when the brain provides it (fallback: z).
    import bot_loops
    bot_loops._logged_decisions.clear()
    bot_loops._logged_trade_decisions.clear()
    calls = []

    async def _capture(payload):
        calls.append(payload)

    cfg = {"measurement_enabled": True, "mode": "paper"}
    with patch.object(bot_loops, "db_write_decision", AsyncMock(side_effect=_capture)):
        asyncio.run(bot_loops._log_decision(_brain("skip", z_raw=1.35), "KX-Z", "SOL",
                                            480, 73, 29, cfg, "strategy2"))
        asyncio.run(bot_loops._log_decision(_brain("skip"), "KX-Z2", "SOL",
                                            480, 73, 29, cfg, "strategy2"))
    assert calls[0]["z"] == pytest.approx(1.35)
    assert calls[1]["z"] == pytest.approx(0.9)   # fallback when z_raw absent
    bot_loops._logged_decisions.clear()
    bot_loops._logged_trade_decisions.clear()


def test_settled_zs_filters_by_strategy(legacy_db):
    import bot_infra
    bot_infra.init_db()
    for strat, z in (("strategy2", 0.8), ("strategy1", 1.6), ("s_fav", 0.9)):
        asyncio.run(bot_infra.db_write_decision({
            "ts": "2026-07-04T00:00:00Z", "ticker": f"KX-{strat}", "asset": "SOL",
            "strategy": strat, "mode": "paper", "side": "yes",
            "model_p_yes": 0.7, "market_mid_p_yes": 0.7, "market_edge": 0.0,
            "entry_price_cents": 70.0, "secs_left": 480.0, "would_trade": False,
            "spot": 150.4, "strike": 150.0, "sigma_eff": 0.0046, "z": z,
        }))
    con = sqlite3.connect(legacy_db)
    con.execute("UPDATE decision_log SET outcome='yes' WHERE ticker LIKE 'KX-s%'")
    con.commit()
    con.close()
    rows = asyncio.run(bot_infra.db_settled_decision_zs())
    assert [(a, z) for a, z, _ in rows] == [("SOL", 0.8)]   # strategy2 only


def test_log_decision_trade_first_gets_single_row():
    import bot_loops
    bot_loops._logged_decisions.clear()
    bot_loops._logged_trade_decisions.clear()
    calls = []

    async def _capture(payload):
        calls.append(payload)

    cfg = {"measurement_enabled": True, "mode": "paper"}
    with patch.object(bot_loops, "db_write_decision", AsyncMock(side_effect=_capture)):
        asyncio.run(bot_loops._log_decision(_brain("trade"), "KX-B", "SOL", 480, 73, 29, cfg, "strategy2"))
        asyncio.run(bot_loops._log_decision(_brain("trade"), "KX-B", "SOL", 470, 73, 29, cfg, "strategy2"))
        asyncio.run(bot_loops._log_decision(_brain("skip"), "KX-B", "SOL", 460, 73, 29, cfg, "strategy2"))
    assert len(calls) == 1
    assert calls[0]["would_trade"] is True
    bot_loops._logged_decisions.clear()
    bot_loops._logged_trade_decisions.clear()
