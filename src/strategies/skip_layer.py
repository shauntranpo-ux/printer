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
    min_entry_price_cents: float = 35.0         # skip markets where either side costs below this floor
    max_entry_price_cents: float = 80.0         # skip trade if the side we'd buy is at or above this ceiling (fee drag)
    cold_start_samples: int = 60                # need this many prices_60m samples
    vol_ratio_threshold: float = 1.80           # skip if expected_move/buffer >= this
    vol_top_pct_threshold: float = 0.95         # legacy field, unused
    vol_bot_pct_threshold: float = 0.05         # legacy field, unused
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

    # Deep-OTM filter: both sides must have at least one tradeable contract above
    # the minimum price. Deep-OTM contracts (<35c) have 0% observed win rate
    # because the model's signal adjustments are too small to overcome market pricing.
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

    # Spread check â€” we check both sides; the EV layer will pick the better one.
    # Skip only if BOTH sides have blown-out spreads.
    if features.spread_yes > cfg.max_spread_cents and features.spread_no > cfg.max_spread_cents:
        return f"spread too wide: yes={features.spread_yes:.0f}c no={features.spread_no:.0f}c"

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


def check_entry_price_cap(entry_cents: float, side: str, cfg: SkipConfig) -> Optional[str]:
    """Post-decision guard: reject trades at or above cfg.max_entry_price_cents.

    At 80c+ entries the Kalshi fee drag exceeds any realistic edge â€” even a 97% WR
    strategy at 85c yields ~0c net after fees, and 100c entries are pure fee drag.
    Returns a skip reason if the cap is violated, else None.
    """
    if entry_cents >= cfg.max_entry_price_cents:
        return (
            f"price_cap: {side}_ask={entry_cents:.0f}c "
            f">= {cfg.max_entry_price_cents:.0f}c (fee drag)"
        )
    return None


def check_skip_15m(
    features: MarketFeatures,
    min_price_cents: float = 35.0,
) -> Optional[str]:
    """
    Minimal pre-EV gate for 15-minute markets.

    15m always trades the continuation side (expensive side = max of yes/no ask):
    above strike buys YES, below strike buys NO. The floor applies to the
    expensive side only — a cheap YES when below strike is irrelevant.
    The 76c ceiling is enforced post-EV by check_entry_price_cap().
    All other hourly gates are intentionally absent for 15m markets.
    """
    expensive = max(features.yes_ask, features.no_ask)
    if expensive < min_price_cents:
        return (
            f"price_floor_15m: max_ask={expensive:.0f}c "
            f"below {min_price_cents:.0f}c floor"
        )
    return None


def check_vol_ratio(features: MarketFeatures, cfg: SkipConfig) -> Optional[str]:
    """
    Buffer durability gate: skip when expected move is too large relative to
    strike distance (buffer too thin). Short-duration markets (< 20 min) bypass
    this check because 15m strikes are placed near ATM, making the ratio explode
    even in mild volatility.

    Called from BaseStrategy.decide() as step 5 of the pipeline (after EV gate).
    """
    if features.seconds_left < 20 * 60:
        return None  # 15m markets: bypass

    rv = features.realized_vol_1min
    if rv is None or rv < 0.0001:
        return None  # no vol data — skip the check, not the trade

    if features.current_price <= 0 or features.strike <= 0:
        return None

    abs_pct = abs(features.current_price - features.strike) / features.current_price
    mins_left = features.seconds_left / 60.0
    if abs_pct <= 0 or mins_left <= 0:
        return None

    vol_ratio = rv * (mins_left ** 0.5) / abs_pct
    buf_durability = (abs_pct / rv) ** 2
    if vol_ratio >= cfg.vol_ratio_threshold:
        return (
            f"buffer_too_thin: vol_ratio={vol_ratio:.2f} >= {cfg.vol_ratio_threshold:.2f} "
            f"(dist={abs_pct*100:.2f}% dur={buf_durability:.1f}min)"
        )
    return None
