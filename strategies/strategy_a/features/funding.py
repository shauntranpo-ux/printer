from __future__ import annotations
import numpy as np
from collections import deque


class FundingFeatures:
    """
    Online z-score computation for funding rate and open interest.
    Consumes one observation per funding interval (typically every 8h on Binance/Bybit).
    OI may arrive more frequently (assumed up to 24 observations/day, sized accordingly).
    """

    def __init__(self, config: dict) -> None:
        fc = config["funding"]
        days = int(fc["zscore_window_days"])
        # 3 funding events/day (8h interval on Binance); OI assumed up to 24/day
        self._fr_buf: deque[float] = deque(maxlen=days * 3 + 5)
        self._oi_buf: deque[float] = deque(maxlen=days * 24 + 5)
        self._cl_thresh  = float(fc["crowded_long_threshold"])
        self._cs_thresh  = float(fc["crowded_short_threshold"])
        self._oi_thresh  = float(fc["oi_crowded_threshold"])
        if self._cs_thresh >= 0:
            raise ValueError(
                f"crowded_short_threshold must be negative, got {self._cs_thresh}"
            )

    @staticmethod
    def _zscore(buf: deque, val: float) -> float:
        if len(buf) < 2:
            return 0.0
        arr = np.array(list(buf))
        std = float(arr.std())
        return (val - float(arr.mean())) / std if std > 1e-14 else 0.0

    def compute(self, data_window: dict) -> dict[str, float]:
        """
        data_window keys:
          funding_rate: float   - latest funding rate (dimensionless; Binance ~0.0001)
          open_interest: float  - latest OI in base currency units
          timestamp: (optional; not consumed, kept for interface uniformity)
        """
        fr = float(data_window.get("funding_rate", 0.0) or 0.0)
        oi = float(data_window.get("open_interest", 0.0) or 0.0)
        # Score against prior window before appending (prevents self-contamination)
        fr_z = self._zscore(self._fr_buf, fr)
        oi_z = self._zscore(self._oi_buf, oi)
        self._fr_buf.append(fr)
        self._oi_buf.append(oi)
        return {
            "funding_rate_zscore": fr_z,
            "oi_zscore":           oi_z,
            "crowded_long":  1.0 if fr_z > self._cl_thresh and oi_z > self._oi_thresh else 0.0,
            "crowded_short": 1.0 if fr_z < self._cs_thresh and oi_z > self._oi_thresh else 0.0,
        }
