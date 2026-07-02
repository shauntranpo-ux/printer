from __future__ import annotations
"""
Strategy A probability combiner.

Label definition (canonical):
  y = 1  if underlying CLOSE at t+15min > OPEN at t=0
  y = 0  otherwise
  Reference price source (spot vs perp) is specified per-asset in config.

Fee threshold derivation (see should_trade docstring):
  The Kalshi taker fee formula is ceil(0.07*C*p*(1-p)) per contract.
  At p=0.50 and C~50 contracts on $25 stake ~ $0.875 fee ~ 3.5%.
  fees.yaml stores a conservative flat-rate approximation (0.03) for threshold
  computation. The actual order-placement layer uses the exact formula in
  src/strategies/fees.py. Do not change this approximation without also updating
  the execution layer's EV computation.
"""
import numpy as np
from typing import Optional


class StrategyAModel:
    """
    Calibrated probability model for Strategy A.
    Primary classifier: logistic regression. Swappable to XGBoost/LightGBM via config.
    Calibration: isotonic regression (default) or Platt scaling.
    """

    def __init__(self, config: dict, fees_config: dict) -> None:
        self.config = config
        self.fees = fees_config
        self._model = None
        self._feature_names: Optional[list[str]] = None
        self._fitted = False

    # ── interface contract (locked - backtester and executor depend on these) ─

    def predict_proba(self, features: dict) -> float:
        """Calibrated P(up at 15-min expiry) in [0, 1]. Returns 0.5 if unfitted."""
        if not self._fitted or self._model is None:
            return 0.5
        vec = self._to_vec(features)
        return float(np.clip(self._model.predict_proba([vec])[0][1], 0.0, 1.0))

    def get_edge(self, p_model: float, p_market: float) -> float:
        """
        Signed edge: p_model - p_market (both in [0, 1]).
        Positive -> YES has edge. Negative -> NO has edge.
        p_market = Kalshi YES price / 100 (i.e. 70c -> 0.70).
        """
        return p_model - p_market

    def should_trade(
        self,
        p_model: float,
        p_market: float,
        regime: str,
        config: dict,
        btc_degraded: bool = False,
    ) -> bool:
        """
        Returns True when |edge| > min_edge.

        min_edge derivation:
          taker_fee_approx  = fees_config["kalshi"]["taker_fee_rate"]  (0.03 flat)
          safety_margin     = fees_config["safety_margin"]              (0.005)
          regime_extra      = config["thresholds"]["edge_above_fee"][regime]
                              (null -> 0.02 default when untuned)
          min_edge = taker_fee_approx + safety_margin + regime_extra
          If btc_degraded: min_edge += config["thresholds"]["btc_degraded_penalty"]
        """
        taker    = float(self.fees["kalshi"]["taker_fee_rate"])
        margin   = float(self.fees["safety_margin"])
        thresholds = config.get("thresholds", {}).get("edge_above_fee", {})
        raw = thresholds.get(regime)
        regime_extra = float(raw) if raw is not None else 0.02
        min_edge = taker + margin + regime_extra
        if btc_degraded:
            penalty = float(config.get("thresholds", {}).get("btc_degraded_penalty", 0.01))
            min_edge += penalty
        return abs(self.get_edge(p_model, p_market)) > min_edge

    # training

    def fit(self, X: np.ndarray, y: np.ndarray, feature_names: list[str]) -> None:
        """
        Train the calibrated classifier.
        X: (n_samples, n_features)
        y: binary labels (1 = underlying up at 15-min expiry, 0 = down)
        """
        from sklearn.calibration import CalibratedClassifierCV
        self._feature_names = list(feature_names)
        mtype = self.config.get("model", {}).get("type", "logistic_regression")
        cal   = self.config.get("model", {}).get("calibration", "isotonic")
        method = "isotonic" if cal == "isotonic" else "sigmoid"
        base = self._build_base(mtype)
        self._model = CalibratedClassifierCV(base, cv=5, method=method)
        self._model.fit(X, y)
        self._fitted = True

    def load(self, weights_path: str | None = None, calibrator_path: str | None = None) -> None:
        """
        Load a pre-trained calibrated model and feature-name list from disk.

        Paths are resolved from arguments first, then from config["model"] keys
        "weights_path" and "calibrator_path".
        """
        import pickle
        wp = weights_path or self.config.get("model", {}).get("weights_path")
        cp = calibrator_path or self.config.get("model", {}).get("calibrator_path")
        if wp is None or cp is None:
            raise ValueError("weights_path and calibrator_path must be provided or set in config")
        with open(wp, "rb") as f:
            self._feature_names = pickle.load(f)
        with open(cp, "rb") as f:
            self._model = pickle.load(f)
        self._fitted = True

    @staticmethod
    def _build_base(mtype: str):
        if mtype == "xgboost":
            from xgboost import XGBClassifier
            return XGBClassifier(n_estimators=100, eval_metric="logloss")
        if mtype == "lightgbm":
            from lightgbm import LGBMClassifier
            return LGBMClassifier(n_estimators=100, verbosity=-1)
        from sklearn.linear_model import LogisticRegression
        return LogisticRegression(max_iter=1000, C=1.0)

    # helpers

    def _to_vec(self, features: dict) -> np.ndarray:
        if self._feature_names is None:
            raise RuntimeError("feature_names not set; call fit() or load() first")
        return np.array([features.get(k, 0.0) for k in self._feature_names], dtype=float)
