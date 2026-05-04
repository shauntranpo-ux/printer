"""
Computes expected value for both YES and NO sides and picks the better one.

EV_yes = p_model - yes_ask_dollars - (fee / stake)
EV_no  = (1 - p_model) - no_ask_dollars - (fee / stake)

Never derive p_no from p_yes. Always pull both orderbook asks independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from strategies.fees import taker_fee, maker_fee


@dataclass
class EVResult:
    best_side: Optional[Literal["yes", "no"]]  # None if neither side is viable
    best_ev: float                              # the higher of yes_ev / no_ev
    yes_ev: float
    no_ev: float
    yes_entry_price: float                      # dollars
    no_entry_price: float                       # dollars


def compute_bidirectional_ev(
    p_model: float,
    yes_ask_cents: float,
    no_ask_cents: float,
    stake_dollars: float,
    maker: bool = False,
) -> EVResult:
    """
    Compute EV for both sides independently.

    Args:
        p_model: strategy's probability that YES wins (0.0 to 1.0)
        yes_ask_cents: best YES ask price (0 to 100)
        no_ask_cents: best NO ask price (0 to 100)
        stake_dollars: target dollar stake
        maker: True if using maker fees, False for taker

    Returns:
        EVResult with best side, EVs, and entry prices.
        best_side is None if both sides have negative EV.
    """
    yes_price = yes_ask_cents / 100.0
    no_price = no_ask_cents / 100.0

    fee_fn = maker_fee if maker else taker_fee

    # Compute yes side
    if yes_price <= 0 or yes_price >= 1.0:
        yes_ev = -9.999
        yes_contracts = 0
        yes_stake_actual = 0.0
    else:
        yes_contracts = max(1, int(stake_dollars / yes_price))
        yes_fee = fee_fn(yes_contracts, yes_price)
        yes_stake_actual = yes_contracts * yes_price
        yes_ev = p_model - yes_price - (yes_fee / yes_stake_actual)

    # Compute no side
    if no_price <= 0 or no_price >= 1.0:
        no_ev = -9.999
        no_contracts = 0
        no_stake_actual = 0.0
    else:
        no_contracts = max(1, int(stake_dollars / no_price))
        no_fee = fee_fn(no_contracts, no_price)
        no_stake_actual = no_contracts * no_price
        no_ev = (1.0 - p_model) - no_price - (no_fee / no_stake_actual)

    # Pick better side
    if yes_ev >= no_ev:
        best = "yes"
        best_ev = yes_ev
    else:
        best = "no"
        best_ev = no_ev

    # If best side is still negative EV, caller should skip
    if best_ev <= 0:
        return EVResult(
            best_side=None,
            best_ev=best_ev,
            yes_ev=yes_ev,
            no_ev=no_ev,
            yes_entry_price=yes_price,
            no_entry_price=no_price,
        )

    return EVResult(
        best_side=best,
        best_ev=best_ev,
        yes_ev=yes_ev,
        no_ev=no_ev,
        yes_entry_price=yes_price,
        no_entry_price=no_price,
    )
