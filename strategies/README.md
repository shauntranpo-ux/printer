# QUARANTINED — strategies/ (test-support only)

This directory is **not imported by any production code**.

- No live bot `.py` file imports from `strategies/`
- It exists on the pytest pythonpath to support backtester/executor tests
- The live trading strategies (S1=EMA momentum, S2=contract velocity+OBI) live in `bot_strategy.py`

**Do not add imports from this directory to production code.**
See [`ARCHITECTURE.md`](../ARCHITECTURE.md) for the authoritative layout.

---

# strategies/ — Original Design Docs

Two-strategy architecture for Kalshi 15-minute crypto binary markets (BTC, ETH, SOL, XRP).

## Two-Strategy Architecture

**Strategy A** — Calibrated probability model.
Five feature modules (HAR-RS-J volatility, order flow, time-of-day regimes, cross-asset
BTC signals, funding/OI z-scores) are concatenated and passed to a calibrated logistic
regression that outputs P_model(up at 15-min expiry). A trade is placed when
|P_model − P_market| exceeds a fee-adjusted, regime-aware edge threshold.

**Strategy B** — Contract dislocation detector.
Detects when the Kalshi YES price moves more or less than the underlying spot price
implies (via a Brownian-with-drift implied probability translation). Fades the residual
back toward fair value with a confidence-scaled signal.

Eight model instances: 4 assets × 2 strategies. Same architecture per strategy;
separately fitted parameters per asset. No pooling of training data across assets.

## Data Assumptions

| Data type       | Schema                                               | Notes                         |
|-----------------|------------------------------------------------------|-------------------------------|
| Bars            | `timestamp, open, high, low, close, volume`          | 10-second default granularity |
| L2 book         | `timestamp, bids: [(price, size)], asks: [...]`      | At least 10 levels            |
| Trades          | `timestamp, price, size, aggressor_side`             | `aggressor_side ∈ {buy,sell}` |
| Funding         | `timestamp, funding_rate, open_interest`             | Binance/Bybit perp            |
| Kalshi ticks    | `timestamp, yes_bid, yes_ask, no_bid, no_ask`        | Optionally `seconds_to_expiry`|

All timestamps: UTC. All returns: log returns. p_market is Kalshi YES price / 100.

## Loading Configs

```python
import yaml, os

def load_config(strategy: str, asset: str) -> dict:
    path = os.path.join(os.path.dirname(__file__), strategy, "config", f"{asset.lower()}.yaml")
    with open(path) as f:
        return yaml.safe_load(f)

def load_fees() -> dict:
    path = os.path.join(os.path.dirname(__file__), "shared", "fees.yaml")
    with open(path) as f:
        return yaml.safe_load(f)

config = load_config("strategy_a", "ETH")
fees   = load_fees()
```

## Downstream Interfaces

The **backtester** and **executor** depend on these exact signatures. Do not change them.

```python
# Every feature module:
def compute(data_window) -> dict[str, float]: ...

# Strategy A model (instance methods on StrategyAModel):
def predict_proba(features: dict) -> float: ...          # calibrated P(up) in [0,1]
def get_edge(p_model: float, p_market: float) -> float: ...
def should_trade(p_model, p_market, regime, config, btc_degraded=False) -> bool: ...

# Strategy B:
def detect_dislocation(contract_stream, underlying_stream) -> Optional[DislocationSignal]: ...
```

**p_market convention**: always pass as a fraction in [0, 1], not cents.
A Kalshi YES ask of 70c → p_market = 0.70.

## Fee Notes

`shared/fees.yaml` stores a conservative flat-rate approximation (3%) used only for
pre-trade edge threshold checks. The actual Kalshi taker fee is
`ceil(0.07 × C × p × (1−p))` per contract (peaks at ~3.5% at p=0.50).
The execution layer in `src/strategies/fees.py` uses the exact formula for EV
computation — do not bypass it.

## Running Tests

```bash
# From the repo root
pytest tests/ -v
```

All tests use synthetic data. No live API calls.
