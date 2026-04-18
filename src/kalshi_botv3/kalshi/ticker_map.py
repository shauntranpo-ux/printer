"""Kalshi 15-minute crypto market ticker utilities.

Verified ticker format (2026-04-17):
  Series: KXBTC15M, KXETH15M, KXXRP15M, KXSOL15M, KXDOGE15M, KXHYPE15M, KXBNB15M
  Market: {SERIES}-{YY}{MON}{DD}{HHMM}  (datetime in US Eastern Time)
  Example: KXBTC15M-26APR171500

If the format has changed, set KALSHI_TICKER_FALLBACK=1 in the environment to
use the list-and-match fallback (fetches all open markets and matches by close_time).

# TODO: verify exact ticker format via live API call at bot startup.
"""

from datetime import UTC, datetime

_SERIES_MAP: dict[str, str] = {
    "BTC": "KXBTC15M",
    "ETH": "KXETH15M",
    "XRP": "KXXRP15M",
    "SOL": "KXSOL15M",
    "DOGE": "KXDOGE15M",
    "HYPE": "KXHYPE15M",
    "BNB": "KXBNB15M",
}

_MONTHS = [
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
]

_MONTH_NUM: dict[str, int] = {m: i + 1 for i, m in enumerate(_MONTHS)}


def _to_eastern(dt: datetime) -> datetime:
    """Convert UTC datetime to US/Eastern (approximation: fixed UTC-4 for EDT)."""
    from datetime import timedelta, timezone

    et = timezone(timedelta(hours=-4))
    return (dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt).astimezone(et)


def build_current_window_ticker(market: str, window_start: datetime) -> str:
    """Return the Kalshi ticker for a 15-minute window.

    Args:
        market: Asset symbol (e.g., "BTC").
        window_start: UTC datetime of the window open.

    Returns:
        Ticker string, e.g., "KXBTC15M-26APR171500".
    """
    series = _SERIES_MAP[market.upper()]
    et = _to_eastern(window_start)
    yy = et.strftime("%y")
    mon = _MONTHS[et.month - 1]
    dd = f"{et.day:02d}"
    hhmm = et.strftime("%H%M")
    return f"{series}-{yy}{mon}{dd}{hhmm}"


def parse_ticker(ticker: str) -> tuple[str, datetime]:
    """Parse a Kalshi 15-minute crypto ticker back to (market, window_start_utc).

    Args:
        ticker: e.g., "KXBTC15M-26APR171500"

    Returns:
        ("BTC", datetime(2026, 4, 17, 21, 0, tzinfo=UTC))  # ET 17:00 → UTC 21:00
    """
    from datetime import timedelta

    series_part, date_part = ticker.rsplit("-", 1)
    market = next(
        (k for k, v in _SERIES_MAP.items() if v == series_part),
        series_part,
    )
    # date_part: "26APR171500"
    yy = int(date_part[:2])
    mon_str = date_part[2:5]
    rest = date_part[5:]
    if len(rest) == 6:
        dd = int(rest[:2])
        hh = int(rest[2:4])
        mm = int(rest[4:6])
    else:
        raise ValueError(f"Cannot parse date portion of ticker: {ticker!r}")

    year = 2000 + yy
    month = _MONTH_NUM[mon_str]
    et_naive = datetime(year, month, dd, hh, mm, 0)
    et_offset = timedelta(hours=-4)
    utc_dt = (et_naive - et_offset).replace(tzinfo=UTC)
    return market, utc_dt
