"""
bot_strategies.py - registry for the test-slot strategies (the paper lab).

S1 (momentum, the main line) and S2 (favorite-bias) keep their dedicated dispatch and
execution paths in bot_loops/bot_risk. Everything in STRATEGY_REGISTRY is an S3+ lab
slot: evaluated on every market alongside S1/S2, executed through the generic slot
executor in bot_risk (hard-forced paper), settled on the same expiry detection, and
ranked on the Edge-tab leaderboard. Adding S7/S8 later = one brain function + one entry
here; labels, stats, API, dashboard, and Telegram all pick it up from this dict.
"""
import bot_state
from bot_strategy import (
    strategy_brain_s3_arb,
    strategy_brain_s4_revert,
    strategy_brain_s5_maker,
    strategy_brain_s6_carry,
)

# Labels for ALL strategies (single source; bot_stats/server/dashboard read these).
STRATEGY_LABELS = {
    "strategy1": "S1 · Momentum",
    "strategy2": "S2 · Favorite-Bias",
    "strategy3": "S3 · Structural Arb",
    "strategy4": "S4 · Mean-Reversion",
    "strategy5": "S5 · Maker Capture",
    "strategy6": "S6 · Window-Carry",
}
STRATEGY_SHORT = {k: f"S{k[-1]}" for k in STRATEGY_LABELS}

# Test-slot registry (S3+ only - S1/S2 have dedicated paths).
STRATEGY_REGISTRY = {
    "strategy3": {
        "brain": strategy_brain_s3_arb,
        "tag": "s3",
        "enabled_key": "s3_arb_enabled",
        "version": bot_state._SLOT_VERSIONS["strategy3"],
        "max_pending_per_asset": 1,
    },
    "strategy4": {
        "brain": strategy_brain_s4_revert,
        "tag": "s4",
        "enabled_key": "s4_revert_enabled",
        "version": bot_state._SLOT_VERSIONS["strategy4"],
        "max_pending_per_asset": 1,
    },
    "strategy5": {
        "brain": strategy_brain_s5_maker,
        "tag": "s5",
        "enabled_key": "s5_maker_enabled",
        "version": bot_state._SLOT_VERSIONS["strategy5"],
        "max_pending_per_asset": 1,
    },
    "strategy6": {
        "brain": strategy_brain_s6_carry,
        "tag": "s6",
        "enabled_key": "s6_carry_enabled",
        "version": bot_state._SLOT_VERSIONS["strategy6"],
        "max_pending_per_asset": 1,
    },
}


def enabled_slots(config: dict) -> list:
    """Slot ids enabled in config (all default on - they are paper-only)."""
    return [slot for slot, meta in STRATEGY_REGISTRY.items()
            if config.get(meta["enabled_key"], True)]
