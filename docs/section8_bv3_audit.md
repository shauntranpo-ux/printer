# Section 8: BV3 Table Regeneration Audit

## Generator: `generate_bv3_table.py`

### Input
- CSV files: `data/{ASSET}_1m.csv` - 1-minute OHLCV from Binance
- Column: `time` (Unix seconds) → normalised to `open_time` (datetime64 UTC)

### Algorithm (`build_table`)
1. Sort by `open_time`, floor to 15-minute `window_start`
2. For each 15-min window with ≥15 rows: take first 15 rows
3. Identify `nearest_strike` using asset-specific increments
4. At each `t ∈ [1, 13]` minutes remaining, check if `close ≥ strike`
5. Accumulate `wins / total` per `(dist_bucket, minutes_remaining)` cell

### Output schema
```json
{
  "table": [[p_row0_t1, ..., p_row0_t13], ...],   // 13 rows × 13 cols
  "dist_bounds": [...],
  "label": "full" | "pre2023",
  "metadata": {"total_windows": N, "data_start": "...", "data_end": "..."}
}
```

### Existing outputs
- `*_bv3_full.json`    - trained on ALL data (contaminated)
- `*_bv3_pre2023.json` - trained on pre-2023 only (cutoff: 2023-01-01)

---

## Data Leakage Analysis

### Problem
The `*_bv3_full.json` tables are trained on data through April 2026. The bot
uses these tables at runtime (`load_bv3_tables(use_pre2023=False)`). Any
evaluation against post-2023 data would be contaminated because the table
was fitted on that same data.

### Why `*_bv3_pre2023.json` is insufficient
The existing pre-2023 tables cut at 2023-01-01, discarding all of 2023. This
wastes a full year of training data. The proper split is:
- **Train**: all data through 2023-12-31
- **Test**: 2024-01-01 onwards

### Data coverage per asset (clean split, train_end=2023-12-31)

| Asset | Train start    | Train rows  | Total rows  | % used |
|-------|----------------|-------------|-------------|--------|
| BTC   | 2017-08-17     | 3,343,448   | 4,537,209   | 73.7%  |
| ETH   | 2019-09-23     | 2,244,408   | 3,447,000   | 65.1%  |
| SOL   | 2020-09-18     | 1,726,068   | 2,928,672   | 58.9%  |
| XRP   | 2019-09-23     |   932,175   | 2,134,781   | 43.7%  |
| DOGE  | 2019-10-25     | 2,199,039   | 3,401,647   | 64.6%  |

All assets have sufficient training data (>900k rows). XRP has the smallest
training set; its table cells will be sparser at extreme distance buckets.

---

## Fix: `scripts/regen_bv3_clean.py`

The clean generator reuses `generate_bv3_table.py`'s core logic but:
1. Reads `data/split_config.json` for `train_end` boundary
2. Filters to `open_time < train_end + 1 day` before building the table
3. Writes output to `bv3_tables/{ASSET}_bv3_full.json` (overwriting contaminated tables)
4. Backs up existing tables to `bv3_tables/legacy/` first (already done)

Bot loading code (`asset_manager.load_bv3_tables`) requires no changes.

---

## Bot loading path

```python
# bot.py line 4361
load_bv3_tables(use_pre2023=False)

# asset_manager.py
def load_bv3_tables(use_pre2023: bool = False) -> None:
    suffix = "pre2023" if use_pre2023 else "full"
    path = f"bv3_tables/{asset}_bv3_{suffix}.json"
    # loads table and dist_bounds
```

After regeneration, `*_bv3_full.json` will be the clean pre-2024 tables.
The `*_bv3_pre2023.json` files are left unchanged (still useful for historical comparison).
