"""
Strategy C2 orchestrator — ladder no-arbitrage scanner.

Thin glue between parse_ladder() and LadderArbitrageScanner.
Logs violation diagnostics including sub-hurdle counts for monitoring.
"""
from __future__ import annotations
import logging
import os
import sys

import pandas as pd

logger = logging.getLogger(__name__)

_STRATEGIES_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
if _STRATEGIES_DIR not in sys.path:
    sys.path.insert(0, _STRATEGIES_DIR)

from strategy_c.features.strike_ladder import parse_ladder         # noqa: E402
from strategy_c.ladder.arbitrage_scanner import LadderArbitrageScanner  # noqa: E402
from shared.types import ArbitrageSignal                           # noqa: E402


class StrategyC2Scanner:
    """
    Orchestrates the C2 ladder no-arbitrage scan for one asset snapshot.

    Usage:
        scanner = StrategyC2Scanner(config)
        signals = scanner.scan_snapshot(snapshot)
    """

    def __init__(self, config: dict) -> None:
        self._config = config
        self._scanner = LadderArbitrageScanner()

    def scan_snapshot(self, snapshot: dict) -> list[ArbitrageSignal]:
        """
        Parse the snapshot and return tradeable ArbitrageSignal objects.

        Args:
            snapshot: Kalshi ladder snapshot dict (see strike_ladder.parse_ladder schema)

        Returns:
            List of ArbitrageSignal objects that clear the fee hurdle.
            Empty list when no violations are detected or hurdle not met.
        """
        try:
            ladder_df = parse_ladder(snapshot)
        except (ValueError, KeyError) as exc:
            logger.warning("Failed to parse ladder snapshot: %s; returning no signals.", exc)
            return []

        if ladder_df.empty:
            logger.debug("Empty ladder after parsing; no signals.")
            return []

        signals = self._scanner.scan(ladder_df, self._config)

        n_total = len(signals)
        if n_total:
            mono = sum(1 for s in signals if s.violation_type == "monotonicity")
            conv = sum(1 for s in signals if s.violation_type == "convexity")
            logger.info(
                "C2 scan: %d tradeable signal(s) — monotonicity=%d, convexity=%d",
                n_total, mono, conv,
            )
        else:
            logger.debug("C2 scan: no tradeable violations found.")

        return signals
