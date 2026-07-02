# Section 7.5: BTC Evidence-Based Strategy Research Audit

## The Problem with Legacy BTC

The legacy BTCStrategy (Sections 0-7) uses a BV3 empirical table lookup as
its primary signal. Live performance showed a 30-point miscalibration gap:

- Average model confidence: 78.6%
- Actual win rate: 48.3%
- Miscalibration: -30.3pp

A 30-point gap cannot be closed by regenerating BV3 tables (Section 8).
The gap is structural - "lookup by distance and time" ignores regime,
concurrent volatility, and order-flow signals that are actually predictive.

## Evidence Base for BTC Specifically

**Wen, Bouri, Xu & Zhao 2022 (NAJEF)**
Tested intraday momentum-reversal patterns on BTC high-frequency data
spanning 2013-2020. BTC has the strongest statistical evidence base of any
cryptocurrency for variance-ratio regime detection. VR(5) > 1.1 indicates
trend-following conditions; VR < 0.9 indicates mean-reversion. The paper
validated these signals specifically on BTC tick data. If any asset in this
universe deserves a VR regime signal, it's BTC.

**Grobys & Sapkota 2021 (IRFA)**
Cross-sectional momentum strategies on top-10 market cap cryptocurrencies
show significant excess returns. BTC is firmly in the large-cap group. The
momentum-biased prior is justified as a structural default for BTC.

**Cont, Kukanov, Stoikov (2014) and successors**
Order-flow imbalance is predictive at short horizons in both equity and
crypto markets. Kalshi contract velocity is a proxy for this: informed flow
shows up as directional pressure in contract prices before the underlying
settles. This signal is included at the same magnitude as ETH/SOL (+/-2pp).

## Why Brownian-Bridge + VR Regime + BV3-as-Secondary Beats Pure BV3

BV3 tells us: "historically, when BTC was X% above strike with T minutes
left, it stayed above strike Y% of the time." That's a coarse probability
anchor but it ignores:

- Is the market currently trending or mean-reverting? (VR regime)
- Is the BTC price move fresh or overextended? (momentum bias)
- Is informed flow building in the contract? (velocity)

The new structure uses Brownian-bridge physics as the anchor (what the price
path should look like under no information), then adjusts with signals that
have evidence behind them. BV3 is retained at 20% weight as a sanity check
because it still contains calibrated empirical information - just not enough
on its own.

## Rollback Path

The BTC_continuation_only flag is the escape hatch:

- `"BTC_continuation_only": false` (default) -> new evidence-based strategy
- `"BTC_continuation_only": true` -> reverts to legacy BV3 + momentum + velocity path

The legacy path is preserved verbatim in `_decide_legacy_fallback()`. The
A/B harness (scripts/section2_ab_harness.py) continues to validate parity.
If paper-trade results show BTC degrading below 48% hit rate, flip the flag
and investigate without redeploying.
