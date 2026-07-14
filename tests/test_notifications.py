"""Notification clarity: display-tz timestamps, ET day bounds, per-trade messages."""
import sys, os, sqlite3, asyncio
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_infra
import bot_stats
import bot_state


def test_et_day_bounds_summer_and_winter():
    # July: EDT (UTC-4); January: EST (UTC-5). DATE(ts) would get both wrong.
    assert bot_infra.et_day_bounds_utc(date(2026, 7, 4)) == \
        ("2026-07-04T04:00:00", "2026-07-05T04:00:00")
    assert bot_infra.et_day_bounds_utc(date(2026, 1, 10)) == \
        ("2026-01-10T05:00:00", "2026-01-11T05:00:00")


def test_fmt_ts_uses_display_timezone_with_real_label():
    dt = datetime(2026, 7, 4, 19, 45, tzinfo=timezone.utc)
    cfg_et = {"display_timezone": "America/New_York"}
    cfg_pt = {"display_timezone": "America/Los_Angeles"}
    assert bot_infra.fmt_ts(dt, config=cfg_et) == "Jul 4, 3:45 PM EDT"
    assert bot_infra.fmt_ts(dt, config=cfg_pt) == "Jul 4, 12:45 PM PDT"
    # January flips the abbreviation automatically - no hard-coded offsets.
    jan = datetime(2026, 1, 10, 19, 45, tzinfo=timezone.utc)
    assert bot_infra.fmt_ts(jan, config=cfg_et).endswith("EST")


def test_fmt_ts_bad_timezone_falls_back_to_et():
    dt = datetime(2026, 7, 4, 19, 45, tzinfo=timezone.utc)
    assert bot_infra.fmt_ts(dt, config={"display_timezone": "Mars/Olympus"}).endswith("EDT")


def test_settle_message_content():
    msg = bot_stats.format_settle_message(
        "win", 4.2, "SOL", "s2", "yes", 73, 100, 7,
        "Jul 4, 3:45 PM EDT", 12.4, "paper")
    assert "<b>WIN +$4.20</b>" in msg
    assert "SOL * S2 * YES 73c -> 100c x7" in msg
    assert "Settled Jul 4, 3:45 PM EDT" in msg
    assert "Today: +$12.40" in msg
    loss = bot_stats.format_settle_message(
        "loss", -5.36, "ETH", "s1", "no", 66, 0, 3,
        "Jul 4, 3:45 PM EDT", -2.1, "paper")
    assert "<b>LOSS -$5.36</b>" in loss
    assert "ETH * S1 * NO 66c -> 0c x3" in loss


def test_entry_message_content():
    msg = bot_stats.format_entry_message(
        "SOL", "s2", "yes", 73, 7, 5.11, "Jul 4, 3:45 PM EDT", "paper")
    assert "<b>ENTRY</b>" in msg
    assert "SOL * S2 * YES @ 73c x7 ($5.11)" in msg
    assert "Window ends Jul 4, 3:45 PM EDT" in msg


def _seed_db(path):
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE trades (
        id INTEGER PRIMARY KEY, ts TEXT, mode TEXT DEFAULT 'paper',
        strategy_variant TEXT, asset TEXT, outcome TEXT, pnl_dollars REAL)""")
    rows = [
        # 7:30pm ET Jul 4 - same UTC date
        ("2026-07-04T23:30:00Z", "strategy2", "SOL", "win", 10.0),
        # 9pm ET Jul 4 - UTC date is ALREADY Jul 5; DATE(ts) misfiles this one
        ("2026-07-05T01:00:00Z", "strategy1", "ETH", "loss", -4.0),
        # 1am ET Jul 5 - next ET day, must be excluded
        ("2026-07-05T05:30:00Z", "strategy2", "SOL", "win", 99.0),
    ]
    con.executemany(
        "INSERT INTO trades (ts, strategy_variant, asset, outcome, pnl_dollars) VALUES (?,?,?,?,?)",
        rows)
    con.commit()
    con.close()


def test_query_stats_et_day_bounds_catch_evening_trades(tmp_path):
    db = str(tmp_path / "t.db")
    _seed_db(db)
    bounds = bot_infra.et_day_bounds_utc(date(2026, 7, 4))
    stats = bot_stats.query_stats(db, today_date="2026-07-04", day_bounds=bounds)
    assert stats["today_trades"] == 2                  # evening trade included
    assert stats["today_pnl"] == pytest.approx(6.0)    # 10 - 4; next-day 99 excluded
    # the legacy UTC bucket misses the 9pm ET trade - the exact bug being fixed
    legacy = bot_stats.query_stats(db, today_date="2026-07-04")
    assert legacy["today_trades"] == 1


def test_db_get_today_pnl_uses_et_day(tmp_path, monkeypatch):
    db = str(tmp_path / "p.db")
    _seed_db(db)
    monkeypatch.setattr(bot_state, "_DB_FILE", db)

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime(2026, 7, 5, 1, 30, tzinfo=timezone.utc)  # 9:30pm ET Jul 4
            return base.astimezone(tz) if tz else base.replace(tzinfo=None)

    monkeypatch.setattr(bot_infra, "datetime", _FrozenDT)
    got = asyncio.run(bot_infra.db_get_today_pnl("paper"))
    assert got == pytest.approx(6.0)                   # both Jul-4-ET trades, not the Jul-5 one


def test_summary_message_has_winner_and_absolute_last_trade():
    stats = {
        "date": "2026-07-04", "today_trades": 3, "today_wins": 2, "today_losses": 1,
        "today_pnl": 6.0, "alltime_trades": 40, "alltime_wins": 24, "alltime_pnl": 100.0,
        "by_strategy_asset": {
            ("strategy2", "SOL"): {"wins": 2, "losses": 0, "pnl": 10.0},
            ("strategy1", "ETH"): {"wins": 0, "losses": 1, "pnl": -4.0},
        },
        "last_trade_ts": "2026-07-05T01:00:00Z", "consecutive_losses": 0, "mode": "PAPER",
        "as_of": "Jul 5, 12:01 AM EDT",
        "display_tz": ZoneInfo("America/New_York"),
    }
    msg = bot_stats.format_telegram(stats)
    assert "(ET trading day)" in msg
    assert "Sent Jul 5, 12:01 AM EDT" in msg
    assert "S1: -$4.00 (0W/1L)" in msg
    assert "S2: +$10.00 (2W/0L)" in msg
    assert "Day winner: <b>S2</b>" in msg
    assert "Jul 4, 9:00 PM EDT" in msg                 # absolute last-trade time


def test_settle_notifications_wired_and_gated():
    import inspect
    import bot_loops, bot_risk
    lsrc = inspect.getsource(bot_loops)
    rsrc = inspect.getsource(bot_risk)
    for src in (lsrc, rsrc):
        assert 'config.get("notify_on_settle", True)' in src
        assert 'config.get("notify_on_entry", False)' in src
        assert "format_settle_message" in src
    # defaults exist so the toggles show up in config.json
    import inspect as _i
    isrc = _i.getsource(bot_infra._init_config)
    for key in ("notify_on_settle", "notify_on_entry", "display_timezone",
                "daily_summary_hour_et"):
        assert key in isrc


def test_midnight_reset_rolls_on_et_day(monkeypatch):
    """limit state must NOT reset at UTC midnight (8pm ET) - only at ET midnight."""
    import bot_risk

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):
            # 01:00 UTC Jul 5 == 9:00 PM ET Jul 4: UTC date has rolled, ET has not
            base = datetime(2026, 7, 5, 1, 0, tzinfo=timezone.utc)
            return base.astimezone(tz) if tz else base.replace(tzinfo=None)

    monkeypatch.setattr(bot_risk, "datetime", _FrozenDT)
    monkeypatch.setattr(bot_state, "daily_reset_date", date(2026, 7, 4))
    monkeypatch.setattr(bot_state, "limit_triggered", True)
    monkeypatch.setattr(bot_state, "limit_reason", "daily profit target reached")
    bot_risk.midnight_reset()
    assert bot_state.limit_triggered is True          # ET day hasn't rolled
    assert bot_state.daily_reset_date == date(2026, 7, 4)

    class _FrozenDT2(datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime(2026, 7, 5, 5, 0, tzinfo=timezone.utc)  # 1:00 AM ET Jul 5
            return base.astimezone(tz) if tz else base.replace(tzinfo=None)

    monkeypatch.setattr(bot_risk, "datetime", _FrozenDT2)
    monkeypatch.setattr(bot_risk, "read_config", lambda: {"mode": "paper"})
    monkeypatch.setattr(bot_risk, "write_config", lambda cfg: None)
    bot_risk.midnight_reset()
    assert bot_state.limit_triggered is False         # ET day rolled - reset fires
