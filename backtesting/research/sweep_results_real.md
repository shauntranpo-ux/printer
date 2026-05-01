# Step 2 -- Single-Feature Threshold Sweep (Inverted Directions)

## Optimal Thresholds

| Asset | MTF threshold | RSI threshold | Boll threshold | Ens IC | Fire% | YES% | WR_Y | WR_N |
|-------|--------------|--------------|----------------|--------|-------|------|------|------|
| BTC | 0.0005 | 5.0 | 0.75 | 0.0589 | 73.3% | 48.8% | 53.8% | 53.0% |
| ETH | 0.0005 | 8.0 | 0.50 | 0.0669 | 76.5% | 46.0% | 52.7% | 55.0% |
| SOL | 0.0005 | 10.0 | 0.50 | 0.0473 | 79.8% | 45.8% | 49.4% | 56.1% |
| XRP | 0.0005 | 8.0 | 0.35 | 0.0539 | 79.4% | 45.6% | 47.0% | 59.2% |

## Updated MTF Thresholds

```python
_MTF_THRESHOLDS = {
    "BTC": 0.0005,
    "ETH": 0.0005,
    "SOL": 0.0005,
    "XRP": 0.0005,
}
```
