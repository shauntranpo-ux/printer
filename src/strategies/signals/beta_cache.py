"""
Per-asset beta cache.

Betas refit weekly from historical 1-min returns in data/historical/.
Cache persists to data/betas.json. Strategies read the cached value.

If the cache is missing or stale (>14 days old), the strategy uses a
conservative default beta from ASSET_DEFAULT_BETAS.
"""

from __future__ import annotations
import json
import math
import os
import time
from pathlib import Path
from typing import Optional

from strategies.signals.rolling_beta import (
    compute_beta_from_returns, log_returns_from_prices
)

BETA_CACHE_PATH = Path("data/betas.json")
STALE_AFTER_DAYS = 14

ASSET_DEFAULT_BETAS = {
    "BTC": 1.00,
    "ETH": 1.10,   # historically 1.0-1.2 vs BTC on intraday
    "SOL": 1.70,   # higher beta (section 5)
    "XRP": 0.80,   # lower, more idiosyncratic
    "DOGE": 1.30,  # retail-flow driven
}


def load_beta(asset: str) -> float:
    """Return cached beta or conservative default."""
    if not BETA_CACHE_PATH.exists():
        return ASSET_DEFAULT_BETAS.get(asset, 1.0)
    try:
        with open(BETA_CACHE_PATH) as f:
            cache = json.load(f)
        entry = cache.get(asset)
        if not entry:
            return ASSET_DEFAULT_BETAS.get(asset, 1.0)
        age_days = (time.time() - entry["computed_at"]) / 86400
        if age_days > STALE_AFTER_DAYS:
            return ASSET_DEFAULT_BETAS.get(asset, 1.0)
        return float(entry["beta"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return ASSET_DEFAULT_BETAS.get(asset, 1.0)


def refit_all_betas(data_dir: str = "data/historical") -> dict:
    """
    Refit betas for all non-BTC assets from historical parquet files.
    Returns the dict that was saved.

    Note on data/split_config.json: betas intentionally use the most recent
    data (data/historical/*_1m_2026.parquet), NOT the pre-2024 training set.
    Betas are correlation estimates — they should reflect the current market
    regime. The split config applies to BV3 win-rate tables (where leakage
    is a model-validity concern), not to rolling correlation parameters.

    Usage:
        python -c "from strategies.signals.beta_cache import refit_all_betas; refit_all_betas()"
    """
    import pandas as pd

    data_path = Path(data_dir)
    if not (data_path / "BTC_1m_2026.parquet").exists():
        raise FileNotFoundError(
            f"BTC historical data missing at {data_path}. "
            "Run scripts/download_historical.py first."
        )

    btc_df = pd.read_parquet(data_path / "BTC_1m_2026.parquet")
    btc_df = btc_df.sort_values("open_time")
    btc_prices_list = list(zip(btc_df["open_time"].astype("int64") // 10**6, btc_df["close"]))
    btc_returns = log_returns_from_prices(btc_prices_list)

    cache = {"BTC": {"beta": 1.00, "computed_at": time.time()}}

    for asset in ["ETH", "SOL", "XRP", "DOGE"]:
        path = data_path / f"{asset}_1m_2026.parquet"
        if not path.exists():
            cache[asset] = {
                "beta": ASSET_DEFAULT_BETAS[asset],
                "computed_at": time.time(),
                "source": "default (historical data missing)",
            }
            continue
        a_df = pd.read_parquet(path).sort_values("open_time")
        a_prices = list(zip(a_df["open_time"].astype("int64") // 10**6, a_df["close"]))

        a_by_min = {int(ts // 60): p for ts, p in a_prices}
        b_by_min = {int(ts // 60): p for ts, p in btc_prices_list}
        common = sorted(set(a_by_min) & set(b_by_min))
        if len(common) < 1000:
            cache[asset] = {
                "beta": ASSET_DEFAULT_BETAS[asset],
                "computed_at": time.time(),
                "source": "default (insufficient aligned data)",
            }
            continue

        aligned_a = [(b, a_by_min[b]) for b in common]
        aligned_b = [(b, b_by_min[b]) for b in common]

        a_rets = log_returns_from_prices(aligned_a)
        b_rets = log_returns_from_prices(aligned_b)
        beta = compute_beta_from_returns(a_rets, b_rets)

        cache[asset] = {
            "beta": float(beta) if beta is not None else ASSET_DEFAULT_BETAS[asset],
            "computed_at": time.time(),
            "source": "refit" if beta is not None else "default (beta compute failed)",
            "sample_size": len(a_rets),
        }

    BETA_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(BETA_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)
    return cache
