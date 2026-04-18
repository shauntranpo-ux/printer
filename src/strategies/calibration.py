"""
Per-asset probability calibration.

Auto-selects calibration method based on sample count:
  - n >= 50: isotonic regression (non-parametric, flexible)
  - 15 <= n < 50: Platt scaling (logistic regression, works on small samples)
  - n < 15: identity (pass raw probability through unchanged)

Calibration models are persisted to disk so they survive restarts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression


CALIBRATION_DIR = Path("data/calibration")
MIN_SAMPLES_FOR_ISOTONIC = 50
MIN_SAMPLES_FOR_PLATT = 15


class AssetCalibrator:
    """One calibrator per asset. Identity until fit."""

    def __init__(self, asset: str):
        self.asset = asset
        self._method: Optional[str] = None      # "isotonic", "platt", or None
        self._isotonic: Optional[IsotonicRegression] = None
        self._platt: Optional[LogisticRegression] = None
        self.sample_count: int = 0
        self._load_if_exists()

    def _state_path(self) -> Path:
        CALIBRATION_DIR.mkdir(parents=True, exist_ok=True)
        return CALIBRATION_DIR / f"{self.asset}_calibration.json"

    def _load_if_exists(self) -> None:
        path = self._state_path()
        if not path.exists():
            return
        try:
            with open(path, "r") as f:
                state = json.load(f)
            if not state.get("fitted"):
                return
            method = state.get("method")
            n = state.get("sample_count", 0)
            if method == "isotonic" and "raw_probs" in state and "outcomes" in state:
                self._isotonic = IsotonicRegression(out_of_bounds="clip")
                self._isotonic.fit(
                    np.array(state["raw_probs"]),
                    np.array(state["outcomes"]),
                )
                self._method = "isotonic"
                self.sample_count = n
            elif method == "platt" and "coef" in state and "intercept" in state:
                lr = LogisticRegression()
                lr.coef_ = np.array(state["coef"])
                lr.intercept_ = np.array(state["intercept"])
                lr.classes_ = np.array([0, 1])
                self._platt = lr
                self._method = "platt"
                self.sample_count = n
        except (json.JSONDecodeError, KeyError, ValueError):
            self._method = None
            self._isotonic = None
            self._platt = None
            self.sample_count = 0

    def calibrate(self, raw_p: float) -> float:
        """Apply calibration if fitted, else return raw_p (identity)."""
        if self._method == "isotonic" and self._isotonic is not None:
            calibrated = float(self._isotonic.predict([raw_p])[0])
            return max(0.001, min(0.999, calibrated))
        if self._method == "platt" and self._platt is not None:
            calibrated = float(self._platt.predict_proba([[raw_p]])[0][1])
            return max(0.001, min(0.999, calibrated))
        return raw_p

    def refit(self, raw_probs: list, outcomes: list) -> None:
        """
        Refit calibration from full history. Auto-selects method by sample count.

        Args:
            raw_probs: list of model probabilities (floats 0-1)
            outcomes: list of 0/1 outcomes (int or bool)
        """
        n = len(raw_probs)
        if n < MIN_SAMPLES_FOR_PLATT:
            return
        if n != len(outcomes):
            raise ValueError("raw_probs and outcomes length mismatch")

        X = np.array(raw_probs)
        y = np.array(outcomes, dtype=float)

        if n >= MIN_SAMPLES_FOR_ISOTONIC:
            self._isotonic = IsotonicRegression(out_of_bounds="clip")
            self._isotonic.fit(X, y)
            self._platt = None
            self._method = "isotonic"
            state = {
                "asset": self.asset,
                "fitted": True,
                "method": "isotonic",
                "sample_count": n,
                "raw_probs": list(map(float, raw_probs)),
                "outcomes": list(map(int, outcomes)),
            }
        else:
            lr = LogisticRegression()
            lr.fit(X.reshape(-1, 1), y)
            self._platt = lr
            self._isotonic = None
            self._method = "platt"
            state = {
                "asset": self.asset,
                "fitted": True,
                "method": "platt",
                "sample_count": n,
                "coef": lr.coef_.tolist(),
                "intercept": lr.intercept_.tolist(),
            }

        self.sample_count = n
        with open(self._state_path(), "w") as f:
            json.dump(state, f, indent=2)
