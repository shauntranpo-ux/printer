from __future__ import annotations

from typing import Any, Dict

import numpy as np
import pandas as pd

from backtesting.metrics.trading import sharpe_ratio

_ASIA_START, _ASIA_END     = 0,  5
_LONDON_START, _LONDON_END = 5,  10


def variance_ratio(prices: np.ndarray, q: int = 4) -> float:
    """
    Lo-MacKinlay (1988) variance ratio at aggregation lag q.
    VR > 1.1 → trending, VR < 0.9 → mean-reverting, ≈1 → random walk.
    """
    n = len(prices)
    if n < q + 5:
        return 1.0
    log_prices = np.log(np.maximum(prices, 1e-12))
    returns = np.diff(log_prices)
    mu = returns.mean()

    var1 = float(np.sum((returns - mu) ** 2) / (n - 1))

    q_returns = log_prices[q:] - log_prices[:-q]
    m = q * (n - q) * (1 - q / n)
    var_q = float(np.sum((q_returns - q * mu) ** 2) / m)

    return (var_q / var1) if var1 > 0 else 1.0


def classify_vol_tercile(bars: pd.DataFrame, window_bars: int = 43_200) -> pd.Series:
    """
    Label each bar 'low', 'mid', or 'high' vol based on rolling realized vol.
    window_bars = 30d × 24h × 60min = 43200 for 1-min bars.
    """
    log_ret = np.log(bars['close'] / bars['close'].shift(1))
    rolling_vol = log_ret.rolling(window_bars).std()
    q33 = rolling_vol.quantile(0.333)
    q67 = rolling_vol.quantile(0.667)
    labels = pd.Series('mid', index=bars.index)
    labels[rolling_vol <= q33] = 'low'
    labels[rolling_vol > q67]  = 'high'
    return labels


def classify_trend_regime(prices: np.ndarray, window: int = 60, q: int = 4) -> str:
    """Return 'trending', 'random', or 'mean_reverting' for a price window."""
    if len(prices) < window:
        return 'random'
    vr = variance_ratio(prices[-window:], q=q)
    if vr > 1.1:
        return 'trending'
    if vr < 0.9:
        return 'mean_reverting'
    return 'random'


def session_label(ts: pd.Timestamp) -> str:
    """Classify UTC timestamp into Asia / London / US trading session."""
    ts_utc = ts.tz_convert('UTC') if ts.tzinfo else ts
    hour = ts_utc.hour
    if _ASIA_START <= hour < _ASIA_END:
        return 'Asia'
    if _LONDON_START <= hour < _LONDON_END:
        return 'London'
    return 'US'


def compute_regime_breakdown(
    trade_log: pd.DataFrame,
    regime_series: pd.Series,
) -> Dict[str, Any]:
    """
    Compute Sharpe per regime cell and per session.
    trade_log: must have 'pnl' and 'timestamp' column OR DatetimeIndex.
    regime_series: Series with DatetimeIndex, values = regime label strings.
    """
    if 'timestamp' in trade_log.columns:
        trade_log = trade_log.set_index('timestamp')

    regime_sharpes: Dict[str, float] = {}
    session_sharpes: Dict[str, float] = {}

    for regime in regime_series.unique():
        regime_ts = regime_series[regime_series == regime].index
        mask = trade_log.index.isin(regime_ts)
        subset = trade_log.loc[mask, 'pnl'].values
        if len(subset) >= 5:
            regime_sharpes[regime] = round(sharpe_ratio(subset), 3)

    for sess in ['Asia', 'London', 'US']:
        mask = trade_log.index.map(session_label) == sess
        subset = trade_log.loc[mask, 'pnl'].values
        if len(subset) >= 5:
            session_sharpes[sess] = round(sharpe_ratio(subset), 3)

    return {'regime_sharpes': regime_sharpes, 'session_sharpes': session_sharpes}


def layer5_verdict(regime_sharpes: Dict[str, float]) -> str:
    if not regime_sharpes:
        return 'INSUFFICIENT_DATA'
    n_pos = sum(1 for v in regime_sharpes.values() if v > 0)
    n_cat = max(round(0.75 * len(regime_sharpes)), 1)
    if n_pos >= n_cat and min(regime_sharpes.values()) > -1.0:
        return 'PASS'
    if n_pos >= round(0.50 * len(regime_sharpes)):
        return 'CONDITIONAL'
    return 'FAIL'


def run_layer5(
    trade_log: pd.DataFrame,
    bars: pd.DataFrame,
    vol_window_bars: int = 43_200,
    vr_window: int = 60,
) -> Dict[str, Any]:
    """
    Layer 5: Regime robustness.
    trade_log: 'pnl' + DatetimeIndex or 'timestamp' column.
    bars: 1-min OHLCV with DatetimeIndex.
    """
    vol_regime = classify_vol_tercile(bars, window_bars=vol_window_bars)

    closes = bars['close'].values
    trend_labels = [
        classify_trend_regime(closes[max(0, i - vr_window):i + 1], window=vr_window)
        for i in range(len(closes))
    ]
    trend_series = pd.Series(trend_labels, index=bars.index)
    regime_combined = vol_regime + '_' + trend_series

    breakdown = compute_regime_breakdown(trade_log, regime_combined)
    verdict   = layer5_verdict(breakdown['regime_sharpes'])

    return {**breakdown, 'verdict': verdict}
