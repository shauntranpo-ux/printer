"""Tests for the ET session / day-type taxonomy in sessions.py."""
import sys, os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import sessions as s

ET = ZoneInfo("America/New_York")


def _et(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


def test_session_boundaries():
    assert s.session_for_dt(_et(2026, 7, 1, 9, 29)) == "overnight"
    assert s.session_for_dt(_et(2026, 7, 1, 9, 30)) == "us_open"
    assert s.session_for_dt(_et(2026, 7, 1, 11, 29)) == "us_open"
    assert s.session_for_dt(_et(2026, 7, 1, 11, 30)) == "us_midday"
    assert s.session_for_dt(_et(2026, 7, 1, 14, 59)) == "us_midday"
    assert s.session_for_dt(_et(2026, 7, 1, 15, 0)) == "us_close"
    assert s.session_for_dt(_et(2026, 7, 1, 16, 29)) == "us_close"
    assert s.session_for_dt(_et(2026, 7, 1, 16, 30)) == "us_evening"
    assert s.session_for_dt(_et(2026, 7, 1, 21, 59)) == "us_evening"
    assert s.session_for_dt(_et(2026, 7, 1, 22, 0)) == "overnight"
    assert s.session_for_dt(_et(2026, 7, 1, 2, 0)) == "overnight"


def test_session_order_covers_all_labels():
    labels = {s.session_for_dt(_et(2026, 7, 1, h, 0)) for h in range(24)}
    labels.add(s.session_for_dt(_et(2026, 7, 1, 9, 45)))
    assert labels <= set(s.ET_SESSION_ORDER)
    assert set(s.ET_SESSION_ORDER) == {"us_open", "us_midday", "us_close", "us_evening", "overnight"}


def test_iso_parsing_and_dst():
    # 14:00 UTC in July = 10:00 EDT (DST) → us_open
    assert s.session_for_iso("2026-07-01T14:00:00+00:00") == "us_open"
    # 14:00 UTC in January = 09:00 EST → overnight (before 09:30)
    assert s.session_for_iso("2026-01-01T14:00:00+00:00") == "overnight"
    # Z suffix accepted
    assert s.session_for_iso("2026-07-01T14:00:00Z") == "us_open"


def test_weekend_detection():
    assert s.is_weekend_et(_et(2026, 7, 4, 12, 0)) is True     # Saturday
    assert s.is_weekend_et(_et(2026, 7, 5, 12, 0)) is True     # Sunday
    assert s.is_weekend_et(_et(2026, 7, 1, 12, 0)) is False    # Wednesday
    # Sat 02:00 UTC = Fri 22:00 ET — must read ET, so still Friday (weekday)
    assert s.is_weekend_et("2026-07-04T02:00:00+00:00") is False
    assert s.day_type_for_iso("2026-07-01T14:00:00+00:00") == "weekday"
    assert s.day_type_for_iso("2026-07-04T16:00:00+00:00") == "weekend"


def test_et_anchoring_across_utc_midnight():
    # Sun 03:00 UTC = Sat 23:00 ET → still weekend
    assert s.is_weekend_et("2026-07-05T03:00:00+00:00") is True
    # Mon 03:00 UTC = Sun 23:00 ET → still weekend (Sunday in ET)
    assert s.is_weekend_et("2026-07-06T03:00:00+00:00") is True
    # Mon 14:00 UTC = Mon 10:00 ET → weekday
    assert s.is_weekend_et("2026-07-06T14:00:00+00:00") is False


def test_fail_open_on_bad_input():
    assert s.session_for_iso("garbage") is None
    assert s.day_type_for_iso("not-a-date") is None
    assert s.is_weekend_et("nope") is False
    # naive datetime assumed UTC, never raises
    assert s.session_for_dt(datetime(2026, 7, 1, 14, 0)) in s.ET_SESSION_ORDER


def test_now_session_returns_valid_label():
    assert s.now_session() in s.ET_SESSION_ORDER
