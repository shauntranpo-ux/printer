"""
Event calendar reader for XRP (and extensible to other assets).

Reads data/xrp_events.json. Returns True if the current time is within a
skip window around any scheduled event.

The calendar is loaded at strategy init and cached. Call reload() to
refresh without restarting.
"""

from __future__ import annotations
import json
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional


CALENDAR_DEFAULT_PATH = Path("data/xrp_events.json")

_SKIP_WINDOW_MINUTES = {
    "high": 30,
    "medium": 15,
    "low": 5,
}


class EventCalendar:
    def __init__(self, path: Path = CALENDAR_DEFAULT_PATH):
        self.path = path
        self._events: list = []
        self._loaded_at: float = 0.0
        self.reload()

    def reload(self) -> None:
        self._events = []
        self._loaded_at = time.time()
        if not self.path.exists():
            return
        try:
            with open(self.path) as f:
                data = json.load(f)
            for e in data.get("events", []):
                try:
                    dt = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    self._events.append({
                        "timestamp": dt.timestamp(),
                        "reason": e.get("reason", "unknown"),
                        "severity": e.get("severity", "medium"),
                    })
                except (KeyError, ValueError):
                    continue
        except (json.JSONDecodeError, OSError):
            pass

    def is_event_active(self, now: Optional[float] = None) -> tuple[bool, str]:
        """Returns (is_active, reason)."""
        if not self._events:
            return False, ""

        current = now if now is not None else time.time()
        for e in self._events:
            window_min = _SKIP_WINDOW_MINUTES.get(e["severity"], 15)
            window_sec = window_min * 60
            if abs(current - e["timestamp"]) <= window_sec:
                return True, f"{e['reason']} ({e['severity']})"
        return False, ""
