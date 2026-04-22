"""
Tests for backtesting/reports/comparison.py

TDD: write failing tests first, implement to pass.
"""
from __future__ import annotations
import json
import os
import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Helpers — build minimal artifact dicts backed by real JSON files
# ---------------------------------------------------------------------------

def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _make_artifacts(tmp_path, asset: str, n_trades: int = 10) -> dict[str, str]:
    """Create minimal per-asset artifact dict with real JSON files on disk."""
    asset_dir = str(tmp_path / "reports" / asset)
    os.makedirs(asset_dir, exist_ok=True)

    ts_path = os.path.join(asset_dir, "trading_summary.json")
    ov_path = os.path.join(asset_dir, "overfitting_summary.json")

    _write_json(ts_path, {
        "regime":      "all",
        "n_trades":    n_trades,
        "sharpe":      1.23,
        "win_rate":    0.58,
        "total_pnl":   42.5,
        "max_drawdown": -5.0,
        "fee_drag":    0.05,
    })
    _write_json(ov_path, {
        "dsr":  0.72,
        "pbo":  0.30,
        "psr":  0.81,
        "dsr_pvalue": 0.28,
    })

    return {
        "trading_summary":    ts_path,
        "overfitting_summary": ov_path,
    }


# ---------------------------------------------------------------------------
# Test 1: build_comparison_report returns non-empty Markdown string
# ---------------------------------------------------------------------------

def test_returns_nonempty_markdown(tmp_path):
    from backtesting.reports.comparison import build_comparison_report

    asset_artifacts = {
        "btc": _make_artifacts(tmp_path, "btc"),
        "eth": _make_artifacts(tmp_path, "eth"),
    }
    out_dir = str(tmp_path / "reports")

    md = build_comparison_report(asset_artifacts, output_dir=out_dir)

    assert isinstance(md, str)
    assert len(md.strip()) > 0


# ---------------------------------------------------------------------------
# Test 2: comparison_report.md is created on disk
# ---------------------------------------------------------------------------

def test_output_file_created_on_disk(tmp_path):
    from backtesting.reports.comparison import build_comparison_report

    asset_artifacts = {
        "btc": _make_artifacts(tmp_path, "btc"),
        "eth": _make_artifacts(tmp_path, "eth"),
    }
    out_dir = str(tmp_path / "reports")

    build_comparison_report(asset_artifacts, output_dir=out_dir)

    report_path = os.path.join(out_dir, "comparison_report.md")
    assert os.path.isfile(report_path)


# ---------------------------------------------------------------------------
# Test 3: comparison table includes all assets
# ---------------------------------------------------------------------------

def test_comparison_table_includes_all_assets(tmp_path):
    from backtesting.reports.comparison import build_comparison_report

    assets = ["btc", "eth", "sol"]
    asset_artifacts = {a: _make_artifacts(tmp_path, a) for a in assets}
    out_dir = str(tmp_path / "reports")

    md = build_comparison_report(asset_artifacts, output_dir=out_dir)

    for asset in assets:
        assert asset.upper() in md, f"Asset {asset.upper()} not found in comparison report"


# ---------------------------------------------------------------------------
# Test 4: works with a single asset (edge case)
# ---------------------------------------------------------------------------

def test_single_asset_no_crash(tmp_path):
    from backtesting.reports.comparison import build_comparison_report

    asset_artifacts = {"btc": _make_artifacts(tmp_path, "btc")}
    out_dir = str(tmp_path / "reports")

    md = build_comparison_report(asset_artifacts, output_dir=out_dir)

    assert isinstance(md, str)
    assert "BTC" in md


# ---------------------------------------------------------------------------
# Test 5: missing JSON files don't crash (graceful _load_json_safe)
# ---------------------------------------------------------------------------

def test_missing_json_files_no_crash(tmp_path):
    from backtesting.reports.comparison import build_comparison_report

    # Provide artifact paths that don't exist on disk
    out_dir = str(tmp_path / "reports")
    os.makedirs(out_dir, exist_ok=True)

    asset_artifacts = {
        "btc": {
            "trading_summary":     "/nonexistent/path/trading_summary.json",
            "overfitting_summary": "/nonexistent/path/overfitting_summary.json",
        }
    }

    # Should not raise
    md = build_comparison_report(asset_artifacts, output_dir=out_dir)
    assert isinstance(md, str)


# ---------------------------------------------------------------------------
# Test 6: report content includes expected column headers
# ---------------------------------------------------------------------------

def test_report_contains_column_headers(tmp_path):
    from backtesting.reports.comparison import build_comparison_report

    asset_artifacts = {"btc": _make_artifacts(tmp_path, "btc")}
    out_dir = str(tmp_path / "reports")

    md = build_comparison_report(asset_artifacts, output_dir=out_dir)

    # Check for key column headers from the template
    for header in ["Sharpe", "Win Rate", "N Trades"]:
        assert header in md, f"Column header '{header}' missing from comparison report"


# ---------------------------------------------------------------------------
# Test 7: report reflects correct n_trades values from JSON
# ---------------------------------------------------------------------------

def test_report_reflects_n_trades(tmp_path):
    from backtesting.reports.comparison import build_comparison_report

    asset_artifacts = {
        "btc": _make_artifacts(tmp_path, "btc", n_trades=99),
    }
    out_dir = str(tmp_path / "reports")

    md = build_comparison_report(asset_artifacts, output_dir=out_dir)

    assert "99" in md, "n_trades value 99 not found in comparison report"


# ---------------------------------------------------------------------------
# Test 8: saved file content matches returned string
# ---------------------------------------------------------------------------

def test_empty_asset_artifacts_no_crash(tmp_path):
    from backtesting.reports.comparison import build_comparison_report

    md = build_comparison_report({}, output_dir=str(tmp_path))
    assert isinstance(md, str)
    assert os.path.exists(os.path.join(str(tmp_path), "comparison_report.md"))


def test_saved_file_matches_returned_string(tmp_path):
    from backtesting.reports.comparison import build_comparison_report

    asset_artifacts = {"eth": _make_artifacts(tmp_path, "eth")}
    out_dir = str(tmp_path / "reports")

    md = build_comparison_report(asset_artifacts, output_dir=out_dir)

    report_path = os.path.join(out_dir, "comparison_report.md")
    with open(report_path, encoding="utf-8") as f:
        saved = f.read()

    assert md == saved
