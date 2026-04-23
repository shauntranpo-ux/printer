"""
Session awareness for elevated skip thresholds on specific trading
windows where idiosyncratic volatility is historically higher.

- Weekend (Saturday/Sunday UTC): retail-heavy, lower institutional
  liquidity, more meme-driven moves
- US afternoon (18:00-22:00 UTC, roughly 1-5pm ET): post-lunch retail
  activity, Robinhood-style flow peaks

During these sessions, variance is higher during these sessions. Rather
than trying to predict direction, we raise Min EV so only the clearest
setups trigger.
"""

from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional


def current_session(now: Optional[float] = None) -> str:
    """
    Returns session label: 'weekend', 'us_afternoon', or 'normal'.

    Args:
        now: unix timestamp (None = use current time)
    """
    import time
    ts = now if now is not None else time.time()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)

    if dt.weekday() >= 5:
        return "weekend"

    if 18 <= dt.hour < 22:
        return "us_afternoon"

    return "normal"


def session_min_ev_multiplier(session: str) -> float:
    """
    Multiplier applied to base Min EV for the current session.

    Weekend and US afternoon get 1.25x (25% stricter) to suppress
    overtrading in retail-heavy windows.
    """
    if session in ("weekend", "us_afternoon"):
        return 1.25
    return 1.0

