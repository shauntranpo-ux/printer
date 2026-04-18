"""
ETHStrategy — evidence-based strategy for ETH 15-min binaries.

Components:
1. Concurrent BTC beta adjustment (primary) — beta-scaled BTC 3-min return
   nudges the baseline toward the side BTC is implying.
2. Variance-ratio regime detector on ETH 1-min returns.
   VR > 1.1: momentum, continuation nudge.
   VR < 0.9: reversion, contrarian nudge.
3. ETH/BTC ratio divergence (30% weight) — z-score of current ratio vs
   4-hour rolling mean; positive z = ETH overpriced → p_yes down.
4. Kalshi contract velocity — informed-flow proxy; rising YES price → +p_yes.

All adjustments are bounded so combined p_yes stays in [0.05, 0.95] and no
single signal can flip the baseline sign. BaseStrategy handles calibration,
EV, and bidirectional side selection.
"""

from __future__ import annotations
import math
from typing import Optional

from strategies.base import BaseStrategy
from strategies.features import MarketFeatures
from strategies.skip_layer import SkipConfig
from strategies.calibration import AssetCalibrator

from strategies.signals.rolling_beta import log_returns_from_prices
from strategies.signals.variance_ratio import variance_ratio, variance_ratio_to_regime
from strategies.signals.ratio_divergence import ratio_z_score
from strategies.signals.kalshi_velocity import contract_velocity
from strategies.signals.beta_cache import load_beta
from strategies.signals.btc_context import three_min_return


BETA_ADJ_MAX   = 0.10   # max contribution from BTC-beta signal
REGIME_ADJ     = 0.03   # momentum / reversion regime nudge
RATIO_ADJ_MAX  = 0.03   # max contribution from ratio divergence
VELOCITY_ADJ   = 0.02   # Kalshi velocity nudge


class ETHStrategy(BaseStrategy):
    def __init__(
        self,
        skip_config: SkipConfig,
        min_ev: float,
        stake_dollars: float,
        calibrator: Optional[AssetCalibrator] = None,
        maker: bool = False,
    ):
        super().__init__(
            asset="ETH",
            skip_config=skip_config,
            min_ev=min_ev,
            stake_dollars=stake_dollars,
            calibrator=calibrator,
            maker=maker,
        )
        self.beta = load_beta("ETH")

    def compute_raw_p_model(
        self,
        features: MarketFeatures,
        baseline_p_above: float,
    ) -> tuple[float, dict]:
        signals = {}

        p_yes = baseline_p_above
        above = features.current_price > features.strike

        # ── Component 1: concurrent BTC beta ────────────────────────────
        btc_3m = three_min_return(features.btc_prices_60m)

        beta_adj = 0.0
        if btc_3m is not None:
            implied_eth_move = self.beta * btc_3m
            rv = features.realized_vol_1min or 0.002
            expected_remaining_move = rv * math.sqrt(max(1.0, features.seconds_left / 60.0))
            if expected_remaining_move > 0:
                nudge = (implied_eth_move / expected_remaining_move) * BETA_ADJ_MAX
                beta_adj = max(-BETA_ADJ_MAX, min(BETA_ADJ_MAX, nudge))
        signals["btc_3m_return"] = btc_3m
        signals["beta"] = self.beta
        signals["beta_adj"] = beta_adj
        p_yes += beta_adj

        # ── Component 2: variance-ratio regime ──────────────────────────
        eth_returns = log_returns_from_prices(list(features.prices_60m))
        vr = variance_ratio(eth_returns, q=5)
        regime = variance_ratio_to_regime(vr)

        regime_adj = 0.0
        if regime == "momentum":
            regime_adj = +REGIME_ADJ if above else -REGIME_ADJ
        elif regime == "reversion":
            regime_adj = -REGIME_ADJ if above else +REGIME_ADJ
        signals["variance_ratio"] = vr
        signals["regime"] = regime
        signals["regime_adj"] = regime_adj
        p_yes += regime_adj

        # ── Component 3: ETH/BTC ratio divergence ───────────────────────
        z = ratio_z_score(
            list(features.prices_60m),
            list(features.btc_prices_60m),
            lookback_minutes=240,
        )

        ratio_adj = 0.0
        if z is not None:
            z_clip = max(-3.0, min(3.0, z))
            ratio_adj = -z_clip * (RATIO_ADJ_MAX / 3.0)
        signals["ratio_z"] = z
        signals["ratio_adj"] = ratio_adj
        p_yes += ratio_adj

        # ── Component 4: Kalshi contract velocity ───────────────────────
        velocity = contract_velocity(
            list(features.kalshi_price_history),
            lookback_samples=30,
            threshold_pct=0.02,
        )
        velocity_adj = 0.0
        if velocity == "rising":
            velocity_adj = +VELOCITY_ADJ
        elif velocity == "falling":
            velocity_adj = -VELOCITY_ADJ
        signals["velocity"] = velocity
        signals["velocity_adj"] = velocity_adj
        p_yes += velocity_adj

        # Final clamp
        p_yes = max(0.05, min(0.95, p_yes))

        signals["final_p_yes"] = p_yes
        signals["baseline_p_above"] = baseline_p_above
        return p_yes, signals

