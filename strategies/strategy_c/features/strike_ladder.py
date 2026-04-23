"""
Strike ladder parser and stub loader for Kalshi hourly strike-ladder markets.

Snapshot schema expected by parse_ladder():
    {
        "event_id": str,
        "event_close_time": pd.Timestamp,   # UTC
        "strikes": [
            {
                "strike": float,
                "yes_bid": float,   # in [0, 1]; Kalshi contract prices are probabilities
                "yes_ask": float,
                "no_bid": float,
                "no_ask": float,
                "last_price": float,
                "volume": float,
                "market_id": str,
            },
            ...
        ]
    }
    OR snapshot["strikes"] may be a list of tuples:
        (strike_price, yes_bid, yes_ask, no_bid, no_ask, last_price, volume)
    In the tuple form, market_id defaults to "".

# TODO: Wire to live Kalshi hourly market loader once that data pipeline is built.
        The loader should call Kalshi's get_event_markets() and transform into
        the dict format above before calling parse_ladder().
"""
from __future__ import annotations
import logging
import pandas as pd

logger = logging.getLogger(__name__)

_REQUIRED_PRICE_COLS = ("yes_bid", "yes_ask", "no_bid", "no_ask")


def _row_to_dict(item) -> dict:
    """Normalise a tuple or dict row from snapshot['strikes']."""
    if isinstance(item, dict):
        return item
    # tuple: (strike, yes_bid, yes_ask, no_bid, no_ask, last_price, volume)
    if len(item) >= 7:
        return {
            "strike": item[0],
            "yes_bid": item[1],
            "yes_ask": item[2],
            "no_bid": item[3],
            "no_ask": item[4],
            "last_price": item[5],
            "volume": item[6],
            "market_id": item[7] if len(item) > 7 else "",
        }
    raise ValueError(f"Cannot interpret strike row with {len(item)} elements: {item!r}")


def parse_ladder(snapshot: dict) -> pd.DataFrame:
    """
    Parse a Kalshi strike ladder snapshot into a clean per-strike DataFrame.

    Returns a DataFrame with columns:
        strike, yes_bid, yes_ask, no_bid, no_ask, mid_price,
        implied_probability, volume, market_id

    Raises:
        ValueError if the ladder has structural violations after edge-trimming.
    """
    raw_rows = snapshot.get("strikes", [])
    if not raw_rows:
        raise ValueError("Snapshot contains no strikes.")

    rows = [_row_to_dict(r) for r in raw_rows]
    df = pd.DataFrame(rows)

    required = {"strike", "yes_bid", "yes_ask", "no_bid", "no_ask"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Snapshot missing required columns: {missing}")

    for col in ("volume", "last_price", "market_id"):
        if col not in df.columns:
            df[col] = 0.0 if col != "market_id" else ""

    df = df.sort_values("strike").reset_index(drop=True)

    # Drop edge rows where all price fields are zero/null (no quotes at tail strikes)
    price_cols = [c for c in _REQUIRED_PRICE_COLS if c in df.columns]
    has_quotes = df[price_cols].gt(0).any(axis=1)
    n_dropped = (~has_quotes).sum()
    if n_dropped > 0:
        logger.warning(
            "Dropped %d strike(s) with no quotes (expected at ladder edges).", n_dropped
        )
        df = df[has_quotes].reset_index(drop=True)

    if df.empty:
        raise ValueError("No strikes remain after dropping rows with missing quotes.")

    # Compute mid_price and implied_probability
    df["mid_price"] = (df["yes_bid"] + df["yes_ask"]) / 2.0
    df["implied_probability"] = df["mid_price"]  # Kalshi prices ARE probabilities

    # Validate: all prices in [0, 1]
    for col in _REQUIRED_PRICE_COLS:
        if col not in df.columns:
            continue
        bad = df[(df[col] < 0) | (df[col] > 1)]
        if not bad.empty:
            raise ValueError(
                f"Column '{col}' has values outside [0, 1] at strikes: "
                f"{bad['strike'].tolist()}"
            )

    # Validate: bids ≤ asks
    if (df["yes_bid"] > df["yes_ask"]).any():
        bad_strikes = df[df["yes_bid"] > df["yes_ask"]]["strike"].tolist()
        raise ValueError(f"yes_bid > yes_ask at strikes: {bad_strikes}")
    if (df["no_bid"] > df["no_ask"]).any():
        bad_strikes = df[df["no_bid"] > df["no_ask"]]["strike"].tolist()
        raise ValueError(f"no_bid > no_ask at strikes: {bad_strikes}")

    # Validate: strikes strictly increasing (guaranteed by sort + uniqueness check)
    if df["strike"].duplicated().any():
        dupes = df[df["strike"].duplicated(keep=False)]["strike"].unique().tolist()
        raise ValueError(f"Duplicate strikes: {dupes}")

    return df[
        ["strike", "yes_bid", "yes_ask", "no_bid", "no_ask",
         "mid_price", "implied_probability", "volume", "market_id"]
    ].copy()
