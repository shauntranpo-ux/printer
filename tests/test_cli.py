"""
Tests for backtesting/cli.py — Task 11.

Coverage:
  1. cmd_dry_run produces report.md at expected path
  2. cmd_dry_run produces all 8 artifact files
  3. cmd_dry_run report.md is non-empty
  4. cmd_dry_run produces valid JSON calibration_summary.json
  5. _load_global_config() loads YAML without error
  6. CLI argparse: dry-run --asset btc --strategy a parsed correctly (no I/O)
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import backtesting.cli as cli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EXPECTED_ARTIFACTS = [
    "calibration_chart",
    "equity_curve",
    "regime_heatmap",
    "cpcv_splits",
    "bootstrap_ci",
    "calibration_summary",
    "trading_summary",
    "overfitting_summary",
]


def _fake_config(tmp_path):
    return {
        "simulation": {"latency_ms": 500},
        "reports": {"output_dir": str(tmp_path)},
        "execution": {
            "assets": ["btc"],
            "strategies": ["a"],
            "sequential_only": True,
        },
    }


# ---------------------------------------------------------------------------
# Tests 1-4: cmd_dry_run end-to-end with patched output_dir
# ---------------------------------------------------------------------------


@pytest.fixture()
def dry_run_result(tmp_path, monkeypatch):
    """Run cmd_dry_run once and return (tmp_path, report_path)."""
    monkeypatch.setattr(cli, "_load_global_config", lambda: _fake_config(tmp_path))
    args = SimpleNamespace(asset="btc", strategy="a")
    cli.cmd_dry_run(args)
    report_path = tmp_path / "btc" / "report.md"
    return tmp_path, report_path


def test_dry_run_creates_report(dry_run_result):
    """report.md must exist after dry run."""
    _tmp_path, report_path = dry_run_result
    assert report_path.exists(), f"report.md not found at {report_path}"


def test_dry_run_all_artifacts_exist(dry_run_result):
    """All 8 artifact files must exist."""
    tmp_path, _report_path = dry_run_result
    asset_dir = tmp_path / "btc"
    missing = []
    for name in EXPECTED_ARTIFACTS:
        # Find any file whose stem starts with name
        matches = list(asset_dir.glob(f"{name}*"))
        if not matches:
            missing.append(name)
    assert not missing, f"Missing artifacts: {missing}\nFound: {list(asset_dir.iterdir())}"


def test_dry_run_report_nonempty(dry_run_result):
    """report.md must contain actual content."""
    _tmp_path, report_path = dry_run_result
    content = report_path.read_text(encoding="utf-8")
    assert len(content) > 20, f"report.md suspiciously small ({len(content)} bytes)"


def test_dry_run_calibration_json_valid(dry_run_result):
    """calibration_summary.json must be valid JSON."""
    tmp_path, _report_path = dry_run_result
    asset_dir = tmp_path / "btc"
    cal_files = list(asset_dir.glob("calibration_summary*"))
    assert cal_files, "calibration_summary artifact not found"
    cal_path = cal_files[0]
    if cal_path.suffix == ".json":
        with open(cal_path, encoding="utf-8") as f:
            data = json.load(f)
        assert isinstance(data, dict), "calibration_summary.json should be a dict"


# ---------------------------------------------------------------------------
# Test 5: _load_global_config loads YAML without error
# ---------------------------------------------------------------------------


def test_load_global_config():
    """_load_global_config must return a dict with expected top-level keys."""
    cfg = cli._load_global_config()
    assert isinstance(cfg, dict), "config should be a dict"
    assert "simulation" in cfg, "config missing 'simulation' key"
    assert "reports" in cfg, "config missing 'reports' key"
    assert "execution" in cfg, "config missing 'execution' key"


# ---------------------------------------------------------------------------
# Test 6: argparse — dry-run args parsed correctly (no I/O)
# ---------------------------------------------------------------------------


def test_argparse_dry_run():
    """CLI should parse 'dry-run --asset btc --strategy a' using the real parser."""
    parser = cli._build_parser()
    args = parser.parse_args(["dry-run", "--asset", "btc", "--strategy", "a"])
    assert args.command == "dry-run"
    assert args.asset == "btc"
    assert args.strategy == "a"
    assert args.func is cli.cmd_dry_run
