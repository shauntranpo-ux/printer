"""
Layer 1 IC analysis using real Kalshi settlement outcomes (per-market strikes).

Each settlement row provides:
  window_open  — market open time (UTC)
  strike       — actual ATM strike for this specific Kalshi market
  result       — real YES=1 / NO=0 outcome settled by Kalshi

For each settlement, signals are computed from the 60 1-min bars
immediately preceding window_open — the same context window the live
bot uses at decision time. We take the final bar's prediction value
(what the bot would output at market open) and correlate it against
the real Kalshi outcome.

This corrects the trivial V1 IC artifact in the synthetic-label
evaluation, where a fixed global strike made BS p_yes trivially
correlated with the synthetic label.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

_log = logging.getLogger(__name__)

_ROOT = Path(__file__).parent.parent.parent
if str(_ROOT / 'src') not in sys.path:
    sys.path.insert(0, str(_ROOT / 'src'))

from backtesting.research.ic_analysis import evaluate_signal
from backtesting.research.layer1 import layer1_verdict
from backtesting.research.signal_extractor import SIGNAL_NAMES, extract_all_signals

CONTEXT_BARS = 60   # 1-min bars of pre-window context used per signal evaluation
MIN_CONTEXT  = 30   # skip windows with fewer than this many pre-window bars


def load_settlements(asset: str, data_dir: Optional[str] = None) -> pd.DataFrame:
    """Load Kalshi settlements parquet for the given asset.

    Raises FileNotFoundError with a run-this-command hint if the file is missing.
    """
    if data_dir is None:
        data_dir = str(_ROOT / 'data' / 'historical')
    path = os.path.join(data_dir, f'{asset.upper()}_kalshi_settlements.parquet')
    if not os.path.exists(path):
        raise FileNotFoundError(
            f'Settlements not found: {path}\n'
            f'Run: python backtesting/scripts/fetch_kalshi_settlements.py --asset {asset}'
        )
    df = pd.read_parquet(path)
    if df['window_open'].dt.tz is None:
        df['window_open'] = df['window_open'].dt.tz_localize('UTC')
    else:
        df['window_open'] = df['window_open'].dt.tz_convert('UTC')
    return df.sort_values('window_open').reset_index(drop=True)


def _window_signals(
    bars_idx: pd.DataFrame,
    window_open: pd.Timestamp,
    strike: float,
    asset: str,
) -> dict[str, float] | None:
    """Extract one prediction per signal for a single settlement window.

    bars_idx must be a DataFrame indexed by UTC timestamp (sorted, tz-aware).
    Returns None if fewer than MIN_CONTEXT bars exist before window_open.
    """
    context_end   = window_open - pd.Timedelta(seconds=1)
    context_start = window_open - pd.Timedelta(minutes=CONTEXT_BARS)
    context = bars_idx.loc[context_start:context_end]
    if len(context) < MIN_CONTEXT:
        return None
    context = context.tail(CONTEXT_BARS).reset_index()
    sigs = extract_all_signals(context, strike=strike, asset=asset)
    return {name: float(arr[-1]) for name, arr in sigs.items()}


def run_layer1_real(
    bars: pd.DataFrame,
    settlements: pd.DataFrame,
    asset: str,
) -> Dict[str, Any]:
    """Layer 1 IC analysis using real Kalshi settlement outcomes.

    Args:
        bars: 1-min price bars with 'timestamp' (UTC) and 'close' columns.
        settlements: DataFrame from load_settlements() with window_open, strike, result.
        asset: 'BTC', 'ETH', 'SOL', 'XRP'.

    Returns a dict with the same structure as run_layer1(), plus:
        'n_windows': int   — number of settlement windows evaluated
        'n_skipped': int   — windows skipped due to insufficient bar context
        'label_mode': 'real_settlements'
    """
    bars = bars.sort_values('timestamp').reset_index(drop=True)

    # Restrict bars to the settlement date range to avoid scanning all 4.5M rows
    t_min = settlements['window_open'].min() - pd.Timedelta(minutes=CONTEXT_BARS + 5)
    t_max = settlements['window_open'].max() + pd.Timedelta(minutes=15)
    bars = bars[(bars['timestamp'] >= t_min) & (bars['timestamp'] <= t_max)].copy()
    bars_idx = bars.set_index('timestamp').sort_index()

    _log.info('[%s] real-IC: %d bars aligned for %d settlement windows',
              asset, len(bars), len(settlements))

    all_preds: dict[str, list[float]] = {name: [] for name in SIGNAL_NAMES}
    outcomes: list[int] = []
    n_skipped = 0

    for _, row in settlements.iterrows():
        preds = _window_signals(bars_idx, row['window_open'], float(row['strike']), asset)
        if preds is None:
            n_skipped += 1
            continue
        for name in SIGNAL_NAMES:
            all_preds[name].append(preds[name])
        outcomes.append(int(row['result']))

    n_windows = len(outcomes)
    _log.info('[%s] real-IC: %d windows used, %d skipped (insufficient context)',
              asset, n_windows, n_skipped)

    if n_windows < 10:
        return {
            'signals':    {},
            'verdict':    'FAIL',
            'n_failing':  len(SIGNAL_NAMES),
            'n_signals':  len(SIGNAL_NAMES),
            'n_windows':  n_windows,
            'n_skipped':  n_skipped,
            'label_mode': 'real_settlements',
        }

    outcomes_arr = np.array(outcomes, dtype=np.int8)
    signal_results: dict[str, Any] = {}
    n_failing = 0

    for name in SIGNAL_NAMES:
        preds_arr = np.array(all_preds[name])
        ic_result = evaluate_signal(
            preds_arr,
            outcomes_arr,
            {lag: outcomes_arr for lag in [1, 2, 4, 8]},
        )
        signal_results[name] = {
            'ic':       ic_result.ic,
            'icir':     ic_result.icir,
            't_stat':   ic_result.t_stat,
            'ic_decay': ic_result.ic_decay,
            'n_obs':    ic_result.n_obs,
            'verdict':  ic_result.verdict,
        }
        if ic_result.verdict == 'FAIL':
            n_failing += 1

    return {
        'signals':    signal_results,
        'verdict':    layer1_verdict(n_failing, len(SIGNAL_NAMES)),
        'n_failing':  n_failing,
        'n_signals':  len(SIGNAL_NAMES),
        'n_windows':  n_windows,
        'n_skipped':  n_skipped,
        'label_mode': 'real_settlements',
    }
