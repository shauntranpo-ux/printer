from __future__ import annotations
"""
Strategy B: Kalshi contract mean-reversion dislocation detector.

Mechanism:
  1. Compute implied_move = f(underlying_return, time_to_expiry, current_price)
     using Brownian-with-drift: translate the underlying log-return into a
     delta-P(up) via probability_utils.drift_vol_to_prob, then convert to cents.
  2. Compute actual_move = contract_mid_now - contract_mid_N_seconds_ago.
  3. residual = actual_move - implied_move.
  4. If |residual| > threshold → output a DislocationSignal to fade the move.

This module is completely independent from Strategy A: separate data, separate
classes, no shared state. Strategy A's HAR-RS-J sigma-hat can be injected via
update_vol() as a vol estimate, but the dislocation logic does not depend on it
(falls back to self._recent_sigma if not injected).
"""
import numpy as np
import pandas as pd
from typing import Optional

from shared.types import DislocationSignal
from shared.probability_utils import drift_vol_to_prob, contract_price_change_from_prob_change


class ContractDislocationDetector:
    def __init__(self, config: dict) -> None:
        dc = config["dislocation"]
        self._lookback_sec: int = int(dc["lookback_seconds"])
        raw_thresh = dc.get("residual_threshold")
        self._threshold: float = float(raw_thresh) if raw_thresh else 2.0
        self._staleness_sec: int = int(dc["signal_staleness_seconds"])
        self._asset: str = config["asset"]["symbol"]
        self._recent_sigma: float = 0.0  # uninitialized; update_vol() must be called before signals fire

    def update_vol(self, sigma_forecast: float) -> None:
        """Inject annualized sigma-hat from Strategy A's HAR-RS-J module."""
        if sigma_forecast > 0:
            self._recent_sigma = sigma_forecast

    def detect_dislocation(
        self,
        contract_stream: list[dict],
        underlying_stream: list[dict],
    ) -> Optional[DislocationSignal]:
        """
        contract_stream: list of Kalshi tick dicts, oldest-first. Each tick:
          {timestamp, yes_bid, yes_ask, no_bid, no_ask[, seconds_to_expiry]}
        underlying_stream: list of {timestamp, price}, oldest-first.
        Returns None if no dislocation is detected or data is insufficient.
        """
        if not contract_stream or not underlying_stream:
            return None
        if self._recent_sigma <= 0:
            return None  # vol estimate not yet injected via update_vol()

        now_ts = self._to_utc(contract_stream[-1]["timestamp"])
        cutoff = now_ts - pd.Timedelta(seconds=self._lookback_sec)

        old_ticks  = [t for t in contract_stream  if self._to_utc(t["timestamp"]) <= cutoff]
        old_prices = [p for p in underlying_stream if self._to_utc(p["timestamp"]) <= cutoff]

        if not old_ticks or not old_prices:
            return None

        old_mid = self._mid(old_ticks[-1])
        cur_mid = self._mid(contract_stream[-1])
        actual_move = cur_mid - old_mid  # cents

        old_px = float(old_prices[-1]["price"])
        cur_px = float(underlying_stream[-1]["price"])
        if old_px <= 0:
            return None

        log_ret = float(np.log(cur_px / old_px))
        tte = float(contract_stream[-1].get("seconds_to_expiry", 450))
        dp = self._implied_dp(log_ret, tte)
        implied_move = contract_price_change_from_prob_change(dp)

        residual = actual_move - implied_move

        if abs(residual) <= self._threshold:
            return None

        direction = "fade_up" if residual > 0 else "fade_down"
        side = "no" if residual > 0 else "yes"
        confidence = min(abs(residual) / (self._threshold * 3.0), 1.0)

        return DislocationSignal(
            timestamp=now_ts,
            asset=self._asset,
            direction=direction,
            confidence=confidence,
            side=side,
            residual_magnitude=abs(residual),
            staleness_timestamp=now_ts + pd.Timedelta(seconds=self._staleness_sec),
        )

    @staticmethod
    def _mid(tick: dict) -> float:
        return (float(tick.get("yes_bid", 0.0)) + float(tick.get("yes_ask", 100.0))) / 2.0

    @staticmethod
    def _to_utc(ts) -> pd.Timestamp:
        t = pd.Timestamp(ts)
        return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")

    def _implied_dp(self, log_return: float, time_to_expiry_sec: float) -> float:
        """ΔP(up) implied by the underlying log-return over the remaining horizon.

        Annualizes log_return over the lookback window (not TTE), then projects
        the forward probability using the remaining time-to-expiry.
        """
        if time_to_expiry_sec <= 0:
            return 0.0
        dt_lookback_years = self._lookback_sec / (365.0 * 24.0 * 3600.0)
        dt_tte_years = time_to_expiry_sec / (365.0 * 24.0 * 3600.0)
        p_before = drift_vol_to_prob(0.0, self._recent_sigma, dt_tte_years)
        mu_annual = log_return / dt_lookback_years
        p_after = drift_vol_to_prob(mu_annual, self._recent_sigma, dt_tte_years)
        return p_after - p_before
