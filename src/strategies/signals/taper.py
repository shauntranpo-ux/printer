from __future__ import annotations
import math


def magnitude_taper(baseline: float) -> float:
    """
    Scale factor for signal adjustments based on how far baseline is from 0.5.
    Peaks at 1.0 when baseline=0.5; approaches 0 at 0 or 1.
    Prevents fixed-magnitude signals from dominating near-settled markets.
    """
    clamped = max(0.0, min(1.0, baseline))
    return 2.0 * math.sqrt(clamped * (1.0 - clamped))
