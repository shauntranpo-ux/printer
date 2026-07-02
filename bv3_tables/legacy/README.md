# BV3 Legacy Tables - Backup

Copied from `bv3_tables/` on 2026-04-18 before Section 8 regeneration.

## Why these exist

The original `*_bv3_full.json` tables were generated from ALL available historical
data, including dates that overlap the intended out-of-sample test set (2024+).
This is data leakage: the model "knows" the very data we would use to evaluate it.

## Contents

- `*_bv3_full.json`    - contaminated tables (trained on full history incl. 2024+)
- `*_bv3_pre2023.json` - partial fix (trained on pre-2023 only; excludes all of 2023)

## Replacement

Section 8 regenerates `bv3_tables/*_bv3_full.json` using only pre-2024 data
(train_end: 2023-12-31). See `data/split_config.json` and `scripts/regen_bv3_clean.py`.

These legacy files are kept for rollback only. Do not load them in production.
