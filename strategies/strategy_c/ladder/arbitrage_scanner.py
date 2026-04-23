"""
Model-free ladder no-arbitrage scanner for Strategy C2.

Detects three violation classes on Kalshi strike-ladder snapshots:
  1. Monotonicity  — P(S > K_low) < P(S > K_high) violates CDF ordering
  2. Convexity     — positive butterfly value indicates K2 is underpriced
  3. Bounds        — p outside [0,1] indicates degenerate book conditions

All three violation types emit ArbitrageSignal objects.
Bounds violations are logged but never traded (no recommended_legs).
A signal is only emitted when theoretical_profit_per_contract exceeds the
2-leg fee hurdle: 2 × taker_fee_rate × avg_leg_price + safety_margin.
"""
from __future__ import annotations
import logging
import os
import sys
import pandas as pd

_SHARED_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "shared")
)
_STRATEGIES_DIR = os.path.normpath(os.path.join(_SHARED_DIR, ".."))
if _STRATEGIES_DIR not in sys.path:
    sys.path.insert(0, _STRATEGIES_DIR)

from shared.types import ArbitrageSignal  # noqa: E402

logger = logging.getLogger(__name__)


def _load_fees(config: dict) -> tuple[float, float]:
    """Return (taker_fee_rate, safety_margin) from fees config embedded in asset config."""
    fees = config.get("fees", {})
    taker = float(fees.get("kalshi", {}).get("taker_fee_rate", 0.03))
    margin = float(fees.get("safety_margin", 0.005))
    return taker, margin


class LadderArbitrageScanner:
    """
    Scans a Kalshi strike-ladder snapshot for no-arbitrage violations.

    All tolerance and fee parameters come from config; no magic numbers in code.
    """

    def scan(self, ladder_df: pd.DataFrame, config: dict) -> list[ArbitrageSignal]:
        """
        Scan ladder_df for arbitrage violations.

        Args:
            ladder_df: output of parse_ladder() — must have 'strike' and
                       'implied_probability' columns, sorted by strike ascending
            config:    asset config dict; uses keys:
                         c2_scanner.monotonicity_tolerance
                         c2_scanner.convexity_tolerance
                         c2_scanner.bounds_epsilon
                       and fees (if present; otherwise uses defaults)

        Returns:
            List of ArbitrageSignal objects with theoretical_profit > fee hurdle.
            Sub-hurdle violations are logged but not returned.
        """
        if ladder_df.empty or "implied_probability" not in ladder_df.columns:
            return []

        scanner_cfg = config.get("c2_scanner", {}) or {}
        mono_tol = float(scanner_cfg.get("monotonicity_tolerance", 0.002))
        conv_tol = float(scanner_cfg.get("convexity_tolerance", 0.002))
        bounds_eps = float(scanner_cfg.get("bounds_epsilon", 0.005))

        taker, margin = _load_fees(config)

        df = ladder_df.sort_values("strike").reset_index(drop=True)
        strikes = df["strike"].tolist()
        probs = df["implied_probability"].tolist()

        signals: list[ArbitrageSignal] = []
        sub_hurdle_count = 0

        # 1. Bounds violations
        for i, (k, p) in enumerate(zip(strikes, probs)):
            if p > 1.0 - bounds_eps:
                logger.warning(
                    "Bounds violation (near-1) at strike=%.2f, p=%.4f; degenerate — skip.", k, p
                )
            elif p < bounds_eps:
                logger.warning(
                    "Bounds violation (near-0) at strike=%.2f, p=%.4f; degenerate — skip.", k, p
                )

        # 2. Monotonicity violations
        for i in range(len(strikes) - 1):
            k_low, k_high = strikes[i], strikes[i + 1]
            p_low, p_high = probs[i], probs[i + 1]
            # P(S > K_low) must be >= P(S > K_high)
            violation = p_high - p_low
            if violation > mono_tol:
                # p_high is overpriced, p_low is underpriced
                avg_price = (p_low + p_high) / 2.0
                fee_hurdle = 2.0 * taker * avg_price + margin
                gross_profit = violation - mono_tol
                if gross_profit > fee_hurdle:
                    signals.append(ArbitrageSignal(
                        violation_type="monotonicity",
                        strikes_involved=[k_low, k_high],
                        recommended_legs=[
                            (k_low, "yes", 1),   # buy underpriced low-strike YES
                            (k_high, "no", 1),   # buy overpriced high-strike NO (= sell YES)
                        ],
                        theoretical_profit_per_contract=gross_profit - fee_hurdle,
                        timestamp=pd.Timestamp.now("UTC"),
                    ))
                else:
                    sub_hurdle_count += 1
                    logger.debug(
                        "Sub-hurdle monotonicity violation: K_low=%.2f, K_high=%.2f, "
                        "violation=%.4f, hurdle=%.4f",
                        k_low, k_high, violation, fee_hurdle,
                    )

        # 3. Convexity violations (butterfly)
        for i in range(len(strikes) - 2):
            k1, k2, k3 = strikes[i], strikes[i + 1], strikes[i + 2]
            p1, p2, p3 = probs[i], probs[i + 1], probs[i + 2]
            butterfly = p1 - 2.0 * p2 + p3
            # Violation when butterfly > conv_tol (p2 is abnormally low vs. outer strikes)
            if butterfly > conv_tol:
                avg_price = (p1 + 2.0 * p2 + p3) / 4.0
                fee_hurdle = 2.0 * taker * avg_price + margin
                gross_profit = butterfly - conv_tol
                if gross_profit > fee_hurdle:
                    signals.append(ArbitrageSignal(
                        violation_type="convexity",
                        strikes_involved=[k1, k2, k3],
                        recommended_legs=[
                            (k1, "no", 1),   # sell K1 YES (outer, overpriced)
                            (k2, "yes", 2),  # buy K2 YES × 2 (middle, underpriced)
                            (k3, "no", 1),   # sell K3 YES (outer, overpriced)
                        ],
                        theoretical_profit_per_contract=gross_profit - fee_hurdle,
                        timestamp=pd.Timestamp.now("UTC"),
                    ))
                else:
                    sub_hurdle_count += 1
                    logger.debug(
                        "Sub-hurdle convexity violation: K1=%.2f K2=%.2f K3=%.2f "
                        "butterfly=%.4f hurdle=%.4f",
                        k1, k2, k3, butterfly, fee_hurdle,
                    )

        if sub_hurdle_count:
            logger.debug(
                "%d sub-hurdle violation(s) detected but not emitted.", sub_hurdle_count
            )

        return signals
