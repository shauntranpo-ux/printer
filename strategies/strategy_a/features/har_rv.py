from __future__ import annotations
"""
HAR-RS-J volatility forecaster (Patton & Sheppard 2015) adapted for 15-minute horizons.

Model: σ̂² = const
         + β_rv+_15m·RV+_15m + β_rv-_15m·RV-_15m
         + β_rv+_1h·RV+_1h   + β_rv-_1h·RV-_1h
         + β_rv+_4h·RV+_4h   + β_rv-_4h·RV-_4h
         + β_J·J_15m

Crypto asymmetry: RV+ and RV- have *separate* coefficients. In crypto, positive
semivariance is a stronger predictor of future variance than negative semivariance
(opposite of equities). The model never constrains β_rv+_15m == β_rv-_15m.

Output: σ̂ (not σ̂²) in log-return space, suitable as a 15-minute vol forecast.
"""
import numpy as np
from collections import deque


class HARRSJForecaster:
    def __init__(self, config: dict) -> None:
        gran = int(config["returns"]["granularity_seconds"])
        scales_min: list[int] = config["har_rs_j"]["timescales_minutes"]
        self._scale_names = ["15m", "1h", "4h"]
        self._n_bars = [int(m * 60 / gran) for m in scales_min]
        max_bars = max(self._n_bars)
        self._buf: deque[float] = deque(maxlen=max_bars)
        self._coef: dict = config["har_rs_j"]["coefficients"]

    # ── public interface ──────────────────────────────────────────────────────

    def update(self, new_bar: dict) -> None:
        """Append one bar. Expects {'log_return': float}."""
        r = new_bar.get("log_return")
        if r is not None:
            self._buf.append(float(r))

    def fit(self, returns_array: np.ndarray) -> None:
        """Batch-load historical returns into the ring buffer."""
        self._buf.clear()
        self._buf.extend(returns_array[-self._buf.maxlen:].tolist())

    def compute(self, data_window=None) -> dict[str, float]:
        """
        Compute all HAR-RS-J features plus σ̂ forecast.
        data_window: optional sequence of log-returns (float or dict with 'log_return')
                     appended before computation.
        """
        if data_window is not None:
            if isinstance(data_window, np.ndarray):
                self._buf.extend(data_window.tolist())
            else:
                for item in data_window:
                    if isinstance(item, dict):
                        self.update(item)
                    else:
                        self._buf.append(float(item))

        arr = np.array(list(self._buf), dtype=np.float64)
        out: dict[str, float] = {}
        for name, n in zip(self._scale_names, self._n_bars):
            window = arr[-n:] if len(arr) >= n else arr
            for k, v in self._rv_components(window).items():
                out[f"{name}_{k}"] = v
        out["sigma_forecast"] = self._forecast(out)
        return out

    # ── internals ─────────────────────────────────────────────────────────────

    @staticmethod
    def _rv_components(r: np.ndarray) -> dict[str, float]:
        if r.size == 0:
            return {k: 0.0 for k in ("rv", "rv_pos", "rv_neg", "bv", "jump", "signed_jump")}
        sq = r ** 2
        rv      = float(sq.sum())
        rv_pos  = float(sq[r > 0].sum())
        rv_neg  = float(sq[r < 0].sum())
        # BV = (π/2) · Σ|r_t|·|r_{t-1}| — scaled to match RV units
        bv = float((np.pi / 2) * (np.abs(r[1:]) * np.abs(r[:-1])).sum()) if r.size > 1 else rv
        jump         = max(rv - bv, 0.0)
        signed_jump  = rv_pos - rv_neg
        return {"rv": rv, "rv_pos": rv_pos, "rv_neg": rv_neg,
                "bv": bv, "jump": jump, "signed_jump": signed_jump}

    def _forecast(self, feats: dict[str, float]) -> float:
        c = self._coef
        if any(v is None for v in c.values()):
            # Untrained model: proxy σ̂ ≈ √RV_15m (no-drift realized vol)
            return float(np.sqrt(max(feats.get("15m_rv", 0.0), 0.0)))
        sigma_sq = (
            c["const"]
            + c["rv_15m_pos"] * feats["15m_rv_pos"]
            + c["rv_15m_neg"] * feats["15m_rv_neg"]
            + c["rv_1h_pos"]  * feats["1h_rv_pos"]
            + c["rv_1h_neg"]  * feats["1h_rv_neg"]
            + c["rv_4h_pos"]  * feats["4h_rv_pos"]
            + c["rv_4h_neg"]  * feats["4h_rv_neg"]
            + c["jump"]       * feats["15m_jump"]
        )
        return float(np.sqrt(max(sigma_sq, 0.0)))
