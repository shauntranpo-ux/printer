"""
Tests for the manual session filter gate (_session_allowed + both brains) and the
per-session / per-day-type edge breakdown in edge_report and /api/edge.
"""
import sys, os, sqlite3, tempfile
from collections import deque
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_state
import asset_manager
import bot_strategy as bs
import sessions


# --------------------------------------------------------------------------- _session_allowed

def test_session_allowed_off_by_default():
    ok, label = bs._session_allowed({})
    assert ok is True
    assert label in sessions.ET_SESSION_ORDER


def test_session_allowed_blocks_current_session():
    cur = sessions.now_session()
    ok, label = bs._session_allowed({"blocked_sessions": [cur]})
    assert ok is False
    assert label == cur


def test_session_allowed_allows_other_session():
    cur = sessions.now_session()
    other = "us_open" if cur != "us_open" else "overnight"
    ok, _ = bs._session_allowed({"blocked_sessions": [other]})
    assert ok is True


def test_session_allowed_block_weekends(monkeypatch):
    import datetime as _dt
    from zoneinfo import ZoneInfo

    class _Sat(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _dt.datetime(2026, 7, 4, 12, 0, tzinfo=ZoneInfo("America/New_York"))
    monkeypatch.setattr(bs.datetime, "datetime", _Sat)
    ok, label = bs._session_allowed({"block_weekends": True})
    assert ok is False
    assert label == "weekend"


def test_session_allowed_fail_open(monkeypatch):
    # A helper that raises must not propagate - fail open (allow).
    monkeypatch.setattr(bs.sessions, "session_for_dt", lambda dt: (_ for _ in ()).throw(ValueError()))
    ok, _ = bs._session_allowed({"blocked_sessions": ["us_open"]})
    assert ok is True


# --------------------------------------------------------------------------- brain gates

def test_s1_session_gate_fires_when_blocked():
    cur = sessions.now_session()
    cfg = {"mode": "paper", "quiet_hours_enabled": False, "blocked_sessions": [cur]}
    with patch("bot_strategy.read_config", return_value=cfg), \
         patch.object(bot_state, "_s1_pending_trades", {}), \
         patch.object(bot_state, "_s1_asset_trade_times", {}), \
         patch.object(bot_state, "_s1_cooldown_until", {}):
        r = bs.strategy_brain_s1(150.0, 149.0, 45.0, 55.0, 300.0, 600.0, "KXSOL-SESS", asset="SOL")
    assert r["action"] == "skip"
    assert r["reasoning"] == f"s1_session_gate:{cur}"


def test_s2_session_gate_fires_when_blocked():
    cur = sessions.now_session()
    cfg = {"mode": "paper", "quiet_hours_enabled": False, "blocked_sessions": [cur]}
    saved = asset_manager._prices.get("SOL")
    try:
        asset_manager._prices["SOL"] = deque([(0, 150.5)], maxlen=10)
        with patch("bot_strategy.read_config", return_value=cfg):
            r = bs.strategy_brain_s2(150.5, 150.0, 45.0, 55.0, 300.0, 600.0, "KXSOL-SESS2", asset="SOL")
    finally:
        if saved is not None:
            asset_manager._prices["SOL"] = saved
    assert r["action"] == "skip"
    assert r["reasoning"] == f"s2_session_gate:{cur}"


def test_session_gate_default_off_does_not_block():
    """With no blocked_sessions, the session gate must not skip (behavior unchanged)."""
    cfg = {"mode": "paper", "quiet_hours_enabled": False}
    with patch("bot_strategy.read_config", return_value=cfg), \
         patch.object(bot_state, "_s1_pending_trades", {}), \
         patch.object(bot_state, "_s1_asset_trade_times", {}), \
         patch.object(bot_state, "_s1_cooldown_until", {}):
        r = bs.strategy_brain_s1(150.0, 149.0, 45.0, 55.0, 300.0, 600.0, "KXSOL-NOSESS", asset="SOL")
    assert "session_gate" not in r["reasoning"]


# --------------------------------------------------------------------------- edge_report bucketing

def _pick(ts, outcome, entry=45.0, side="yes"):
    return {"ts": ts, "side": side, "outcome": outcome, "entry_price_cents": entry}


def test_bucket_picks_splits_by_session_and_pnl_sign():
    import scripts.edge_report as er
    picks = [_pick("2026-07-01T14:00:00+00:00", "yes", 45.0) for _ in range(4)] \
          + [_pick("2026-07-01T04:00:00+00:00", "no", 55.0) for _ in range(4)]
    stats = er.bucket_picks(picks, er.sessions.session_for_iso)
    assert stats["us_open"]["win_rate"] == 1.0
    assert stats["us_open"]["net_pnl"] > 0
    assert stats["overnight"]["win_rate"] == 0.0
    assert stats["overnight"]["net_pnl"] < 0


# --------------------------------------------------------------------------- /api/edge shape

def _seed_db(rows):
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    os.environ["BOT_DB_FILE"] = f.name
    bot_state._DB_FILE = f.name
    import bot_infra
    bot_infra.init_db()
    if rows:
        conn = sqlite3.connect(f.name)
        conn.executemany(
            "INSERT INTO decision_log (ts,ticker,asset,strategy,mode,side,model_p_yes,"
            "market_mid_p_yes,market_edge,entry_price_cents,secs_left,would_trade,outcome) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        conn.close()
    return f.name


def test_api_edge_by_session_shape():
    rows = []
    for i in range(55):
        rows.append(("2026-07-01T14:%02d:00+00:00" % (i % 60), "KX%d" % i, "SOL", "strategy2",
                     "paper", "yes", 0.62, 0.55, 0.07, 45.0, 300.0, 1, "yes"))
    for i in range(55):
        rows.append(("2026-07-04T04:%02d:00+00:00" % (i % 60), "KXW%d" % i, "SOL", "strategy2",
                     "paper", "yes", 0.60, 0.52, 0.08, 55.0, 300.0, 1, "no"))
    path = _seed_db(rows)
    try:
        import importlib, server
        importlib.reload(server)
        d = server.app.test_client().get("/api/edge").get_json()
        by_s = d["decisions"]["by_session"]
        by_d = d["decisions"]["by_daytype"]
        assert by_s["us_open"]["net_pnl_per_contract"] > 0
        assert by_s["overnight"]["net_pnl_per_contract"] < 0
        assert by_s["us_open"]["insufficient"] is False   # n=55 >= MIN_BUCKET_N (50)
        assert set(by_d.keys()) == {"weekday", "weekend"}
    finally:
        os.unlink(path)


def test_api_edge_empty_db_has_session_keys():
    path = _seed_db([])
    try:
        import importlib, server
        importlib.reload(server)
        resp = server.app.test_client().get("/api/edge")
        assert resp.status_code == 200
        dec = resp.get_json()["decisions"]
        assert dec["by_session"] == {}
        assert dec["by_daytype"] == {}
    finally:
        os.unlink(path)
