# Section 7: DOGE Strategy Research Audit

## Why DOGE is the Hardest Market

DOGE is a special case in this asset universe. The evidence for systematic
edge is the weakest of all five markets, but that makes the design choice
clear: don't extract alpha aggressively. Trade selectively and skip everything
ambiguous.

## Research Findings

**Grobys & Sapkota 2021 — Large-cap momentum**
Cross-sectional momentum strategies on cryptocurrencies with top-10 market
cap status show statistically significant excess returns. DOGE qualifies
under this regime. The momentum signal is the primary structural prior in
DOGEStrategy, but at reduced magnitude compared to SOL (0.015 vs 0.02)
because DOGE's momentum is less reliable than SOL's.

**Absence of DOGE-specific intraday studies**
No published academic work validates variance-ratio, volume-spike, or
intraday mean-reversion signals on DOGE specifically. ETH and SOL have
validated signals applied in their strategies. Applying unvalidated signal
logic to DOGE would be adding noise, not edge. DOGEStrategy explicitly
excludes these signals.

## Live Trade Data

DOGE bot performance under legacy strategy:
- Hit rate: 61.5% (best of all five markets)
- Average entry price: ~61.4¢
- Breakeven hit rate at 61.4¢ entry: ~63%
- Result: losing despite highest hit rate

The "best" performer was still below breakeven. The issue is entry prices,
not prediction quality. Higher Min EV (10% vs 8% for others) ensures we
only enter when the expected edge clears the bar after fees.

## Why DOGE is Hostile to Pure TA

Elon Musk tweets, Reddit posts, celebrity mentions, and random meme
accelerations move DOGE faster and more violently than any of the other four
markets. These moves are fundamentally unpredictable from price data alone.

Adverse selection on sentiment APIs: even if we integrated a sentiment API,
propagation delay is 30-120 seconds. By the time a signal fires, Kalshi
market makers have already repriced the contracts. We would be buying after
the informed flow, not with it.

## DOGEStrategy Design

The strategy is conservative by construction:

1. **BTC beta signal** — DOGE runs at ~1.3x BTC on correlated moves.
   Primary signal when DOGE is in beta-following mode.

2. **Momentum-biased prior** — Small magnitude (+0.015), always-on in
   the direction of the move. Large-cap default per Grobys & Sapkota.

3. **Kalshi velocity** — Informed-flow proxy (±0.02).

4. **Idiosyncratic mode detector** — When recent 15-min DOGE return
   diverges from beta * BTC by more than 2.5 sigma, DOGE is in meme/news
   mode. Hard skip. We have no edge in these windows.

5. **Session-aware Min EV** — Weekend and US afternoon (18-22 UTC) apply
   1.25x multiplier to Min EV, raising the bar from 10% to 12.5% in
   retail-heavy sessions.

6. **Higher base Min EV (10%)** — Set in config asset_overrides to reflect
   the reduced confidence in DOGE signals vs ETH/SOL/XRP.
