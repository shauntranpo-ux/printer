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
    min_entry_price_cents: float = 20.0         # entry range lower bound (inclusive)
    max_entry_price_cents: float = 80.0         # entry range upper bound (exclusive)
    cold_start_samples: int = 60                # need this many prices_60m samples
    vol_ratio_threshold: float = 1.80           # skip if expected_move/buffer >= this
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

    # Deep-OTM filter: both sides must price above the configured floor.
    # Below the floor the model's signal adjustments are too small to overcome
    # market pricing. Floor is cfg.min_entry_price_cents (default 20c).
    min_p = cfg.min_entry_price_cents
    if min(features.yes_ask, features.no_ask) < min_p:
        cheap_side = "yes" if features.yes_ask < features.no_ask else "no"
        cheap_price = min(features.yes_ask, features.no_ask)
        return (
            f"deep_otm: {cheap_side}_ask={cheap_price:.0f}c below {min_p:.0f}c floor"
        )

    # Macro event
    if macro_event_active:
        return f"within {cfg.macro_event_skip_minutes}min of macro event"

    # Cold start
    if len(features.prices_60m) < cfg.cold_start_samples:
        return f"cold_start: only {len(features.prices_60m)} samples (need {cfg.cold_start_samples})"

    # Spread check â€" we check both sides; the EV layer will pick the better one.
    # Skip only if BOTH sides have blown-out spreads.
    if features.spread_yes > cfg.max_spread_cents and features.spread_no > cfg.max_spread_cents:
        return f"spread too wide: yes={features.spread_yes:.0f}c no={features.spread_no:.0f}c"

    return None

def check_entry_range(entry_cents: float, side: str, cfg: SkipConfig) -> Optional[str]:
    """Post-decision range gate: entry must be within [min_entry_price_cents, max_entry_price_cents)."""
    if entry_cents < cfg.min_entry_price_cents:
        return (
            f"entry_range: {side}_ask={entry_cents:.0f}c "
            f"below {cfg.min_entry_price_cents:.0f}c"
        )
    if entry_cents >= cfg.max_entry_price_cents:
        return (
            f"entry_range: {side}_ask={entry_cents:.0f}c "
            f">= {cfg.max_entry_price_cents:.0f}c"
        )
    return None


def check_vol_ratio(features: MarketFeatures, cfg: SkipConfig) -> Optional[str]:
    """
    Buffer durability gate: skip when expected move is too large relative to
    strike distance (buffer too thin). Applies to all markets including 15m.

    Called from BaseStrategy.decide() as step 5 of the pipeline (after EV gate).
    """
    rv = features.realized_vol_1min
    if rv is None or rv < 0.0001:
        return None  # no vol data — skip the check, not the trade

    if features.current_price <= 0 or features.strike <= 0:
        return None

    abs_pct = abs(features.current_price - features.strike) / features.current_price
    abs_pct = max(abs_pct, 0.003)  # floor prevents division by near-zero at strike open
    mins_left = features.seconds_left / 60.0
    if mins_left <= 0:
        return None

    vol_ratio = rv * (mins_left ** 0.5) / abs_pct
    buf_durability = (abs_pct / rv) ** 2
    if vol_ratio >= cfg.vol_ratio_threshold:
        return (
            f"buffer_too_thin: vol_ratio={vol_ratio:.2f} >= {cfg.vol_ratio_threshold:.2f} "
            f"(dist={abs_pct*100:.2f}% dur={buf_durability:.1f}min)"
        )
    return None
