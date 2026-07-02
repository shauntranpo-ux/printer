# Section 4: ETH Strategy - Research Audit

## Problem Statement

The legacy bot applies the same BTC BV3 empirical table to ETH, which is wrong.
ETH has distinct intraday microstructure. This document summarizes the four
evidence-based components of the new ETHStrategy.

---

## Component 1: Concurrent BTC Beta (Primary Signal)

**Basis:** Kurihara & Matsumoto (2026), *Applied Finance & Markets*, Springer
open-access. 1-min Binance data, four 2024-2025 regimes. Key finding: ETH
reacts to BTC at **lag 0** on 1-minute returns - co-movement is synchronous,
not lagged.

**Implementation:** Compute BTC's 3-minute log return from the running
`bot.btc_prices` deque. Scale by ETH/BTC beta (default 1.10, refit weekly
from historical parquets). Normalize by expected remaining move = `vol * sqrt(t)`.
Max contribution: +/-10 percentage points on p_yes.

**Beta source:** OLS regression of aligned ETH vs BTC 1-min log returns over
full 2026 history. Persisted to `data/betas.json`, stale after 14 days.

---

## Component 2: Variance-Ratio Regime Detector

**Basis:** Lo & MacKinlay (1988) variance-ratio test, applied to crypto by
Wen, Bouri, Xu & Zhao (2022), *North American Journal of Economics and Finance*.
Tested on ETH specifically. Finding: intraday momentum and reversal coexist,
regime-dependent. VR(5) on 1-min returns over 60-minute windows distinguishes.

**Implementation:** Compute `VR(5) = Var(5-min return) / (5 * Var(1-min return))`
over the last 60 minutes of ETH 1-min returns.
- VR > 1.1 -> momentum -> continuation nudge (+/-3pp)
- VR < 0.9 -> reversion -> contrarian nudge (∓3pp)
- Otherwise -> neutral

---

## Component 3: ETH/BTC Ratio Divergence (30% Weight)

**Basis:** Classic cross-market statistical arbitrage. When a ratio deviates
from rolling mean without news, it tends to revert (pairs trading literature).

**Implementation:** Compute z-score of current ETH/BTC ratio vs 4-hour rolling
mean/std from aligned price deques. z > 1: ETH overpriced -> p_yes down.
z < -1: ETH underpriced -> p_yes up. Max contribution: +/-3pp (clipped at |z|=3).

---

## Component 4: Kalshi Contract Velocity

**Basis:** Informed-flow proxy. Professional market participants reprice fast
when they have new information. Rapid YES price increases signal informed buying.

**Implementation:** 30-sample lookback on `kalshi_price_history`. Threshold 2%.
Rising -> +2pp on p_yes. Falling -> -2pp.

---

## Intentional Deviations from Legacy

1. No BV3 table - ETH uses physics (Brownian bridge) + evidence adjustments
2. Bidirectional by default - no continuation-only override for ETH
3. Per-asset `min_ev_base: 8` (vs BTC's legacy `min_ev_base: 5`)
