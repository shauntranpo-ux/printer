# Strategy C - Kalshi Hourly Strike-Ladder Markets

BTC and ETH only. No SOL, no XRP.

## Architecture

Strategy C has two parallel components that share one underlying probability model.

### C1 - Per-Strike Probability Forecasting

C1 applies a Black-Scholes-style digital call formula (`N(d2)`) to every
strike on the ~40-strike ladder, producing a model probability for each.
Strikes whose model probability diverges from the Kalshi market-implied
probability by more than the fee-adjusted threshold are flagged as candidates.

The key insight: the full ladder forms an empirical risk-neutral CDF of the
expiry price. C1 exploits systematic mispricing of individual strikes while
treating the overall distribution as given.

**Pipeline (per snapshot):**
1. `features/strike_ladder.parse_ladder()` - parse and validate the raw ladder
2. `features/vol_term_structure.integrate_forecasted_variance()` - integrate HAR-forecasted vol over `[now, expiry]`
3. `probability/probability_surface.ProbabilitySurface.evaluate()` - evaluate N(d2) per strike, apply per-bucket calibration
4. `model.StrategyC1Model.rank_candidates()` - gate on fee + regime + moneyness thresholds
5. `selection/event_selector.select_positions()` - de-correlate adjacent picks, cap at 2 per event

### C2 - Ladder No-Arbitrage Scanner

C2 is model-free. It runs on every snapshot and detects mathematical
violations of the no-arbitrage conditions that the full ladder must satisfy:

| Violation       | Condition                                        | Trade                                |
|-----------------|--------------------------------------------------|--------------------------------------|
| Monotonicity    | `P(K_low) < P(K_high)` (impossible)              | Buy K_low YES, buy K_high NO         |
| Convexity       | `p(K1) - 2*p(K2) + p(K3) > 0` (K2 underpriced) | Sell K1 YES, buy 2x K2 YES, sell K3 YES |
| Bounds          | `p < ε` or `p > 1-ε`                            | Log only - do not trade              |

Both C1 and C2 run independently.  A single snapshot may produce signals
from both components simultaneously.

## Feature Reuse from Strategy A

`features/feature_adapter.py` re-exports four modules from `strategy_a/features/`
without copying code:

| Module | Exported symbol | Used by C1 for |
|--------|----------------|---------------|
| `har_rv.py` | `HARRSJForecaster` | Realized variance forecasting; σ drives N(d2) |
| `time_of_day.py` | `compute_time_of_day` | Session regime lookup for threshold gating |
| `cross_asset.py` | `compute_cross_asset` | ETH model: BTC jump signal -> drift adjustment `mu_hat` |
| `funding.py` | `FundingFeatures` | Crowded-position modifier (future calibration feature) |

Strategy C does **not** use the `order_flow` module (disabled codebase-wide).

## Per-Moneyness Calibration

Raw N(d2) outputs are systematically biased at the tails - the model over-
or under-states probability for deep-OTM and deep-ITM strikes.  C1 trains
**separate calibrators per moneyness bucket**:

| Bucket    | log(K/S) range        | Calibrator | Reason |
|-----------|----------------------|------------|--------|
| `deep_itm` | <= -0.02              | isotonic   | near-certain contract; monotone correction |
| `itm`      | (-0.02, -0.005]      | isotonic   | mild tail, monotone correction sufficient |
| `atm`      | (-0.005, 0.005]      | isotonic   | best-fit zone; isotonic preserves ordering |
| `otm`      | (0.005, 0.02]        | isotonic   | mild OTM |
| `deep_otm` | > 0.02               | platt      | heavy-tailed; Platt scaling handles extreme values |

Artifact paths in `config/{asset}.yaml -> calibration.artifact_paths` are
`null` until training has been run.  Until then, raw N(d2) values are used.

## Threshold Interaction

For each candidate strike:

```
base_threshold  = taker_fee_rate + safety_margin + base_edge_above_fee[regime]
                  (null regime entry -> 0.02 default)
min_edge        = base_threshold x moneyness_multiplier[moneyness_bucket]
                  (deep_itm and deep_otm get 2x multiplier by default)
if deep_otm AND buying YES (longshot):
    min_edge   += longshot_buy_penalty   (0.02 default)
trade if |edge| > min_edge
```

The longshot-buy penalty encodes the well-documented longshot bias in prediction
markets - bettors systematically overpay for low-probability outcomes.  Selling
deep-OTM (buying NO) does *not* get this penalty.

## Interface Contract for Downstream Consumers

These signatures are locked.  Backtester and executor import them directly.

```python
# Probability evaluator
from strategy_c.probability.digital_call import binary_call_probability
binary_call_probability(current_price, strike_price, integrated_variance,
                        time_to_expiry_seconds, risk_free_rate=0.0) -> float

# C1 model
from strategy_c.model import StrategyC1Model
model = StrategyC1Model(config)
surface_df  = model.predict_surface(snapshot, feature_vector, config)
candidates  = model.rank_candidates(surface_df, config)
go          = model.should_trade_strike(edge, moneyness_bucket, regime, config)

# C2 scanner
from strategy_c.scanner import StrategyC2Scanner
scanner = StrategyC2Scanner(config)
signals = scanner.scan_snapshot(snapshot)  # -> list[ArbitrageSignal]

# Event selector
from strategy_c.selection.event_selector import select_positions
picks = select_positions(candidates_df, config)

# Vol term structure
from strategy_c.features.vol_term_structure import integrate_forecasted_variance
iv = integrate_forecasted_variance(har_forecast_fn, ts_now, ts_expiry,
                                   regime_lookup_fn, config) -> float
```

## Snapshot Schema

```python
snapshot = {
    "event_id": str,
    "event_close_time": pd.Timestamp,      # UTC
    "timestamp_now": pd.Timestamp,         # UTC, for term-structure integration
    "timestamp_expiry": pd.Timestamp,      # UTC, = event_close_time
    "spot_price": float,
    "time_to_expiry_seconds": float,
    "strikes": [
        {
            "strike": float,
            "yes_bid": float,    # in [0, 1]
            "yes_ask": float,
            "no_bid": float,
            "no_ask": float,
            "last_price": float,
            "volume": float,
            "market_id": str,
        },
        ...
    ]
}
```

## Assets in Scope

BTC and ETH only.  Strategy C is not applicable to SOL or XRP - strike
spacing for those assets is not standardised on Kalshi, and the per-strike
CDF interpretation requires uniform spacing for the convexity scanner.
