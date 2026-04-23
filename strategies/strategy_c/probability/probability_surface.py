"""
Probability surface evaluator for Strategy C1.

Evaluates P_model(S_T > K) for every strike on the Kalshi ladder, producing
a surface DataFrame with model probability, market-implied probability, and edge.

Calibration is applied per moneyness bucket (see model.py).  When no calibrators
are loaded, raw N(d₂) values are used.
"""
from __future__ import annotations
import math
import logging
import pandas as pd
import numpy as np

from strategy_c.probability.digital_call import binary_call_probability
from strategy_c.features.moneyness import compute_moneyness_features

logger = logging.getLogger(__name__)


class ProbabilitySurface:
    """
    Evaluates the model probability surface across all ladder strikes.

    Usage:
        surface = ProbabilitySurface(calibrators={})
        df = surface.evaluate(spot, ladder_df, tte_seconds, iv, feature_vector, config)
    """

    def __init__(self, calibrators: dict | None = None) -> None:
        """
        Args:
            calibrators: dict mapping moneyness_bucket -> fitted sklearn calibrator.
                         May be empty (no calibration applied until training is done).
        """
        self._calibrators: dict = calibrators or {}

    def evaluate(
        self,
        spot_price: float,
        ladder_df: pd.DataFrame,
        time_to_expiry_seconds: float,
        integrated_variance: float,
        feature_vector: dict,
        config: dict,
        mu_hat: float = 0.0,
    ) -> pd.DataFrame:
        """
        Evaluate P_model and edge for every strike in ladder_df.

        Args:
            spot_price:              current underlying spot price
            ladder_df:               output of parse_ladder(); must have 'strike' and
                                     'implied_probability' columns
            time_to_expiry_seconds:  T−t in seconds
            integrated_variance:     σ²·(T−t) from vol_term_structure.integrate_forecasted_variance
            feature_vector:          flat feature dict (reserved for future calibration features)
            config:                  asset config dict
            mu_hat:                  optional signed drift adjustment added to d₂'s numerator
                                     as mu_hat·(T−t); 0.0 = no adjustment

        Returns:
            DataFrame with columns: strike, p_model, p_market, edge, moneyness_bucket
            p_model is monotonically non-increasing with strike under GBM (before calibration).
        """
        if ladder_df.empty:
            return pd.DataFrame(columns=["strike", "p_model", "p_market", "edge", "moneyness_bucket"])

        sigma_hat = float(
            config.get("volatility_reference", {}).get("annualized", 0.5)
        )
        risk_free_rate = float(
            config.get("probability", {}).get("risk_free_rate", 0.0)
        )

        # Apply drift adjustment by shifting effective spot price
        if mu_hat != 0.0 and time_to_expiry_seconds > 0:
            adjusted_spot = spot_price * math.exp(mu_hat * time_to_expiry_seconds)
        else:
            adjusted_spot = spot_price

        records = []
        for _, row in ladder_df.iterrows():
            strike = float(row["strike"])
            p_mkt = float(row["implied_probability"])

            p_raw = binary_call_probability(
                adjusted_spot,
                strike,
                integrated_variance,
                time_to_expiry_seconds,
                risk_free_rate,
            )

            mono_feat = compute_moneyness_features(
                spot_price, strike, sigma_hat, time_to_expiry_seconds, config
            )
            bucket = mono_feat["moneyness_bucket"]

            # Apply per-bucket calibrator if available
            p_model = self._calibrate(p_raw, bucket)
            edge = p_model - p_mkt

            records.append({
                "strike": strike,
                "p_model": p_model,
                "p_market": p_mkt,
                "edge": edge,
                "moneyness_bucket": bucket,
            })

        return pd.DataFrame(records)

    def _calibrate(self, p_raw: float, bucket: str) -> float:
        """Apply bucket calibrator; return p_raw unchanged if no calibrator loaded."""
        cal = self._calibrators.get(bucket)
        if cal is None:
            return p_raw
        try:
            arr = np.array([[p_raw]])
            return float(cal.predict_proba(arr)[0, 1])
        except Exception as exc:
            logger.warning("Calibrator for bucket=%s failed (%s); using raw.", bucket, exc)
            return p_raw
