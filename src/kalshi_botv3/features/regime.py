"""Session / time-of-day regime features.

All pure — time is an explicit argument, no clocks.
UTC session boundaries:
  asia  00:00-07:00
  eu    07:00-12:00
  us    12:00-21:00
  off   21:00-24:00
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal


def session_bucket(now: datetime) -> Literal["asia", "eu", "us", "off"]:
    hour = now.astimezone(UTC).hour
    if hour < 7:
        return "asia"
    if hour < 12:
        return "eu"
    if hour < 21:
        return "us"
    return "off"


def is_weekend(now: datetime) -> bool:
    """True if UTC day is Saturday (5) or Sunday (6)."""
    return now.astimezone(UTC).weekday() >= 5


def minutes_to_top_of_hour(now: datetime) -> float:
    """Fractional minutes until the next full hour (range: (0, 60])."""
    dt = now.astimezone(UTC)
    seconds_past = dt.minute * 60 + dt.second + dt.microsecond / 1_000_000
    return (3_600.0 - seconds_past) / 60.0
