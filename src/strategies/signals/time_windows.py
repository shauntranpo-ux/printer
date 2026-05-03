"""
Time-of-day trading windows (Vegas / Pacific time).

Three windows, each with a min_ev delta and entry price cap:
  normal    01:00–12:00  — base values
  strict    12:00–17:00  — +4 pp EV, tighter entry cap
  strictest 17:00–01:00  — +8 pp EV, tightest entry cap
"""
from __future__ import annotations

import time as _time
from datetime import datetime
from zoneinfo import ZoneInfo


def get_trading_window(ts: float | None = None, tz: str = "America/Los_Angeles") -> str:
    """Return 'normal', 'strict', or 'strictest' for the given Unix timestamp."""
    if ts is None:
        ts = _time.time()
    hour = datetime.fromtimestamp(ts, tz=ZoneInfo(tz)).hour
    if 1 <= hour < 12:
        return "normal"
    if 12 <= hour < 17:
        return "strict"
    return "strictest"  # 17–24 and 0–1


def get_window_params(config: dict, window: str) -> dict:
    """Return min_ev_delta and max_entry_price_cents for the given window."""
    defaults = {
        "normal":    {"min_ev_delta": 0, "max_entry_price_cents": 75},
        "strict":    {"min_ev_delta": 4, "max_entry_price_cents": 70},
        "strictest": {"min_ev_delta": 8, "max_entry_price_cents": 60},
    }
    windows = config.get("time_windows", defaults)
    params = windows.get(window, defaults.get(window, defaults["normal"]))
    return params
