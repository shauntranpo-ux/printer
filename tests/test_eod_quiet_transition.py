"""End-of-day summary: one message covering the previous full ET day, restart-safe."""
import sys, os, asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import bot_loops


_ET = ZoneInfo("America/New_York")
_BASE_STATS = {
    "date": "2026-07-04", "today_trades": 3, "today_wins": 2, "today_losses": 1,
    "today_pnl": 12.4, "alltime_trades": 40, "alltime_wins": 24, "alltime_pnl": 100.0,
    "by_strategy_asset": {("strategy2", "SOL"): {"wins": 2, "losses": 1, "pnl": 12.4}},
    "last_trade_ts": "2026-07-04T23:30:00Z", "consecutive_losses": 0, "mode": "PAPER",
}


def _run(cfg, now_et, sent=None, written=None):
    sent = sent if sent is not None else []
    written = written if written is not None else []

    async def _capture(msg):
        sent.append(msg)

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return now_et if tz else now_et.replace(tzinfo=None)

    with patch.object(bot_loops, "read_config", return_value=cfg), \
         patch.object(bot_loops, "write_config", side_effect=written.append), \
         patch.object(bot_loops, "send_telegram", AsyncMock(side_effect=_capture)), \
         patch.object(bot_loops.bot_stats, "query_stats",
                      return_value=dict(_BASE_STATS)), \
         patch.object(bot_loops, "datetime", _FrozenDT):
        asyncio.run(bot_loops._maybe_send_daily_summary())
    return sent, written


def _fresh():
    bot_loops._last_summary_sent_for = ""


def test_summary_covers_previous_et_day():
    _fresh()
    sent, written = _run({"mode": "paper"}, datetime(2026, 7, 5, 0, 1, tzinfo=_ET))
    assert len(sent) == 1
    assert "2026-07-04" in sent[0]           # yesterday's ET day, not today's
    assert written and written[0]["_last_daily_summary_for"] == "2026-07-04"


def test_summary_dedups_within_process_and_across_restart():
    _fresh()
    cfg = {"mode": "paper"}
    sent, _ = _run(cfg, datetime(2026, 7, 5, 0, 1, tzinfo=_ET))
    assert len(sent) == 1
    # same process, later the same day: no resend
    sent2, _ = _run(cfg, datetime(2026, 7, 5, 9, 0, tzinfo=_ET), sent=sent)
    assert len(sent2) == 1
    # simulated restart with the persisted marker: no resend
    _fresh()
    sent3, _ = _run({"mode": "paper", "_last_daily_summary_for": "2026-07-04"},
                    datetime(2026, 7, 5, 9, 0, tzinfo=_ET))
    assert sent3 == []


def test_summary_respects_configured_hour():
    _fresh()
    cfg = {"mode": "paper", "daily_summary_hour_et": 8}
    sent, _ = _run(cfg, datetime(2026, 7, 5, 6, 0, tzinfo=_ET))
    assert sent == []                        # too early
    sent, _ = _run(cfg, datetime(2026, 7, 5, 8, 5, tzinfo=_ET))
    assert len(sent) == 1


def test_summary_queries_et_day_bounds():
    _fresh()

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 5, 0, 1, tzinfo=_ET)

    with patch.object(bot_loops.bot_stats, "query_stats",
                      return_value=dict(_BASE_STATS)) as qs, \
         patch.object(bot_loops, "read_config", return_value={"mode": "paper"}), \
         patch.object(bot_loops, "write_config"), \
         patch.object(bot_loops, "send_telegram", AsyncMock()), \
         patch.object(bot_loops, "datetime", _FrozenDT):
        asyncio.run(bot_loops._maybe_send_daily_summary())
    bounds = qs.call_args.kwargs["day_bounds"]
    # ET day Jul 4 2026 (EDT, UTC-4): 04:00Z Jul 4 -> 04:00Z Jul 5
    assert bounds == ("2026-07-04T04:00:00", "2026-07-05T04:00:00")


def test_old_report_machinery_removed():
    import inspect
    src = inspect.getsource(bot_loops)
    assert "_check_daily_stats" not in src
    assert "_send_brain_scorecard" not in src
    assert "_DAILY_REPORT_HOUR_ET" not in src
