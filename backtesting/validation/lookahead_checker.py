"""
Programmatic look-ahead verification.

For every feature value computed at time t, all source data timestamps
must be strictly less than t. Fails the run if any feature violates this.
"""
from __future__ import annotations
import pandas as pd
from dataclasses import dataclass


@dataclass
class LookaheadViolation:
    feature_name: str
    decision_timestamp: pd.Timestamp
    source_timestamp: pd.Timestamp
    delta_seconds: float


def check_no_lookahead(
    decisions: list[dict],
) -> list[LookaheadViolation]:
    """
    Check that every feature in each decision record was computed from
    data strictly before the decision timestamp.

    Each decision dict must have:
        "timestamp": pd.Timestamp — the decision time
        "feature_timestamps": dict[str, pd.Timestamp] — feature name → source data timestamp

    Returns a list of violations. Empty list means pass.
    """
    violations = []
    for decision in decisions:
        decision_ts = decision.get("timestamp")
        feature_timestamps = decision.get("feature_timestamps", {})
        if decision_ts is None:
            continue
        for feat_name, src_ts in feature_timestamps.items():
            if src_ts >= decision_ts:
                violations.append(LookaheadViolation(
                    feature_name=feat_name,
                    decision_timestamp=decision_ts,
                    source_timestamp=src_ts,
                    delta_seconds=float((src_ts - decision_ts).total_seconds()),
                ))
    return violations


def assert_no_lookahead(decisions: list[dict]) -> None:
    """Raises RuntimeError if any look-ahead is detected."""
    violations = check_no_lookahead(decisions)
    if violations:
        msgs = [
            f"  {v.feature_name}: source={v.source_timestamp} >= decision={v.decision_timestamp} "
            f"(+{v.delta_seconds:.1f}s)"
            for v in violations[:10]
        ]
        raise RuntimeError(
            f"LOOK-AHEAD DETECTED in {len(violations)} feature(s):\n" + "\n".join(msgs)
        )
