from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd


@dataclass
class FeatureVector:
    """All feature module outputs. Call to_flat_dict() before feeding to model."""
    har_rv: dict[str, float] = field(default_factory=dict)
    order_flow: dict[str, float] = field(default_factory=dict)
    time_of_day: dict[str, float] = field(default_factory=dict)
    cross_asset: dict[str, float] = field(default_factory=dict)
    funding: dict[str, float] = field(default_factory=dict)

    def to_flat_dict(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for module in ("har_rv", "order_flow", "time_of_day", "cross_asset", "funding"):
            for k, v in getattr(self, module).items():
                out[f"{module}__{k}"] = v
        return out


@dataclass
class Signal:
    """Output of Strategy A's trade decision."""
    timestamp: pd.Timestamp
    asset: str
    p_model: float        # calibrated P(up at 15-min expiry) in [0, 1]
    p_market: float       # Kalshi YES price in [0, 1] (not cents)
    edge: float           # p_model - p_market; positive → buy YES
    regime: str
    side: str             # "YES" or "NO"
    strategy: str = "strategy_a"


@dataclass
class DislocationSignal:
    """Output of Strategy B's dislocation detector."""
    timestamp: pd.Timestamp
    asset: str
    direction: str        # "fade_up" or "fade_down"
    confidence: float     # [0, 1] scaled by residual magnitude
    side: str             # "YES" or "NO" (the recommended trade side)
    residual_magnitude: float   # |actual_move - implied_move| in cents
    staleness_timestamp: pd.Timestamp   # signal expires after this time


@dataclass
class JumpFlag:
    """A statistically detected price jump from BV-based heuristic."""
    timestamp: pd.Timestamp
    is_jump: bool
    magnitude: float      # |J| = max(RV - BV, 0)
    direction: str        # "up" or "down"
