"""
Per-asset probability calibration.

Starts as identity (no calibration) until we have >= 50 resolved trades per
asset. Then fits isotonic regression mapping raw p_model values to empirical
win rates.

Calibration models are persisted to disk so they survive restarts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np
from sklearn.isotonic import IsotonicRegression


CALIBRATION_DIR = Path("data/calibration")
MIN_SAMPLES_FOR_FIT = 50


class AssetCalibrator:
    """One calibrator per asset. Identity until fit."""

    def __init__(self, asset: str):
        self.asset = asset
        self.model: Optional[IsotonicRegression] = None
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
            if state.get("fitted") and "raw_probs" in state and "outcomes" in state:
                self.model = IsotonicRegression(out_of_bounds="clip")
                self.model.fit(
                    np.array(state["raw_probs"]),
                    np.array(state["outcomes"]),
                )
                self.sample_count = len(state["raw_probs"])
        except (json.JSONDecodeError, KeyError, ValueError):
            # Corrupt state, start fresh
            self.model = None
            self.sample_count = 0

    def calibrate(self, raw_p: float) -> float:
        """Apply calibration if fitted, else return raw_p (identity)."""
        if self.model is None:
            return raw_p
        calibrated = float(self.model.predict([raw_p])[0])
        return max(0.001, min(0.999, calibrated))

    def refit(self, raw_probs: list, outcomes: list) -> None:
        """
        Refit calibration from full history.

        Args:
            raw_probs: list of model probabilities (floats 0-1)
            outcomes: list of 0/1 outcomes (int or bool)
        """
        if len(raw_probs) < MIN_SAMPLES_FOR_FIT:
            return
        if len(raw_probs) != len(outcomes):
            raise ValueError("raw_probs and outcomes length mismatch")

        X = np.array(raw_probs)
        y = np.array(outcomes, dtype=float)

        self.model = IsotonicRegression(out_of_bounds="clip")
        self.model.fit(X, y)
        self.sample_count = len(raw_probs)

        # Persist raw data for future refits
        state = {
            "asset": self.asset,
            "fitted": True,
            "sample_count": self.sample_count,
            "raw_probs": list(map(float, raw_probs)),
            "outcomes": list(map(int, outcomes)),
        }
        with open(self._state_path(), "w") as f:
            json.dump(state, f, indent=2)
