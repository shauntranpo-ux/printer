"""
Data loaders for the backtesting layer.

Each loader returns a pandas DataFrame with a UTC-indexed DatetimeIndex.
Bars are loaded from flat files: data/historical/{ASSET}_1m_extended.parquet

Path and filename pattern are configurable via backtesting/configs/backtest.yaml:
    data.historical_root: data/historical
    data.bars_filename_pattern: "{asset_upper}_1m_extended.parquet"

Non-bars loaders (L2, trades, funding, kalshi_ticks) return empty DataFrames with
a warning when data files are not found, allowing the pipeline to continue without them.
"""
from __future__ import annotations
import logging
import os
import warnings
import pandas as pd
import numpy as np
from typing import Optional

logger = logging.getLogger(__name__)

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.normpath(os.path.join(_THIS_DIR, "..", "configs", "backtest.yaml"))

MIN_HISTORY_DAYS = 180

# Alternate column names found in various data sources → canonical lowercase
_COLUMN_ALIASES: dict[str, str] = {
    "open_time": "timestamp", "time": "timestamp",
    "Open": "open", "High": "high", "Low": "low",
    "Close": "close", "Volume": "volume",
    "vol": "volume",
    "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume",
}


def _load_data_config() -> dict:
    """Load data section from backtest.yaml; fall back to empty dict if unavailable."""
    try:
        import yaml
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("data", {})
    except Exception:
        return {}


def _get_historical_root() -> str:
    cfg = _load_data_config()
    return cfg.get("historical_root", os.environ.get("KALSHI_DATA_PATH", "data/historical"))


def _get_filename_pattern() -> str:
    cfg = _load_data_config()
    return cfg.get("bars_filename_pattern", "{asset_upper}_1m_extended.parquet")


def _resolve_bars_path(asset: str, base_path: Optional[str] = None) -> str:
    """Return the full file path for the bars parquet.

    If base_path is given, builds legacy-style path: base_path/{ASSET}_1m_extended.parquet
    Otherwise reads historical_root and bars_filename_pattern from config.
    """
    if base_path is not None:
        return os.path.join(base_path, f"{asset.upper()}_1m_extended.parquet")
    root = _get_historical_root()
    pattern = _get_filename_pattern()
    filename = pattern.format(asset_upper=asset.upper(), asset_lower=asset.lower())
    return os.path.join(root, filename)


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename known column aliases to canonical lowercase names."""
    rename = {col: _COLUMN_ALIASES[col] for col in df.columns if col in _COLUMN_ALIASES}
    return df.rename(columns=rename) if rename else df


def _load_parquet_or_csv(path: str) -> pd.DataFrame:
    """Load parquet if available, fall back to CSV. Raises FileNotFoundError if neither."""
    parquet_path = path if path.endswith(".parquet") else path + ".parquet"
    csv_path = (
        os.path.splitext(path)[0] + ".csv" if path.endswith(".parquet") else path + ".csv"
    )
    if os.path.exists(parquet_path):
        return pd.read_parquet(parquet_path)
    if os.path.exists(csv_path):
        return pd.read_csv(csv_path)
    raise FileNotFoundError(
        f"No data file found at:\n  {parquet_path}\n  {csv_path}\n"
        f"Check data.historical_root and data.bars_filename_pattern in backtest.yaml."
    )


def _enforce_utc(df: pd.DataFrame, ts_col: str = "timestamp") -> pd.DataFrame:
    """Ensure timestamp column is UTC-aware Timestamp.

    Handles three source formats:
    - float/int (epoch seconds, e.g. 1502942400.0)
    - string (ISO-8601 or similar)
    - datetime / Timestamp (already parsed)
    """
    if ts_col not in df.columns:
        raise ValueError(f"Missing '{ts_col}' column in DataFrame")
    raw = df[ts_col]
    if pd.api.types.is_float_dtype(raw) or pd.api.types.is_integer_dtype(raw):
        if raw.dropna().median() > 1e10:
            col = pd.to_datetime(raw, unit="ms", utc=True)
        else:
            col = pd.to_datetime(raw, unit="s", utc=True)
    else:
        col = pd.to_datetime(raw)
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


def _detect_granularity_seconds(df: pd.DataFrame, ts_col: str = "timestamp") -> Optional[int]:
    """Estimate bar granularity from median inter-bar gap in seconds."""
    if len(df) < 2:
        return None
    diffs = df[ts_col].diff().dropna()
    median_s = diffs.dt.total_seconds().median()
    return max(1, int(round(median_s)))


def load_bars(
    asset: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    base_path: Optional[str] = None,
    check_min_history: bool = True,
) -> pd.DataFrame:
    """
    Load underlying price bars (1-minute granularity by default).

    Returns DataFrame with columns: timestamp (UTC), open, high, low, close, volume

    Path is resolved from backtest.yaml (data.historical_root + data.bars_filename_pattern).
    Pass base_path to override (legacy: looks for {base_path}/{ASSET}_1m_extended.parquet).
    """
    path = _resolve_bars_path(asset, base_path)
    df = _load_parquet_or_csv(path)
    df = _normalize_columns(df)

    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"[{asset}] bars missing columns: {missing}")

    df = _enforce_utc(df)
    df = _filter_date_range(df, start_date, end_date)

    if check_min_history:
        _check_history_length(df, asset)

    granularity = _detect_granularity_seconds(df)
    logger.info(
        "[%s] Loaded %s bars | %s → %s | granularity=%ss",
        asset.upper(),
        f"{len(df):,}",
        df["timestamp"].min().date(),
        df["timestamp"].max().date(),
        granularity,
    )
    return df


def load_l2_snapshots(
    asset: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    base_path: Optional[str] = None,
) -> pd.DataFrame:
    """
    Load L2 book snapshots. Returns empty DataFrame if data is not available.
    Expected columns: timestamp (UTC), bids, asks
    """
    root = base_path or _get_historical_root()
    path = os.path.join(root, f"{asset.upper()}_l2_snapshots")
    try:
        df = _load_parquet_or_csv(path)
    except FileNotFoundError:
        warnings.warn(
            f"[{asset}] L2 snapshot data not found. Returning empty DataFrame.",
            stacklevel=2,
        )
        return pd.DataFrame(columns=["timestamp", "bids", "asks"])
    required = {"timestamp", "bids", "asks"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"[{asset}] l2_snapshots missing columns: {missing}")
    return _enforce_utc(df)


def load_trades(
    asset: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    base_path: Optional[str] = None,
) -> pd.DataFrame:
    """
    Load trade tape. Returns empty DataFrame if data is not available.
    Expected columns: timestamp (UTC), price, size, aggressor_side
    """
    root = base_path or _get_historical_root()
    path = os.path.join(root, f"{asset.upper()}_trades")
    try:
        df = _load_parquet_or_csv(path)
    except FileNotFoundError:
        warnings.warn(
            f"[{asset}] Trade tape data not found. Returning empty DataFrame.",
            stacklevel=2,
        )
        return pd.DataFrame(columns=["timestamp", "price", "size", "aggressor_side"])
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
    base_path: Optional[str] = None,
) -> pd.DataFrame:
    """
    Load funding rate and open interest data. Returns empty DataFrame if not available.
    Expected columns: timestamp (UTC), funding_rate, open_interest
    """
    root = base_path or _get_historical_root()
    path = os.path.join(root, f"{asset.upper()}_funding")
    try:
        df = _load_parquet_or_csv(path)
    except FileNotFoundError:
        warnings.warn(
            f"[{asset}] Funding rate data not found. Returning empty DataFrame.",
            stacklevel=2,
        )
        return pd.DataFrame(columns=["timestamp", "funding_rate", "open_interest"])
    required = {"timestamp", "funding_rate", "open_interest"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"[{asset}] funding missing columns: {missing}")
    return _enforce_utc(df)


def load_kalshi_ticks(
    asset: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    base_path: Optional[str] = None,
) -> pd.DataFrame:
    """
    Load Kalshi contract tick data. Returns empty DataFrame if not available.
    Expected columns: timestamp (UTC), yes_bid, yes_ask, no_bid, no_ask[, seconds_to_expiry]
    """
    root = base_path or _get_historical_root()
    path = os.path.join(root, f"{asset.upper()}_kalshi_ticks")
    try:
        df = _load_parquet_or_csv(path)
    except FileNotFoundError:
        warnings.warn(
            f"[{asset}] No Kalshi tick data found. Windows without tick data will be skipped.",
            stacklevel=2,
        )
        return pd.DataFrame(columns=["timestamp", "yes_bid", "yes_ask", "no_bid", "no_ask"])
    required = {"timestamp", "yes_bid", "yes_ask", "no_bid", "no_ask"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"[{asset}] kalshi_ticks missing columns: {missing}")
    return _enforce_utc(df)


# ---------------------------------------------------------------------------
# Strategy C — Kalshi hourly strike-ladder loader
# ---------------------------------------------------------------------------

_LADDER_REQUIRED = {
    "event_id", "event_close_time", "timestamp", "strike",
    "yes_bid", "yes_ask", "no_bid", "no_ask", "mid_price", "volume", "market_id",
}


def load_strike_ladder_history(
    asset: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    base_path: Optional[str] = None,
) -> pd.DataFrame:
    """
    Load Kalshi hourly strike-ladder history for Strategy C backtesting.

    Returns one row per (event_id, strike, snapshot_timestamp).

    # TODO: Verify exact path convention once live Kalshi ladder data pipeline is built.
    Expected layout:
        data/kalshi/hourly/{ASSET_UPPERCASE}/
            one parquet file per event OR one merged parquet for the full history

    Required columns per row:
        event_id, event_close_time (UTC), timestamp (UTC), strike,
        yes_bid, yes_ask, no_bid, no_ask, mid_price, volume, market_id

    Incomplete individual events (missing strikes) are dropped with a warning.
    Missing directory raises FileNotFoundError immediately.
    """
    ladder_dir = base_path or os.path.join("data", "kalshi", "hourly", asset.upper())

    if not os.path.exists(ladder_dir):
        raise FileNotFoundError(
            f"[{asset}] Kalshi hourly ladder directory not found: {ladder_dir}\n"
            f"Expected per-event parquet files at data/kalshi/hourly/{asset.upper()}/\n"
            f"# TODO: populate with live Kalshi ladder history before running Strategy C."
        )

    parquet_files = sorted(
        f for f in os.listdir(ladder_dir) if f.endswith(".parquet")
    )
    if not parquet_files:
        raise FileNotFoundError(
            f"[{asset}] No parquet files found in {ladder_dir}"
        )

    dfs: list[pd.DataFrame] = []
    for fname in parquet_files:
        fpath = os.path.join(ladder_dir, fname)
        try:
            df = pd.read_parquet(fpath)
        except Exception as exc:
            warnings.warn(
                f"[{asset}] Skipping malformed parquet '{fname}': {exc}",
                stacklevel=2,
            )
            continue

        missing = _LADDER_REQUIRED - set(df.columns)
        if missing:
            warnings.warn(
                f"[{asset}] Dropping '{fname}' — missing columns: {missing}",
                stacklevel=2,
            )
            continue
        dfs.append(df)

    if not dfs:
        raise FileNotFoundError(f"[{asset}] No valid ladder files loaded from {ladder_dir}")

    combined = pd.concat(dfs, ignore_index=True)

    # Enforce UTC on the snapshot timestamp
    combined = _enforce_utc(combined, ts_col="timestamp")

    # Enforce UTC on event_close_time separately (may be string or datetime)
    raw_ect = combined["event_close_time"]
    if pd.api.types.is_float_dtype(raw_ect) or pd.api.types.is_integer_dtype(raw_ect):
        combined["event_close_time"] = pd.to_datetime(raw_ect, unit="s", utc=True)
    else:
        ect = pd.to_datetime(raw_ect)
        if ect.dt.tz is None:
            combined["event_close_time"] = ect.dt.tz_localize("UTC")
        else:
            combined["event_close_time"] = ect.dt.tz_convert("UTC")

    combined = _filter_date_range(combined, start_date, end_date)

    # Drop events whose ladder is incomplete (< 5 distinct strikes)
    strike_counts = combined.groupby("event_id")["strike"].nunique()
    incomplete = strike_counts[strike_counts < 5].index
    if len(incomplete) > 0:
        logger.warning(
            "[%s] Dropping %d event(s) with < 5 distinct strikes (incomplete ladders): %s",
            asset.upper(), len(incomplete), list(incomplete)[:5],
        )
        combined = combined[~combined["event_id"].isin(incomplete)]

    logger.info(
        "[%s] Loaded %s ladder rows | %d events | %s → %s",
        asset.upper(),
        f"{len(combined):,}",
        combined["event_id"].nunique(),
        combined["event_close_time"].min().date(),
        combined["event_close_time"].max().date(),
    )
    return combined.reset_index(drop=True)
