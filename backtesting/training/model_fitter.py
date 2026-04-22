"""
Strategy A model fitter.

Fits a calibrated classifier and saves weights and calibrator to disk
for loading via StrategyAModel.load().

strategies/ is READ-ONLY — we import from it but never modify it.
"""
from __future__ import annotations
import os
import sys
import pickle
import numpy as np
import yaml


def _ensure_strategies_importable() -> None:
    strategies_path = os.path.abspath("strategies")
    if strategies_path not in sys.path:
        sys.path.insert(0, strategies_path)


def fit_and_save(
    X: np.ndarray,
    y: np.ndarray,
    feature_names: list[str],
    asset: str,
    model_config: dict,
    fees_config: dict,
    output_dir: str = "backtesting/output/models",
    refit: bool = True,
) -> tuple[str, str]:
    """
    Fit a calibrated classifier and save to disk.

    Returns:
        (weights_path, calibrator_path)
        weights_path:    pickle of feature_names list
        calibrator_path: pickle of the fitted CalibratedClassifierCV object
    """
    _ensure_strategies_importable()
    from strategy_a.model import StrategyAModel  # strategies/ source root

    os.makedirs(output_dir, exist_ok=True)
    weights_path    = os.path.join(output_dir, f"{asset.lower()}_feature_names.pkl")
    calibrator_path = os.path.join(output_dir, f"{asset.lower()}_calibrated_model.pkl")

    if not refit and os.path.exists(weights_path) and os.path.exists(calibrator_path):
        return weights_path, calibrator_path

    model = StrategyAModel(model_config, fees_config)
    model.fit(X, y, feature_names)

    with open(weights_path, "wb") as f:
        pickle.dump(model._feature_names, f)
    with open(calibrator_path, "wb") as f:
        pickle.dump(model._model, f)

    return weights_path, calibrator_path


def load_model(
    asset: str,
    model_config: dict,
    fees_config: dict,
    output_dir: str = "backtesting/output/models",
):
    """Load a pre-fitted StrategyAModel from disk."""
    _ensure_strategies_importable()
    from strategy_a.model import StrategyAModel  # noqa

    weights_path    = (model_config.get("model", {}).get("weights_path")
                       or os.path.join(output_dir, f"{asset.lower()}_feature_names.pkl"))
    calibrator_path = (model_config.get("model", {}).get("calibrator_path")
                       or os.path.join(output_dir, f"{asset.lower()}_calibrated_model.pkl"))

    model = StrategyAModel(model_config, fees_config)
    model.load(weights_path=weights_path, calibrator_path=calibrator_path)
    return model
