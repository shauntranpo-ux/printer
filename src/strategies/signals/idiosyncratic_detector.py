from __future__ import annotations

import numpy as np


def detect_idiosyncratic_mode(
    prices_60m: list[float],
    btc_prices_60m: list[float],
    beta: float,
    sigma_threshold: float = 2.5,
) -> tuple[bool, dict]:
    """
    Detects when DOGE is moving independently of BTC (meme pump, Elon tweet, etc.).
    Computes residual returns (DOGE - beta*BTC) and z-scores recent residuals vs history.
    Returns (is_idiosyncratic, signals_dict). If True, TA signals are unreliable — skip.
    """
    min_len = min(len(prices_60m), len(btc_prices_60m))
    if min_len < 10:
        return False, {"reason": "insufficient_data", "n_samples": min_len}

    asset_arr = np.array(prices_60m[-min_len:], dtype=float)
    btc_arr   = np.array(btc_prices_60m[-min_len:], dtype=float)

    asset_rets = np.diff(np.log(np.clip(asset_arr, 1e-10, None)))
    btc_rets   = np.diff(np.log(np.clip(btc_arr,   1e-10, None)))

    if len(asset_rets) < 8:
        return False, {"reason": "insufficient_returns", "n_samples": len(asset_rets)}

    residuals = asset_rets - beta * btc_rets

    recent_window  = min(5, len(residuals))
    history_window = min(30, len(residuals))

    recent  = residuals[-recent_window:]
    history = residuals[-history_window:-recent_window] if history_window > recent_window else residuals

    if len(history) < 3:
        return False, {"reason": "insufficient_history"}

    mu    = float(np.mean(history))
    sigma = float(np.std(history))

    if sigma < 1e-8:
        return False, {"reason": "zero_volatility"}

    recent_z = (float(np.mean(recent)) - mu) / sigma
    is_idio  = abs(recent_z) >= sigma_threshold

    return is_idio, {
        "reason":           f"residual_z={recent_z:.2f}" if is_idio else "normal",
        "residual_z":       round(recent_z, 3),
        "sigma_threshold":  sigma_threshold,
        "beta":             beta,
    }
