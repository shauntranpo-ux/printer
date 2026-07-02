# Section 6: XRP Strategy Research Audit

## Why XRP is Different

XRP is the outlier in this bot's asset universe. Three independent research
streams and live trading data converge on the same finding: treating XRP as
a BTC-follower loses money.

## Research Findings

**Ozaydin 2021 - VAR Granger causality on BTC → alts**
Tested whether BTC daily returns Granger-cause ETH, ADA, BNB, and XRP.
Found significant BTC → alt relationships for three of those assets. XRP
was the exception - no significant Granger causality from BTC to XRP at
daily level. XRP's price dynamics are not driven by BTC momentum.

**Verma et al. 2022 - Random walk and predictability tests**
Applied unit-root and runs tests to BTC, ETH, LTC, USDT, and XRP. Strongly
rejected the random walk hypothesis for XRP specifically. XRP is predictable,
but the predictability comes from its own internal dynamics, not from BTC.

**Wen, Bouri, Xu & Zhao 2022 (NAJEF) - Intraday momentum-reversal**
Found that intraday momentum-reversal patterns hold for XRP, consistent
with findings for BTC and ETH. XRP is predictable from its own recent returns
on short intraday horizons - this is the basis for the variance-ratio regime
signal in XRPStrategy.

## Live Trade Data

XRP bot performance under BTC-coupled strategy:
- YES side hit rate: 41.7%
- NO side hit rate: 62.5%

The systematic YES-side underperformance is exactly what you'd expect if
the strategy assumes XRP follows BTC's upside momentum when it doesn't.
When BTC was rising and the bot went YES, XRP was decoupled and the contract
expired below strike.

## XRPStrategy Components

1. **Decoupled prior** - BTC signal capped at 30% weight maximum (vs 100%
   for ETH/SOL). XRP's own regime is the primary signal.

2. **Live correlation monitor** - Rolling 60-min Pearson correlation between
   XRP and BTC log returns. When correlation drops below 0.3, BTC signal
   is zeroed. XRP is in idiosyncratic mode.

3. **News-mode detector** - Extreme Kalshi contract velocity (>5% move over
   30 samples) as proxy for news/volume spike. Switches to momentum-
   continuation mode rather than adjusting probability.

4. **Variance-ratio regime** - Lo-MacKinlay VR(5) on XRP's own 60-min
   returns. Validated by Wen et al. as predictive for XRP specifically.
   Given higher weight than in ETH/SOL (4pp vs 3pp).

5. **XRP/BTC ratio divergence** - Secondary signal only (2pp cap). Detects
   when XRP has moved far from its historical ratio to BTC.

6. **Event calendar hard skip** - JSON file of SEC/Ripple events with
   per-severity skip windows (high=30min, medium=15min, low=5min). When
   a scheduled event is active, all XRP windows are skipped regardless of
   signal strength.
