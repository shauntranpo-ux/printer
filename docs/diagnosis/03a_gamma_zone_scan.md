# Step 3A: Gamma Zone Edge Scan

**Date:** 2026-05-07
**Verdict:** **NO EDGE - zero cells survive all criteria**

---

## Hypothesis

S1 (PRINTER_BRAIN/BV3) continuation signal may have edge in the deep
ITM/OTM late-window zone where BV3 win probability approaches 1.0.
In this zone the strategy sells residual volatility at expiry - betting
that a 1%+ price gap from strike will NOT reverse in the final 1-3 minutes.

---

## Fee Structure (confirmed from src/strategies/fees.py)

Taker fee = `ceil(0.07 x contracts x price x (1 - price))`

| Entry price | Fee per contract | Net profit if WIN | Breakeven WR |
|-------------|-----------------|-------------------|-------------|
| 0.90 (90c) | $0.01 | $0.090 | 90.1% |
| 0.93 (93c) | $0.01 | $0.060 | 93.1% |
| 0.95 (95c) | $0.01 | $0.040 | 95.0% |
| 0.97 (97c) | $0.01 | $0.020 | 97.0% |
| 0.99 (99c) | $0.01 | $0.000 | 99.0% |

**Critical constraint:** At >=97c entry, fee alone consumes all profit.
Net profit-if-win becomes zero or negative even at 100% win rate.
Viable gamma zone trades require entry price <= 95c.

---

## Fill Risk Assessment

Available orderbook data: hourly Kalshi ladder snapshots (`data/kalshi/hourly/BTC/`),
April 2026 only. No 15-minute ladder data exists for any time period.

Hourly ladder data shows `yes_ask = 1.00` (100c) for deep-ITM strikes - the
market maker quotes no meaningful depth below the face value for contracts
priced above ~97c. For 15-minute markets in the final 1-3 minutes with
1%+ distance from strike, the continuation-side contract would similarly
face ask prices at 99-100c with minimal fill availability.

All cells with avg_entry > 95c are flagged FILL_RISK.

---

## Full Cell Table (n >= 100)

Entry price proxy: BV3 prediction (conservative, assumes market efficiency).
EV formula: empirical_WR x net_win + (1-empirical_WR) x net_loss.

| Asset | Dist | T | n | Win% | Avg entry | Fee | EV | Sharpe | Gap | Fill Risk |
|-------|------|---|---|------|----------|-----|-----|--------|-----|-----------|
| BTC | 1.0-1.5% | 1 | 484 | 100.0% | 0.99 | $0.01 | +0.00% | 0.000 | +0.00% | YES |
| BTC | 1.0-1.5% | 2 | 494 | 100.0% | 0.99 | $0.01 | +0.00% | 0.000 | -0.30% | YES |
| BTC | 1.0-1.5% | 3 | 440 | 100.0% | 0.99 | $0.01 | +0.00% | 0.000 | -0.50% | YES |
| BTC | 1.5-2.0% | 1 | 125 | 100.0% | 0.99 | $0.01 | +0.00% | 0.000 | +0.00% | YES |
| BTC | 1.5-2.0% | 2 | 101 | 100.0% | 0.99 | $0.01 | +0.00% | 0.000 | -0.30% | YES |
| ETH | 1.0-1.5% | 1 | 1178 | 100.0% | 0.99 | $0.01 | +0.00% | 0.000 | +0.00% | YES |
| ETH | 1.0-1.5% | 2 | 1077 | 100.0% | 0.99 | $0.01 | +0.00% | 0.000 | -0.30% | YES |
| ETH | 1.5-2.0% | 1 | 288 | 100.0% | 0.99 | $0.01 | +0.00% | 0.000 | +0.00% | YES |
| ETH | 1.5-2.0% | 2 | 274 | 100.0% | 0.99 | $0.01 | +0.00% | 0.000 | -0.30% | YES |
| ETH | 1.5-2.0% | 3 | 266 | 100.0% | 0.99 | $0.01 | +0.00% | 0.000 | -0.50% | YES |
| ETH | 2.0-3.0% | 1 | 163 | 100.0% | 0.99 | $0.01 | +0.00% | 0.000 | +0.00% | YES |
| ETH | 2.0-3.0% | 2 | 140 | 100.0% | 0.99 | $0.01 | +0.00% | 0.000 | -0.30% | YES |
| ETH | 2.0-3.0% | 3 | 120 | 100.0% | 0.99 | $0.01 | +0.00% | 0.000 | -0.50% | YES |
| SOL | 1.0-1.5% | 1 | 2166 | 100.0% | 0.99 | $0.01 | +0.00% | 0.000 | +0.00% | YES |
| SOL | 1.5-2.0% | 1 | 594 | 100.0% | 0.99 | $0.01 | +0.00% | 0.000 | +0.00% | YES |
| SOL | 1.5-2.0% | 2 | 564 | 100.0% | 0.99 | $0.01 | +0.00% | 0.000 | -0.30% | YES |
| SOL | 2.0-3.0% | 1 | 278 | 100.0% | 0.99 | $0.01 | +0.00% | 0.000 | +0.00% | YES |
| SOL | 2.0-3.0% | 2 | 256 | 100.0% | 0.99 | $0.01 | +0.00% | 0.000 | -0.30% | YES |
| SOL | 2.0-3.0% | 3 | 236 | 100.0% | 0.99 | $0.01 | +0.00% | 0.000 | -0.50% | YES |
| XRP | 1.5-2.0% | 1 | 583 | 100.0% | 0.99 | $0.01 | +0.00% | 0.000 | +0.00% | YES |
| XRP | 1.5-2.0% | 2 | 585 | 100.0% | 0.99 | $0.01 | +0.00% | 0.000 | -0.30% | YES |
| XRP | 2.0-3.0% | 1 | 352 | 100.0% | 0.99 | $0.01 | +0.00% | 0.000 | +0.00% | YES |
| XRP | 2.0-3.0% | 2 | 305 | 100.0% | 0.99 | $0.01 | +0.00% | 0.000 | -0.30% | YES |
| XRP | 2.0-3.0% | 3 | 305 | 100.0% | 0.99 | $0.01 | +0.00% | 0.000 | -0.50% | YES |
| XRP | 3.0%+ | 1 | 148 | 100.0% | 0.99 | $0.01 | +0.00% | 0.000 | +0.00% | YES |
| XRP | 3.0%+ | 3 | 119 | 100.0% | 0.99 | $0.01 | +0.00% | 0.000 | -0.50% | YES |
| SOL | 1.0-1.5% | 2 | 2078 | 99.9% | 0.99 | $0.01 | -0.10% | -0.031 | -0.20% | YES |
| ETH | 1.0-1.5% | 3 | 1018 | 99.9% | 0.99 | $0.01 | -0.10% | -0.031 | -0.40% | YES |
| XRP | 1.0-1.5% | 1 | 1952 | 99.9% | 0.99 | $0.01 | -0.10% | -0.032 | +0.10% | YES |
| SOL | 1.0-1.5% | 3 | 1930 | 99.9% | 0.99 | $0.01 | -0.10% | -0.032 | -0.40% | YES |
| XRP | 1.0-1.5% | 2 | 1851 | 99.9% | 0.99 | $0.01 | -0.11% | -0.033 | -0.19% | YES |
| SOL | 1.5-2.0% | 3 | 521 | 99.8% | 0.99 | $0.01 | -0.19% | -0.044 | -0.31% | YES |
| XRP | 1.0-1.5% | 3 | 1787 | 99.7% | 0.99 | $0.01 | -0.28% | -0.053 | -0.22% | YES |
| XRP | 1.5-2.0% | 3 | 526 | 99.4% | 0.99 | $0.01 | -0.57% | -0.076 | +0.07% | YES |
| XRP | 3.0%+ | 2 | 148 | 99.3% | 0.99 | $0.01 | -0.68% | -0.082 | +0.38% | YES |

---

## Holdout + Tail Risk Results (preliminary survivors only)

No preliminary survivors qualified for holdout analysis.

---

## Verdict

**Zero cells survive all criteria.**

The gamma zone has no extractable edge. Root causes:

1. **Fee structure kills thin margins.** At entry prices above 95c,
   the minimum $0.01 fee per contract consumes the entire profit-if-win.
   Net profit at p=0.99 is zero or negative regardless of win rate.

2. **Market efficiency.** The continuation-side contract IS priced near
   the true win probability by Kalshi's market makers. The BV3-based
   entry price proxy reflects what the market would charge. There is no
   systematic underpricing in the gamma zone.

3. **Tail risk is structural.** Even if a cell showed positive mean EV,
   selling deep-ITM vol exposes the strategy to catastrophic losses on
   the rare fast reversal. The asymmetry (win $0.01-0.05, lose $0.95-0.99)
   requires >95% win rate sustained across all market regimes.

This is the final empirical entry for the 15-minute BTC binary strategy.
Both the directional zone (Steps 2D-2H) and the gamma zone (Step 3A)
show no edge in 2024-2026 OOS data. See NEXT_DIRECTIONS.md for options.
