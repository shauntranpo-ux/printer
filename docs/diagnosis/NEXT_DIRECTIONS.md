# Next Directions

**Date:** 2026-05-07
**Context:** 15-minute crypto binary strategies (S1, S2) confirmed dead by 2024-2026 OOS holdout.
No recommendations below - options only.

---

## Hourly Kalshi Markets (Strategy C)

The `strategies/strategy_c/` scaffolding exists and `enable_hourly_markets` is a config key.
Hourly markets run on a different window structure: the strike is set at market open, settlement
is 60 minutes later. The BV3 continuation logic may behave differently at the hourly scale -
price persistence over 60 minutes has a different character than 15-minute momentum. The
existing `bv3_calibrator.py` and `fetch_kalshi_settlements.py` could be adapted to generate
hourly settlement records if Kalshi offers a comparable hourly series (e.g. `KXBTC1H`). Edge
existence would need the same OOS holdout discipline: generate synthetic settlements from the
1m price bars back to 2024, run the cell analysis, require multi-regime survival before any
live deployment.

---

## Last-Minute Gamma Exploration

A separate hypothesis: Kalshi YES/NO prices in the final 60-90 seconds of a 15-minute window
may exhibit predictable momentum - a binary option approaching expiry has convex theta, and
the order book may show systematic mispricing as market makers hedge off risk. This is
distinct from the BV3 continuation signal (which looks at distance from strike across the
full 1-13 minute window). A targeted backtest would require tick-level order book snapshots
from the final 90 seconds of each window, which are not in the current `data/historical/`
directory. No infrastructure exists for this today; it would need a new data collection layer.

---

## Market Making

An entirely different game. Rather than taking directional positions, a market maker posts
resting YES and NO limit orders simultaneously and earns the bid-ask spread from both sides.
Profitability depends on spread width, fill rate, adverse selection (informed traders picking
off the MM), and inventory risk (unbalanced positions as expiry approaches). Kalshi allows
resting limit orders. The existing codebase has no MM infrastructure - order management,
delta hedging, and inventory control would all need to be built from scratch. The adversarial
dynamics are completely different from directional trading and are not a revision of the current
approach; they are a different product.

---

## Pivot to Non-Crypto Kalshi Markets

Kalshi offers markets on economic data (CPI, Fed rate decisions, unemployment), weather,
elections, and sports. These markets have different liquidity profiles, longer time horizons,
and signal sources orthogonal to price momentum. A CPI prediction market might be tradeable
using economic forecasting models that outperform consensus; an election market might be
tradeable during inefficient early-pricing windows. The current codebase is entirely
crypto-specific - price bars, Kalshi crypto series tickers, BV3 distance-from-strike logic.
A pivot would require a new data pipeline, a new feature set, and different OOS validation
methodology (event-based rather than time-sliced). The only reusable infrastructure would be
the Kalshi API authentication layer and the DB schema for recording trades.
