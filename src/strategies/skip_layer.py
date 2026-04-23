"""
Explicit pre-strategy skip rules. Returns skip reason or None (proceed).

Runs BEFORE any strategy logic. If this returns a reason, we never call the
strategy's decide() method for this window.
"""

from __future__ import annotations

import time
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
    vol_ratio_threshold: float = 1.80           # base threshold: skip if expected_move/buffer >= this
    vol_confirm_mult: float = 1.25              # relax threshold by this when momentum confirms trade
    vol_oppose_mult: float = 0.70               # tighten threshold by this when momentum opposes trade
    vol_top_pct_threshold: float = 0.95         # legacy field, unused
    vol_bot_pct_threshold: float = 0.05         # legacy field, unused
    macro_event_skip_minutes: float = 15.0      # skip within +/- this of macro event
    mom_lock_enabled: bool = True               # skip when momentum opposes best-EV side
    mom_lock_neutral_tighten: float = 1.0       # tighten vol threshold by this mult when momentum is neutral
    mom_accel_scale: float = 3.0               # multiplier for acceleration probability adjustment


def _momentum_acceleration(prices, window_seconds: int = 180) -> tuple[float, str]:
    """
    Second derivative of price: how much is momentum changing?

    Compares the most recent `window_seconds` momentum against the prior
    equal-length window. Returns (acceleration, label):
      acceleration: pct-point difference (recent_mom - prior_mom)
      label: "accelerating" | "decelerating" | "flat"
    """
    if not prices:
        return 0.0, "flat"
    now = time.time()
    cutoff_recent = now - window_seconds
    cutoff_prior  = now - window_seconds * 2

    p_prior  = next((p for ts, p in prices if ts >= cutoff_prior),  None)
    p_mid    = next((p for ts, p in prices if ts >= cutoff_recent), None)
    p_now    = list(prices)[-1][1]

    if p_prior is None or p_mid is None or p_prior <= 0 or p_mid <= 0:
        return 0.0, "flat"

    mom_recent = (p_now - p_mid)   / p_mid
    mom_prior  = (p_mid - p_prior) / p_prior
    accel = mom_recent - mom_prior

    THRESHOLD = 0.002
    if accel > THRESHOLD:
        return accel, "accelerating"
    if accel < -THRESHOLD:
        return accel, "decelerating"
    return accel, "flat"


def _momentum_label(prices, window_seconds: int = 180) -> str:
    """3-min momentum label from a (timestamp, price) deque."""
    if not prices:
        return "neutral"
    now = time.time()
    cutoff = now - window_seconds
    oldest = None
    for ts, price in prices:
        if ts >= cutoff:
            oldest = price
            break
    entries = list(prices)
    if oldest is None or not entries:
        return "neutral"
    current = entries[-1][1]
    if oldest == 0:
        return "neutral"
    pct = (current - oldest) / oldest
    if pct > 0.005:
        return "bullish"
    if pct < -0.005:
        return "bearish"
    return "neutral"


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

    # Vol gate — need realized_vol computed
    if features.realized_vol_1min is None:
        return "realized_vol not yet computed"

    # Spread check — we check both sides; the EV layer will pick the better one.
    # Skip only if BOTH sides have blown-out spreads.
    if features.spread_yes > cfg.max_spread_cents and features.spread_no > cfg.max_spread_cents:
        return f"spread too wide: yes={features.spread_yes:.0f}c no={features.spread_no:.0f}c"

    rv = features.realized_vol_1min
    if rv < 0.0001:
        return f"realized_vol too low: {rv:.4f}/min (market may be halted)"

    # Buffer durability gate with momentum-adjusted threshold.
    # vol_ratio = (rv * sqrt(mins_left)) / buffer_pct
    # Skip when ratio is too high (buffer too thin relative to expected move).
    # Momentum confirmation relaxes the threshold; opposition tightens it.
    if features.current_price > 0 and features.strike > 0 and rv > 0:
        abs_pct = abs(features.current_price - features.strike) / features.current_price
        mins_left = features.seconds_left / 60.0
        if abs_pct > 0 and mins_left > 0:
            vol_ratio      = rv * (mins_left ** 0.5) / abs_pct
            buf_durability = (abs_pct / rv) ** 2

            above = features.current_price > features.strike
            mom   = _momentum_label(features.prices_60m)
            mom_confirms = (mom == "bullish" and above) or (mom == "bearish" and not above)
            mom_opposes  = (mom == "bullish" and not above) or (mom == "bearish" and above)

            if mom_confirms:
                eff_thresh = cfg.vol_ratio_threshold * cfg.vol_confirm_mult
            elif mom_opposes:
                eff_thresh = cfg.vol_ratio_threshold * cfg.vol_oppose_mult
            else:
                eff_thresh = cfg.vol_ratio_threshold * cfg.mom_lock_neutral_tighten

            if vol_ratio >= eff_thresh:
                align = ("confirms/relaxed" if mom_confirms else
                         "opposes/tightened" if mom_opposes else
                         "neutral/tightened" if cfg.mom_lock_neutral_tighten < 1.0 else "neutral")
                return (
                    f"buffer_too_thin: vol_ratio={vol_ratio:.2f} >= {eff_thresh:.2f} "
                    f"(dist={abs_pct*100:.2f}% dur={buf_durability:.1f}min "
                    f"mom={mom}/{align})"
                )

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

    At 80c+ entries the Kalshi fee drag exceeds any realistic edge — even a 97% WR
    strategy at 85c yields ~0c net after fees, and 100c entries are pure fee drag.
    Returns a skip reason if the cap is violated, else None.
    """
    if entry_cents >= cfg.max_entry_price_cents:
        return (
            f"price_cap: {side}_ask={entry_cents:.0f}c "
            f">= {cfg.max_entry_price_cents:.0f}c (fee drag)"
        )
    return None
