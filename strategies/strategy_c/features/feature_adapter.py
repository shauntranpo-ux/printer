"""
Thin re-export wrappers around reusable feature modules from strategy_a.

Does NOT copy code.  Adds strategy_a's parent directory to sys.path so
imports resolve, then re-exports the public classes/functions.

Strategy C does NOT use the order_flow module (disabled codebase-wide).
"""
from __future__ import annotations
import os
import sys

_STRATEGIES_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)
if _STRATEGIES_DIR not in sys.path:
    sys.path.insert(0, _STRATEGIES_DIR)

# strategy_a.features is available once _STRATEGIES_DIR (= .../strategies/) is on sys.path
# TODO: If the strategies/ directory is not on sys.path at import time (e.g. when run
#       outside the project root), set PYTHONPATH or call _ensure_on_path() explicitly.

from strategy_a.features.har_rv import HARRSJForecaster       # noqa: E402
from strategy_a.features.time_of_day import compute as compute_time_of_day   # noqa: E402
from strategy_a.features.cross_asset import compute as compute_cross_asset   # noqa: E402
from strategy_a.features.funding import FundingFeatures        # noqa: E402

__all__ = [
    "HARRSJForecaster",
    "compute_time_of_day",
    "compute_cross_asset",
    "FundingFeatures",
]
