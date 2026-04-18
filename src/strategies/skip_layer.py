"""
Explicit pre-strategy skip rules. Returns skip reason or None (proceed).

Runs BEFORE any strategy logic. If this returns a reason, we never call the
strategy's decide() method for this window.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from strategies.features import MarketFeatures


@dataclass
class SkipConfig:
    max_spread_cents: float = 3.0               # max spread on the side we'd trade
    min_seconds_left: float = 30.0              # skip if under this
    cold_start_samples: int = 60                # need this many prices_60m samples
    vol_top_pct_threshold: float = 0.95         # skip if realized vol > 95th pct
    vol_bot_pct_threshold: float = 0.05         # skip if realized vol < 5th pct
    macro_event_skip_minutes: float = 15.0      # skip within +/- this of macro event


def check_skip(
    features: MarketFeatures,
    cfg: SkipConfig,
    macro_event_active: bool = False,
) -> Optional[str]:
    """
    Returns None if safe to proceed, else a reason string.
    """
    # Time remaining
    if features.seconds_left < cfg.min_seconds_left:
        return f"seconds_left={features.seconds_left:.0f} < {cfg.min_seconds_left}"

    # Macro event
    if macro_event_active:
        return f"within {cfg.macro_event_skip_minutes}min of macro event"

    # Cold start
    if len(features.prices_60m) < cfg.cold_start_samples:
        return f"cold_start: only {len(features.prices_60m)} samples (need {cfg.cold_start_samples})"

    # Vol gate — need realized_vol computed
    if features.realized_vol_1min is None:
        return "realized_vol not yet computed"

    # Spread check — we check both sides; the EV layer will pick the better one.
    # Skip only if BOTH sides have blown-out spreads.
    if features.spread_yes > cfg.max_spread_cents and features.spread_no > cfg.max_spread_cents:
        return f"spread too wide: yes={features.spread_yes:.0f}c no={features.spread_no:.0f}c"

    # Top/bottom vol check — requires vol history, which we don't have yet.
    # Placeholder: implement percentile-based vol gate in a later section when
    # we have rolling vol tracking. For now, absolute threshold:
    if features.realized_vol_1min > 0.01:  # 1% per minute = extreme
        return f"realized_vol too high: {features.realized_vol_1min:.4f}/min"
    if features.realized_vol_1min < 0.0001:  # 0.01% per minute = market halted?
        return f"realized_vol too low: {features.realized_vol_1min:.4f}/min"

    return None


def check_skip_with_asset_hook(
    features: "MarketFeatures",
    cfg: "SkipConfig",
    macro_event_active: bool = False,
    asset_hook_result: "Optional[tuple[bool, str]]" = None,
) -> "Optional[str]":
    """
    Same as check_skip but respects an optional asset-specific health hook.

    asset_hook_result: (is_healthy, reason)
        If is_healthy is False, returns the reason immediately (fail-safe).
    """
    if asset_hook_result is not None:
        is_healthy, reason = asset_hook_result
        if not is_healthy:
            return f"asset_hook: {reason}"
    return check_skip(features, cfg, macro_event_active)
