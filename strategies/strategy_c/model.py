"""
Strategy C1 combiner - per-strike probability mispricing engine.

StrategyC1Model wraps the probability surface evaluator with:
  - per-moneyness-bucket calibration loading
  - fee-aware, regime-aware, moneyness-aware threshold gating
  - candidate ranking for hand-off to the event selector

Calibrators are trained separately (see training prompt).  Until artifact_paths
are filled in config, raw N(d2) values pass through uncalibrated.
"""
from __future__ import annotations
import logging
import os
import pickle
import sys

import pandas as pd
import yaml

logger = logging.getLogger(__name__)

_STRATEGIES_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
if _STRATEGIES_DIR not in sys.path:
    sys.path.insert(0, _STRATEGIES_DIR)

from strategy_c.probability.probability_surface import ProbabilitySurface  # noqa: E402
from strategy_c.features.vol_term_structure import integrate_forecasted_variance  # noqa: E402


def _load_fees_yaml() -> dict:
    fees_path = os.path.join(_STRATEGIES_DIR, "shared", "fees.yaml")
    try:
        with open(fees_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("Could not load shared/fees.yaml: %s; using defaults.", exc)
        return {"kalshi": {"taker_fee_rate": 0.03, "maker_fee_rate": 0.00}, "safety_margin": 0.005}


class StrategyC1Model:
    """
    Per-strike probability mispricing engine for Kalshi hourly strike-ladder markets.

    BTC and ETH only.  Each asset gets its own fitted config; do not share models.
    """

    def __init__(self, config: dict) -> None:
        self._config = config
        self._fees = _load_fees_yaml()
        calibrators = self._load_calibrators(config)
        self._surface = ProbabilitySurface(calibrators=calibrators)

    # public interface

    def predict_surface(
        self,
        snapshot: dict,
        feature_vector: dict,
        config: dict,
    ) -> pd.DataFrame:
        """
        Parse the ladder snapshot and return the calibrated per-strike probability surface.

        Args:
            snapshot:       Kalshi ladder snapshot dict (see strike_ladder.parse_ladder)
            feature_vector: flat feature dict (reserved for calibration features)
            config:         asset config dict

        Returns:
            DataFrame with columns: strike, p_model, p_market, edge, moneyness_bucket
        """
        from strategy_c.features.strike_ladder import parse_ladder

        ladder_df = parse_ladder(snapshot)
        spot_price = float(snapshot["spot_price"])
        time_to_expiry_seconds = float(snapshot["time_to_expiry_seconds"])

        integrated_variance = self._get_integrated_variance(
            snapshot, feature_vector, config, time_to_expiry_seconds
        )

        mu_hat = 0.0
        if config.get("probability", {}).get("drift_adjustment_enabled", False):
            raw_weight = config.get("probability", {}).get("drift_adjustment_weight")
            if raw_weight is not None:
                jump_signal = feature_vector.get("har_rv__jump_15m", 0.0)
                mu_hat = float(raw_weight) * jump_signal

        return self._surface.evaluate(
            spot_price=spot_price,
            ladder_df=ladder_df,
            time_to_expiry_seconds=time_to_expiry_seconds,
            integrated_variance=integrated_variance,
            feature_vector=feature_vector,
            config=config,
            mu_hat=mu_hat,
        )

    def rank_candidates(
        self,
        surface_df: pd.DataFrame,
        config: dict,
    ) -> pd.DataFrame:
        """
        Filter surface_df to tradeable strikes and sort by |edge| descending.

        A strike is tradeable when should_trade_strike() returns True.
        Adds a 'side' column: 'yes' if edge > 0, 'no' if edge < 0.

        Returns empty DataFrame if no strikes pass the threshold.
        """
        if surface_df.empty:
            return surface_df.copy()

        from shared.regime_filters import get_current_regime as classify_session
        import pandas as pd

        regime = config.get("_runtime_regime", "us_afternoon")

        tradeable = []
        for _, row in surface_df.iterrows():
            edge = float(row["edge"])
            bucket = str(row["moneyness_bucket"])
            if self.should_trade_strike(edge, bucket, regime, config):
                r = row.to_dict()
                r["side"] = "yes" if edge > 0 else "no"
                tradeable.append(r)

        if not tradeable:
            return surface_df.iloc[0:0].copy()

        result = pd.DataFrame(tradeable)
        result["_abs_edge"] = result["edge"].abs()
        result = result.sort_values("_abs_edge", ascending=False).drop(columns=["_abs_edge"])
        return result.reset_index(drop=True)

    def should_trade_strike(
        self,
        edge: float,
        moneyness_bucket: str,
        regime: str,
        config: dict,
    ) -> bool:
        """
        Return True when |edge| exceeds the fee-aware, regime-aware, moneyness-aware threshold.

        Threshold math:
            base_threshold   = taker_fee_rate + safety_margin + base_regime_extra[regime]
                               (null regime entries default to 0.02)
            min_edge         = base_threshold x moneyness_multiplier[moneyness_bucket]
                               (null multiplier entries default to 1.0)
            If moneyness_bucket == "deep_otm" AND edge > 0 (buying longshot YES):
                min_edge    += longshot_buy_penalty  (null -> 0.02)

        Args:
            edge:             p_model - p_market; positive -> buy YES, negative -> buy NO
            moneyness_bucket: one of deep_itm | itm | atm | otm | deep_otm
            regime:           session regime string
            config:           asset config dict

        Returns:
            bool - True means the strike clears all thresholds.
        """
        taker = float(self._fees.get("kalshi", {}).get("taker_fee_rate", 0.03))
        margin = float(self._fees.get("safety_margin", 0.005))

        thresholds = config.get("thresholds", {}) or {}
        base_by_regime = thresholds.get("base_edge_above_fee", {}) or {}
        raw_regime_extra = base_by_regime.get(regime)
        regime_extra = float(raw_regime_extra) if raw_regime_extra is not None else 0.02

        mult_map = thresholds.get("moneyness_multiplier", {}) or {}
        raw_mult = mult_map.get(moneyness_bucket, 1.0)
        mult = float(raw_mult) if raw_mult is not None else 1.0

        min_edge = (taker + margin + regime_extra) * mult

        if moneyness_bucket == "deep_otm" and edge > 0:
            raw_penalty = thresholds.get("longshot_buy_penalty")
            penalty = float(raw_penalty) if raw_penalty is not None else 0.02
            min_edge += penalty

        return abs(edge) > min_edge

    # internals

    def _get_integrated_variance(
        self,
        snapshot: dict,
        feature_vector: dict,
        config: dict,
        time_to_expiry_seconds: float,
    ) -> float:
        """
        Build integrated_variance for the full [now, expiry] window.

        Uses vol_term_structure.integrate_forecasted_variance with the HAR
        forecaster's current sigma as a flat-vol per-sub-interval estimate.
        """
        import pandas as pd
        from strategy_c.features.vol_term_structure import integrate_forecasted_variance
        from shared.regime_filters import get_current_regime as classify_session

        ts_now = snapshot.get("timestamp_now")
        ts_expiry = snapshot.get("timestamp_expiry")

        if ts_now is None or ts_expiry is None:
            sigma_hat = float(
                config.get("volatility_reference", {}).get("annualized", 0.5)
            )
            t_years = time_to_expiry_seconds / (365.25 * 24.0 * 3600.0)
            return sigma_hat ** 2 * t_years

        ts_now = pd.Timestamp(ts_now, tz="UTC") if not isinstance(ts_now, pd.Timestamp) else ts_now
        ts_expiry = pd.Timestamp(ts_expiry, tz="UTC") if not isinstance(ts_expiry, pd.Timestamp) else ts_expiry

        sigma_hat = float(
            config.get("volatility_reference", {}).get("annualized", 0.5)
        )
        sub_min = float(
            config.get("vol_term_structure", {}).get("sub_interval_minutes", 15)
        )
        sub_seconds = sub_min * 60.0
        t_years_sub = sub_seconds / (365.25 * 24.0 * 3600.0)
        per_sub_variance = sigma_hat ** 2 * t_years_sub

        def _har_forecast(_ts):
            return per_sub_variance

        return integrate_forecasted_variance(
            har_forecast_fn=_har_forecast,
            timestamp_now=ts_now,
            timestamp_expiry=ts_expiry,
            regime_lookup_fn=classify_session,
            config=config,
        )

    @staticmethod
    def _load_calibrators(config: dict) -> dict:
        """Load per-bucket calibrators from disk; skip buckets with null paths."""
        cal_cfg = config.get("calibration", {}) or {}
        paths = cal_cfg.get("artifact_paths", {}) or {}
        calibrators: dict = {}
        for bucket, path in paths.items():
            if path is None:
                continue
            try:
                with open(path, "rb") as f:
                    calibrators[bucket] = pickle.load(f)
                logger.info("Loaded calibrator for bucket=%s from %s", bucket, path)
            except FileNotFoundError:
                logger.warning(
                    "Calibrator artifact not found for bucket=%s at path=%s; skipping.", bucket, path
                )
            except Exception as exc:
                logger.warning(
                    "Failed to load calibrator for bucket=%s: %s; skipping.", bucket, exc
                )
        return calibrators
