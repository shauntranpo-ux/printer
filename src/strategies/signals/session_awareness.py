"""
Session awareness for elevated skip thresholds on specific trading
windows where idiosyncratic volatility is historically higher.

For DOGE specifically:
- Weekend (Saturday/Sunday UTC): retail-heavy, lower institutional
  liquidity, more meme-driven moves
- US afternoon (18:00-22:00 UTC, roughly 1-5pm ET): post-lunch retail
  activity, Robinhood-style flow peaks
- Retail-FOMO weekend window (Fri 16:00 UTC through Sun 22:00 UTC) when
  Kalshi YES quote shows continuation pressure: this is the D3 strategy
  from the strategy plan. Documented sources:
    https://www.kucoin.com/news/flash/dogecoin-price-rises-21-amid-191-surge-in-trading-volume
    https://www.sciencedirect.com/science/article/abs/pii/S0378426624001894

During default weekend / US-afternoon sessions, DOGE's idiosyncratic
variance is higher and we raise Min EV. The retail-FOMO override lowers
Min EV (and halves stake) when the continuation signal is present.
"""

from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional


WEEKEND_FOMO_YES_QUOTE_FLOOR_CENTS = 55.0
WEEKEND_FOMO_MIN_EV_MULTIPLIER = 0.9
WEEKEND_FOMO_SIZE_FACTOR = 0.5


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


def is_weekend_retail_window(now: Optional[float] = None) -> bool:
    """
    Returns True during Fri 16:00 UTC through Sun 22:00 UTC, the documented
    DOGE retail/Robinhood activity window where weekend volume runs ~1.4-1.9x
    the weekday baseline.
    """
    import time
    ts = now if now is not None else time.time()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    weekday = dt.weekday()
    hour = dt.hour

    if weekday == 4 and hour >= 16:  # Friday >= 16:00
        return True
    if weekday in (5, 6):
        if weekday == 6 and hour >= 22:
            return False
        return True
    return False


def is_weekend_retail_fomo(
    yes_ask_cents: float,
    velocity: str,
    now: Optional[float] = None,
) -> bool:
    """
    D3 trigger condition: in the weekend retail window AND the Kalshi YES
    quote shows continuation pressure (rising velocity, YES >= 55c).

    The YES-quote floor and rising-velocity gate are deliberately joint:
    Kalshi velocity rising alone is too noisy on DOGE; the YES >= 55c
    floor filters to the cases where retail order flow has actually moved
    the market price.
    """
    if not is_weekend_retail_window(now):
        return False
    if velocity != "rising":
        return False
    if yes_ask_cents < WEEKEND_FOMO_YES_QUOTE_FLOOR_CENTS:
        return False
    return True


def session_min_ev_multiplier(session: str, retail_fomo: bool = False) -> float:
    """
    Multiplier applied to base Min EV for the current session.

    Default: weekend and US afternoon get 1.25x (25% stricter) to suppress
    overtrading in retail-heavy windows.

    Override: when the D3 retail-FOMO condition fires, the multiplier drops
    to 0.9 (10% looser). The looser threshold is paired with a 0.5x size
    factor exposed via `weekend_fomo_size_factor` in the strategy's
    contributing_signals; the order-placement layer is responsible for
    honouring it.
    """
    if retail_fomo:
        return WEEKEND_FOMO_MIN_EV_MULTIPLIER
    if session in ("weekend", "us_afternoon"):
        return 1.25
    return 1.0
