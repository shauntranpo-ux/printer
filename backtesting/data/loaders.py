"""
Data loaders for the backtesting layer.

Each loader returns a pandas DataFrame with a UTC-indexed DatetimeIndex.
Fails loudly when data is missing, malformed, or insufficient.

# TODO: Adjust base_path and file naming conventions to match actual disk layout.
# Current assumption: data/historical/{asset}/bars_10s.parquet or .csv
"""
from __future__ import annotations
import os
import warnings
import pandas as pd
import numpy as np
from typing import Optional

_BASE_PATH = os.environ.get("KALSHI_DATA_PATH", "data/historical")
MIN_HISTORY_DAYS = 180


def _resolve_path(asset: str, filename: str, base_path: str = _BASE_PATH) -> str:
    return os.path.join(base_path, asset.lower(), filename)


def _load_parquet_or_csv(path: str) -> pd.DataFrame:
    """Load parquet if available, fall back to CSV. Raises FileNotFoundError if neither."""
    parquet_path = path if path.endswith(".parquet") else path + ".parquet"
    csv_path = path if path.endswith(".csv") else path + ".csv"
    if os.path.exists(parquet_path):
        return pd.read_parquet(parquet_path)
    if os.path.exists(csv_path):
        return pd.read_csv(csv_path, parse_dates=["timestamp"])
    raise FileNotFoundError(
        f"No data file found at {parquet_path} or {csv_path}. "
        f"# TODO: Verify data path matches actual disk layout."
    )


def _enforce_utc(df: pd.DataFrame, ts_col: str = "timestamp") -> pd.DataFrame:
    """Ensure timestamp column is UTC-aware. Raises if column is missing."""
    if ts_col not in df.columns:
        raise ValueError(f"Missing '{ts_col}' column in DataFrame")
    col = pd.to_datetime(df[ts_col])
    if col.dt.tz is None:
        col = col.dt.tz_localize("UTC")
    else:
        col = col.dt.tz_convert("UTC")
    df = df.copy()
    df[ts_col] = col
    return df.sort_values(ts_col).reset_index(drop=True)


def _check_history_length(df: pd.DataFrame, asset: str, ts_col: str = "timestamp") -> None:
    """Raise if history is shorter than MIN_HISTORY_DAYS."""
    if df.empty:
        raise ValueError(f"[{asset}] Loaded DataFrame is empty.")
    span = (df[ts_col].max() - df[ts_col].min()).days
    if span < MIN_HISTORY_DAYS:
        raise ValueError(
            f"[{asset}] Insufficient history: {span} days < {MIN_HISTORY_DAYS} days minimum. "
            f"Extend historical data before running backtests."
        )


def _filter_date_range(
    df: pd.DataFrame,
    start: Optional[str],
    end: Optional[str],
    ts_col: str = "timestamp",
) -> pd.DataFrame:
    if start:
        df = df[df[ts_col] >= pd.Timestamp(start, tz="UTC")]
    if end:
        df = df[df[ts_col] <= pd.Timestamp(end, tz="UTC")]
    return df.reset_index(drop=True)


def load_bars(
    asset: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    base_path: str = _BASE_PATH,
    check_min_history: bool = True,
) -> pd.DataFrame:
    """
    Load underlying price bars (10-second granularity by default).
    Returns DataFrame with columns: timestamp (UTC), open, high, low, close, volume
    # TODO: Adjust filename 'bars_10s' to match actual disk layout.
    """
    path = _resolve_path(asset, "bars_10s", base_path)
    df = _load_parquet_or_csv(path)
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"[{asset}] bars missing columns: {missing}")
    df = _enforce_utc(df)
    df = _filter_date_range(df, start_date, end_date)
    if check_min_history:
        _check_history_length(df, asset)
    return df


def load_l2_snapshots(
    asset: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    base_path: str = _BASE_PATH,
) -> pd.DataFrame:
    """
    Load L2 book snapshots.
    Returns DataFrame with columns: timestamp (UTC), bids, asks
    # TODO: Adjust filename 'l2_snapshots' to match actual disk layout.
    """
    path = _resolve_path(asset, "l2_snapshots", base_path)
    df = _load_parquet_or_csv(path)
    required = {"timestamp", "bids", "asks"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"[{asset}] l2_snapshots missing columns: {missing}")
    return _enforce_utc(df)


def load_trades(
    asset: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    base_path: str = _BASE_PATH,
) -> pd.DataFrame:
    """
    Load trade tape.
    Returns DataFrame with columns: timestamp (UTC), price, size, aggressor_side
    # TODO: Adjust filename 'trades' to match actual disk layout.
    """
    path = _resolve_path(asset, "trades", base_path)
    df = _load_parquet_or_csv(path)
    required = {"timestamp", "price", "size", "aggressor_side"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"[{asset}] trades missing columns: {missing}")
    df = _enforce_utc(df)
    if not df["aggressor_side"].isin(["buy", "sell"]).all():
        bad = df[~df["aggressor_side"].isin(["buy", "sell"])]["aggressor_side"].unique()
        raise ValueError(f"[{asset}] trades has invalid aggressor_side values: {bad}")
    return df


def load_funding(
    asset: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    base_path: str = _BASE_PATH,
) -> pd.DataFrame:
    """
    Load funding rate and open interest data.
    Returns DataFrame with columns: timestamp (UTC), funding_rate, open_interest
    # TODO: Adjust filename 'funding' to match actual disk layout.
    """
    path = _resolve_path(asset, "funding", base_path)
    df = _load_parquet_or_csv(path)
    required = {"timestamp", "funding_rate", "open_interest"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"[{asset}] funding missing columns: {missing}")
    return _enforce_utc(df)


def load_kalshi_ticks(
    asset: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    base_path: str = _BASE_PATH,
) -> pd.DataFrame:
    """
    Load Kalshi contract tick data.
    Returns DataFrame with columns: timestamp (UTC), yes_bid, yes_ask, no_bid, no_ask[, seconds_to_expiry]
    If data is not available, returns empty DataFrame with schema columns (logs a warning).
    # TODO: Adjust filename 'kalshi_ticks' to match actual disk layout.
    """
    path = _resolve_path(asset, "kalshi_ticks", base_path)
    try:
        df = _load_parquet_or_csv(path)
    except FileNotFoundError:
        warnings.warn(
            f"[{asset}] No Kalshi tick data found at {path}. "
            f"Windows without tick data will be skipped.",
            stacklevel=2,
        )
        return pd.DataFrame(columns=["timestamp", "yes_bid", "yes_ask", "no_bid", "no_ask"])
    required = {"timestamp", "yes_bid", "yes_ask", "no_bid", "no_ask"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"[{asset}] kalshi_ticks missing columns: {missing}")
    return _enforce_utc(df)
