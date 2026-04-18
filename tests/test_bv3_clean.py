"""
Tests for Section 8: BV3 clean regeneration.

Verifies:
1. split_config.json is valid and parseable
2. regen_bv3_clean.py dry-run completes without errors
3. All *_bv3_full.json tables exist and have correct schema
4. Tables use pre-2024 data only (data_end metadata <= 2023-12-31)
5. New tables load via asset_manager.load_bv3_tables()
6. Legacy backup tables are intact
"""

import json
import os
import subprocess
import sys

import pytest

ROOT      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BV3_DIR   = os.path.join(ROOT, "bv3_tables")
DATA_DIR  = os.path.join(ROOT, "data")
LEGACY    = os.path.join(BV3_DIR, "legacy")
ASSETS    = ["BTC", "ETH", "SOL", "XRP", "DOGE"]


# ── helpers ──────────────────────────────────────────────────────────────────

def _load_table(asset: str) -> dict:
    path = os.path.join(BV3_DIR, f"{asset}_bv3_full.json")
    with open(path) as f:
        return json.load(f)


# ── Task 8.3: split_config.json ───────────────────────────────────────────────

def test_split_config_exists():
    path = os.path.join(DATA_DIR, "split_config.json")
    assert os.path.exists(path), "data/split_config.json missing"


def test_split_config_has_train_end():
    path = os.path.join(DATA_DIR, "split_config.json")
    with open(path) as f:
        cfg = json.load(f)
    assert "train_end" in cfg, "split_config.json missing 'train_end'"
    assert cfg["train_end"] == "2023-12-31", f"unexpected train_end: {cfg['train_end']}"


# ── Task 8.4 / 8.6: regen script dry-run ────────────────────────────────────

def test_regen_script_dry_run():
    result = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "regen_bv3_clean.py"), "--dry-run"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, f"dry-run failed:\n{result.stderr}"
    assert "dry-run" in result.stdout
    for asset in ASSETS:
        assert asset in result.stdout


# ── Task 8.7 / 8.8: tables exist and have correct schema ────────────────────

@pytest.mark.parametrize("asset", ASSETS)
def test_clean_table_exists(asset):
    path = os.path.join(BV3_DIR, f"{asset}_bv3_full.json")
    assert os.path.exists(path), f"{asset}_bv3_full.json missing"


@pytest.mark.parametrize("asset", ASSETS)
def test_clean_table_schema(asset):
    tbl = _load_table(asset)
    assert "table" in tbl
    assert "dist_bounds" in tbl
    assert "metadata" in tbl
    assert len(tbl["table"]) == 13, "expected 13 distance rows"
    for row in tbl["table"]:
        assert len(row) == 13, "expected 13 minute columns per row"
        for p in row:
            assert 0.0 <= p <= 1.0, f"probability out of range: {p}"


@pytest.mark.parametrize("asset", ASSETS)
def test_clean_table_uses_pre2024_data(asset):
    tbl = _load_table(asset)
    meta = tbl["metadata"]
    data_end = meta["data_end"][:10]  # YYYY-MM-DD prefix
    assert data_end <= "2023-12-31", (
        f"{asset}: data_end={data_end} extends past train_end 2023-12-31 — "
        "table was NOT generated with clean split"
    )


@pytest.mark.parametrize("asset", ASSETS)
def test_clean_table_label(asset):
    tbl = _load_table(asset)
    assert tbl.get("label") == "full_clean", f"{asset}: expected label='full_clean', got {tbl.get('label')!r}"


@pytest.mark.parametrize("asset", ASSETS)
def test_clean_table_sufficient_windows(asset):
    tbl = _load_table(asset)
    windows = tbl["metadata"]["total_windows"]
    assert windows >= 50_000, f"{asset}: only {windows:,} windows — table may be too sparse"


# ── Task 8.8: asset_manager loads tables ─────────────────────────────────────

def test_asset_manager_loads_clean_tables():
    sys.path.insert(0, os.path.join(ROOT, "src"))
    import asset_manager as am
    am.load_bv3_tables(use_pre2023=False)
    for asset in ASSETS:
        assert am._bv3[asset]["loaded"], f"{asset}: BV3 table not marked as loaded"
        assert am._bv3[asset]["label"] == "full_clean", f"{asset}: unexpected label after load"


def test_empirical_win_prob_returns_valid_prob():
    sys.path.insert(0, os.path.join(ROOT, "src"))
    import asset_manager as am
    am.load_bv3_tables(use_pre2023=False)
    for asset in ASSETS:
        p = am.empirical_win_prob(asset, 0.005, 6.0)
        assert 0.0 <= p <= 1.0, f"{asset}: empirical_win_prob returned {p}"


# ── Task 8.1: legacy backup ──────────────────────────────────────────────────

@pytest.mark.parametrize("asset", ASSETS)
def test_legacy_backup_exists(asset):
    for suffix in ("full", "pre2023"):
        path = os.path.join(LEGACY, f"{asset}_bv3_{suffix}.json")
        assert os.path.exists(path), f"Legacy backup missing: {path}"


def test_legacy_readme_exists():
    assert os.path.exists(os.path.join(LEGACY, "README.md"))
