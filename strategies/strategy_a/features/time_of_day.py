from __future__ import annotations
"""
Time-of-day regime encoder for Kalshi 15-minute crypto markets.

Sessions (UTC):
  asia_deep_night  00:00–04:00   Low liquidity, wide spreads
  asia_active      04:00–08:00   Tokyo / Singapore active
  eu_open          08:00–13:00   London open, highest volume
  eu_us_overlap    13:00–16:00   Peak global liquidity
  us_afternoon     16:00–20:00   US afternoon, Fed-news window
  us_late          20:00–24:00   Thin US tail, Asia pre-open
"""
import numpy as np
import pandas as pd

_SESSIONS: list[tuple[str, int, int]] = [
    ("asia_deep_night",  0,  4),
    ("asia_active",      4,  8),
    ("eu_open",          8, 13),
    ("eu_us_overlap",   13, 16),
    ("us_afternoon",    16, 20),
    ("us_late",         20, 24),
]
_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _minutes_until(ts: pd.Timestamp, h: int, m: int) -> float:
    candidate = ts.floor("D") + pd.Timedelta(hours=h, minutes=m)
    if candidate < ts:
        candidate += pd.Timedelta(days=1)
    return min((candidate - ts).total_seconds() / 60.0, 120.0)


def compute(data_window) -> dict[str, float]:
    """
    data_window: UTC pd.Timestamp (or anything castable to one).
    Returns a flat dict of all time-of-day features.
    """
    ts = pd.Timestamp(data_window)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")

    out: dict[str, float] = {}
    hour = ts.hour
    session_idx = next(
        (i for i, (_, s, e) in enumerate(_SESSIONS) if s <= hour < e), len(_SESSIONS) - 1
    )
    for i, (name, _, _) in enumerate(_SESSIONS):
        out[f"session_{name}"] = 1.0 if i == session_idx else 0.0

    out["is_weekend"] = 1.0 if ts.dayofweek >= 5 else 0.0

    for i, day in enumerate(_DAYS):
        out[f"dow_{day}"] = 1.0 if ts.dayofweek == i else 0.0

    m = ts.minute
    out["minute_sin"] = float(np.sin(2 * np.pi * m / 60))
    out["minute_cos"] = float(np.cos(2 * np.pi * m / 60))

    out["minutes_until_0800"] = _minutes_until(ts, 8, 0)
    out["minutes_until_1430"] = _minutes_until(ts, 14, 30)

    out["monday_asia_open"] = 1.0 if (
        (ts.dayofweek == 6 and ts.hour >= 23)
        or (ts.dayofweek == 0 and ts.hour < 4)
    ) else 0.0

    return out
