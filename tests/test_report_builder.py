"""
Tests for backtesting/reports/report_builder.py

TDD: write failing tests first, implement to pass.
"""
from __future__ import annotations
import json
import os
import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_trade_log(n: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    ts = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")
    return pd.DataFrame({
        "entry_time": ts,
        "exit_time":  ts + pd.Timedelta(minutes=15),
        "asset":      "btc",
        "strategy":   "strategy_a",
        "side":       (["yes", "no"] * (n // 2)) + ["yes"] * (n % 2),
        "p_model":    rng.random(n),
        "p_market":   [0.5] * n,
        "edge":       rng.random(n) - 0.5,
        "regime":     ["eu_open"] * n,
        "fill_price": [0.55] * n,
        "pnl":        np.random.default_rng(44).random(n) - 0.3,
        "fee":        [0.015] * n,
        "label":      ([1, 0, 1, 0, 1] * (n // 5 + 1))[:n],
    })


def _make_p_arrays(n: int = 20) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(7)
    y_true = rng.integers(0, 2, size=n).astype(float)
    p_hat  = rng.uniform(0.1, 0.9, size=n)
    return y_true, p_hat


REQUIRED_ARTIFACT_KEYS = {
    "calibration_chart",
    "equity_curve",
    "regime_heatmap",
    "cpcv_splits",
    "bootstrap_ci",
    "calibration_summary",
    "trading_summary",
    "overfitting_summary",
}

REQUIRED_PNG_KEYS = {
    "calibration_chart",
    "equity_curve",
    "regime_heatmap",
    "cpcv_splits",
    "bootstrap_ci",
}

REQUIRED_JSON_KEYS = {
    "calibration_summary",
    "trading_summary",
    "overfitting_summary",
}


# ---------------------------------------------------------------------------
# Test 1: build_asset_report creates the output directory
# ---------------------------------------------------------------------------

def test_build_creates_output_directory(tmp_path):
    from backtesting.reports.report_builder import build_asset_report

    trade_log = _make_trade_log()
    y_true, p_hat = _make_p_arrays()

    out_dir = str(tmp_path / "reports")
    assert not os.path.exists(out_dir)  # should not exist yet

    build_asset_report("btc", trade_log, y_true, p_hat, output_dir=out_dir)

    assert os.path.isdir(os.path.join(out_dir, "btc"))


# ---------------------------------------------------------------------------
# Test 2: returns dict with all 8 required artifact keys
# ---------------------------------------------------------------------------

def test_build_returns_all_8_artifact_keys(tmp_path):
    from backtesting.reports.report_builder import build_asset_report

    trade_log = _make_trade_log()
    y_true, p_hat = _make_p_arrays()

    artifacts = build_asset_report(
        "btc", trade_log, y_true, p_hat,
        output_dir=str(tmp_path / "reports"),
    )

    assert REQUIRED_ARTIFACT_KEYS == set(artifacts.keys()), (
        f"Missing keys: {REQUIRED_ARTIFACT_KEYS - set(artifacts.keys())}\n"
        f"Extra keys:   {set(artifacts.keys()) - REQUIRED_ARTIFACT_KEYS}"
    )


# ---------------------------------------------------------------------------
# Test 3: all 5 PNG files exist on disk
# ---------------------------------------------------------------------------

def test_all_png_files_created(tmp_path):
    from backtesting.reports.report_builder import build_asset_report

    trade_log = _make_trade_log()
    y_true, p_hat = _make_p_arrays()

    artifacts = build_asset_report(
        "btc", trade_log, y_true, p_hat,
        output_dir=str(tmp_path / "reports"),
    )

    for key in REQUIRED_PNG_KEYS:
        path = artifacts[key]
        assert os.path.isfile(path), f"PNG not found on disk: {key} -> {path}"


# ---------------------------------------------------------------------------
# Test 4: all 3 JSON files exist and are valid JSON
# ---------------------------------------------------------------------------

def test_all_json_files_created_and_valid(tmp_path):
    from backtesting.reports.report_builder import build_asset_report

    trade_log = _make_trade_log()
    y_true, p_hat = _make_p_arrays()

    artifacts = build_asset_report(
        "btc", trade_log, y_true, p_hat,
        output_dir=str(tmp_path / "reports"),
    )

    for key in REQUIRED_JSON_KEYS:
        path = artifacts[key]
        assert os.path.isfile(path), f"JSON not found on disk: {key} -> {path}"
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert isinstance(data, dict), f"{key} JSON is not a dict"


# ---------------------------------------------------------------------------
# Test 5: empty trade_log doesn't crash; n_trades=0 in trading_summary.json
# ---------------------------------------------------------------------------

def test_empty_trade_log_no_crash(tmp_path):
    from backtesting.reports.report_builder import build_asset_report

    empty_log = pd.DataFrame(columns=[
        "entry_time", "exit_time", "asset", "strategy", "side",
        "p_model", "p_market", "edge", "regime", "fill_price",
        "pnl", "fee", "label",
    ])
    y_true, p_hat = _make_p_arrays()

    artifacts = build_asset_report(
        "btc", empty_log, y_true, p_hat,
        output_dir=str(tmp_path / "reports"),
    )

    with open(artifacts["trading_summary"], encoding="utf-8") as f:
        ts = json.load(f)
    assert ts.get("n_trades") == 0


# ---------------------------------------------------------------------------
# Test 6: empty y_true / p_hat doesn't crash
# ---------------------------------------------------------------------------

def test_empty_y_true_p_hat_no_crash(tmp_path):
    from backtesting.reports.report_builder import build_asset_report

    trade_log = _make_trade_log()

    # Should not raise
    artifacts = build_asset_report(
        "btc", trade_log,
        y_true=np.array([]),
        p_hat=np.array([]),
        output_dir=str(tmp_path / "reports"),
    )

    assert REQUIRED_ARTIFACT_KEYS == set(artifacts.keys())


# ---------------------------------------------------------------------------
# Test 7: render_asset_report returns non-empty string and creates report.md
# ---------------------------------------------------------------------------

def test_render_asset_report_creates_md(tmp_path):
    from backtesting.reports.report_builder import build_asset_report, render_asset_report

    trade_log = _make_trade_log()
    y_true, p_hat = _make_p_arrays()
    out_dir = str(tmp_path / "reports")

    artifacts = build_asset_report("btc", trade_log, y_true, p_hat, output_dir=out_dir)
    md = render_asset_report("btc", artifacts, output_dir=out_dir)

    assert isinstance(md, str)
    assert len(md.strip()) > 0

    report_path = os.path.join(out_dir, "btc", "report.md")
    assert os.path.isfile(report_path)


# ---------------------------------------------------------------------------
# Test 8: PNG files are non-empty (> 0 bytes)
# ---------------------------------------------------------------------------

def test_png_files_are_non_empty(tmp_path):
    from backtesting.reports.report_builder import build_asset_report

    trade_log = _make_trade_log()
    y_true, p_hat = _make_p_arrays()

    artifacts = build_asset_report(
        "btc", trade_log, y_true, p_hat,
        output_dir=str(tmp_path / "reports"),
    )

    for key in REQUIRED_PNG_KEYS:
        path = artifacts[key]
        size = os.path.getsize(path)
        assert size > 0, f"PNG is zero bytes: {key} -> {path}"


# ---------------------------------------------------------------------------
# Test 9: with CPCV results and OOS/IS sharpes
# ---------------------------------------------------------------------------

def test_build_with_cpcv_and_sharpes(tmp_path):
    from backtesting.reports.report_builder import build_asset_report

    trade_log = _make_trade_log(n=20)
    y_true, p_hat = _make_p_arrays(n=20)

    rng = np.random.default_rng(0)
    cpcv_results = pd.DataFrame({
        "split_id":  [1, 2, 3],
        "n_train":   [100, 100, 100],
        "n_test":    [20, 20, 20],
        "test_start": pd.to_datetime(["2024-01-01", "2024-02-01", "2024-03-01"]),
        "test_end":   pd.to_datetime(["2024-01-31", "2024-02-28", "2024-03-31"]),
        "sharpe":    rng.normal(0.5, 0.2, 3),
        "win_rate":  [0.55, 0.60, 0.58],
        "brier":     [0.22, 0.20, 0.21],
        "log_loss":  [0.65, 0.62, 0.63],
        "n_trades":  [10, 12, 11],
    })
    oos_sharpes = rng.normal(0.5, 0.2, 8)
    is_sharpes  = rng.normal(0.8, 0.3, 8)

    artifacts = build_asset_report(
        "eth", trade_log, y_true, p_hat,
        cpcv_results=cpcv_results,
        oos_sharpes=oos_sharpes,
        is_sharpes=is_sharpes,
        output_dir=str(tmp_path / "reports"),
    )

    assert REQUIRED_ARTIFACT_KEYS == set(artifacts.keys())

    # Overfitting summary should have DSR/PSR/PBO values
    with open(artifacts["overfitting_summary"], encoding="utf-8") as f:
        ov = json.load(f)
    assert "dsr" in ov
    assert "pbo" in ov
    assert "psr" in ov


# ---------------------------------------------------------------------------
# Test 10: _make_json_safe converts nan/inf to None
# ---------------------------------------------------------------------------

def test_make_json_safe_handles_nan_inf():
    from backtesting.reports.report_builder import _make_json_safe

    d = {
        "a": float("nan"),
        "b": float("inf"),
        "c": float("-inf"),
        "d": 1.23,
        "e": 42,
        "f": "hello",
    }
    result = _make_json_safe(d)

    assert result["a"] is None
    assert result["b"] is None
    assert result["c"] is None
    assert result["d"] == pytest.approx(1.23)
    assert result["e"] == 42
    assert result["f"] == "hello"

    # Must be JSON-serializable
    serialized = json.dumps(result)
    assert isinstance(serialized, str)
