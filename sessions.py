"""sessions.py - ET time-of-day session + weekday/weekend taxonomy.

Pure, dependency-free helpers shared by the strategy brains (session gate), the
dashboard (`server.api_edge`), and the offline reports (`scripts/edge_report.py`).

All boundaries are anchored to US/Eastern (DST-aware via ZoneInfo), matching the
rest of the live bot's wall-clock logic (`_is_quiet_hours`, `bot_loops._ET_TZ`).
Every function is fail-open: on any bad input it returns a neutral value rather
than raising, so it can never break the trading loop.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

# Session labels in display order. Boundaries below are ET minutes-of-day.
US_OPEN    = "us_open"      # 09:30-11:30 ET - highest realized vol
US_MIDDAY  = "us_midday"    # 11:30-15:00 ET
US_CLOSE   = "us_close"     # 15:00-16:30 ET - elevated vol
US_EVENING = "us_evening"   # 16:30-22:00 ET
OVERNIGHT  = "overnight"    # 22:00-09:30 ET - thin, wide spreads

ET_SESSION_ORDER = [US_OPEN, US_MIDDAY, US_CLOSE, US_EVENING, OVERNIGHT]

# ET minute-of-day boundaries.
_OPEN_START  = 9 * 60 + 30    # 570
_OPEN_END    = 11 * 60 + 30   # 690
_MIDDAY_END  = 15 * 60        # 900
_CLOSE_END   = 16 * 60 + 30   # 990
_EVENING_END = 22 * 60        # 1320


def _to_et(dt: datetime) -> datetime:
    """Convert a datetime to ET. Naive datetimes are assumed to be UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_ET)


def session_for_dt(dt: datetime) -> str:
    """Return the ET session label for a datetime. Falls back to OVERNIGHT on error."""
    try:
        et = _to_et(dt)
        t = et.hour * 60 + et.minute
        if _OPEN_START <= t < _OPEN_END:
            return US_OPEN
        if _OPEN_END <= t < _MIDDAY_END:
            return US_MIDDAY
        if _MIDDAY_END <= t < _CLOSE_END:
            return US_CLOSE
        if _CLOSE_END <= t < _EVENING_END:
            return US_EVENING
        return OVERNIGHT  # t >= 22:00 or t < 09:30
    except Exception:
        return OVERNIGHT


def session_for_iso(ts_iso: str):
    """Return the ET session label for an ISO-8601 timestamp string, or None on parse error."""
    try:
        dt = datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00"))
    except Exception:
        return None
    return session_for_dt(dt)


def is_weekend_et(value) -> bool:
    """True iff the moment falls on Sat/Sun in ET. Accepts a datetime or ISO string.
    Fail-open: returns False on any parse error (do not block on bad data)."""
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return _to_et(dt).weekday() >= 5   # Mon=0 .. Sun=6
    except Exception:
        return False


def day_type_for_iso(ts_iso: str):
    """'weekend' | 'weekday' for an ISO timestamp, or None on parse error."""
    try:
        dt = datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00"))
    except Exception:
        return None
    return "weekend" if is_weekend_et(dt) else "weekday"


def now_session(tz=_ET) -> str:
    """Current ET session label (uses wall clock)."""
    return session_for_dt(datetime.now(tz))
