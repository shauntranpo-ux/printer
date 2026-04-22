"""
Per-asset report builder.

Generates 8 artifacts for each asset backtest run:
  1. calibration_chart.png    — reliability diagram (actual vs predicted)
  2. equity_curve.png         — cumulative P&L over time
  3. regime_heatmap.png       — Sharpe by regime/session
  4. cpcv_splits.png          — OOS Sharpe across CPCV splits
  5. bootstrap_ci.png         — bootstrap CI bar chart for Sharpe
  6. calibration_summary.json — calibration metrics dict
  7. trading_summary.json     — trading performance dict
  8. overfitting_summary.json — DSR, PSR, PBO dict

All PNGs use matplotlib with non-interactive backend (matplotlib.use("Agg")).
All JSONs are valid UTF-8.
Output directory: backtesting/output/reports/{asset}/
"""
from __future__ import annotations
import json
import os
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")  # MUST be before any other matplotlib imports
import matplotlib.pyplot as plt
from typing import Optional


def build_asset_report(
    asset: str,
    trade_log: pd.DataFrame,          # from run_backtest()
    y_true: np.ndarray,               # actual labels (0/1)
    p_hat: np.ndarray,                # model probability outputs
    cpcv_results: Optional[pd.DataFrame] = None,  # from run_cpcv()
    oos_sharpes: Optional[np.ndarray] = None,      # OOS Sharpes from CPCV splits
    is_sharpes: Optional[np.ndarray] = None,       # IS Sharpes from CPCV splits
    output_dir: str = "backtesting/output/reports",
) -> dict[str, str]:
    """
    Generate all per-asset report artifacts.
    Returns dict mapping artifact_name -> file_path.
    Creates output_dir/{asset}/ if it doesn't exist.
    """
    asset_dir = os.path.join(output_dir, asset)
    os.makedirs(asset_dir, exist_ok=True)

    artifacts: dict[str, str] = {}

    # 1. Calibration chart (reliability diagram)
    path = _plot_reliability_diagram(y_true, p_hat, asset_dir, asset)
    artifacts["calibration_chart"] = path

    # 2. Equity curve
    path = _plot_equity_curve(trade_log, asset_dir, asset)
    artifacts["equity_curve"] = path

    # 3. Regime heatmap
    path = _plot_regime_heatmap(trade_log, asset_dir, asset)
    artifacts["regime_heatmap"] = path

    # 4. CPCV splits chart
    path = _plot_cpcv_splits(cpcv_results, asset_dir, asset)
    artifacts["cpcv_splits"] = path

    # 5. Bootstrap CI chart
    if oos_sharpes is not None and len(oos_sharpes) > 1:
        path = _plot_bootstrap_ci(oos_sharpes, asset_dir, asset)
    else:
        path = _plot_bootstrap_ci(np.array([0.0]), asset_dir, asset)
    artifacts["bootstrap_ci"] = path

    # 6. Calibration summary JSON
    from backtesting.metrics.calibration import calibration_summary
    if len(y_true) > 0 and len(p_hat) > 0:
        cal = calibration_summary(y_true, p_hat)
        cal = {k: v for k, v in cal.items() if k != "regime"}
    else:
        cal = {"brier": None, "log_loss": None, "ece": None, "n": 0}
    cal_path = os.path.join(asset_dir, "calibration_summary.json")
    with open(cal_path, "w", encoding="utf-8") as f:
        json.dump(_make_json_safe(cal), f, indent=2)
    artifacts["calibration_summary"] = cal_path

    # 7. Trading summary JSON
    from backtesting.metrics.trading import trading_summary
    ts = trading_summary(trade_log) if not trade_log.empty else {
        "n_trades": 0, "sharpe": None, "win_rate": None,
        "total_pnl": None, "max_drawdown": None, "fee_drag": None,
    }
    ts_path = os.path.join(asset_dir, "trading_summary.json")
    with open(ts_path, "w", encoding="utf-8") as f:
        json.dump(_make_json_safe(ts), f, indent=2)
    artifacts["trading_summary"] = ts_path

    # 8. Overfitting summary JSON
    from backtesting.metrics.overfitting import overfitting_summary
    if oos_sharpes is not None and is_sharpes is not None and len(oos_sharpes) > 0:
        n_trials = len(oos_sharpes)
        ov = overfitting_summary(oos_sharpes, is_sharpes, n_trials)
    else:
        ov = {"dsr": float("nan"), "pbo": float("nan"), "psr": float("nan")}
    ov_path = os.path.join(asset_dir, "overfitting_summary.json")
    with open(ov_path, "w", encoding="utf-8") as f:
        json.dump(_make_json_safe(ov), f, indent=2)
    artifacts["overfitting_summary"] = ov_path

    return artifacts


def _plot_reliability_diagram(y_true: np.ndarray, p_hat: np.ndarray, asset_dir: str, asset: str) -> str:
    """Plot reliability diagram (actual fraction vs mean predicted probability per bin)."""
    from backtesting.metrics.calibration import reliability_diagram_data
    path = os.path.join(asset_dir, "calibration_chart.png")

    fig, ax = plt.subplots(figsize=(6, 6))

    if len(y_true) > 0 and len(p_hat) > 0:
        rd = reliability_diagram_data(y_true, p_hat)
        if not rd.empty:
            # Use confidence (mean_predicted) vs accuracy (fraction_positive)
            mask = rd["count"] > 0
            if mask.any():
                ax.plot(
                    rd.loc[mask, "mean_predicted"].values,
                    rd.loc[mask, "fraction_positive"].values,
                    "s-", label="Model",
                )

    ax.plot([0, 1], [0, 1], "k--", label="Perfect calibration")
    ax.set_xlabel("Mean predicted probability")
    ax.set_ylabel("Fraction of positives")
    ax.set_title(f"{asset.upper()} Reliability Diagram")
    ax.legend()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    fig.tight_layout()
    fig.savefig(path, dpi=100)
    plt.close(fig)
    return path


def _plot_equity_curve(trade_log: pd.DataFrame, asset_dir: str, asset: str) -> str:
    """Plot cumulative P&L (net of fees) over time."""
    path = os.path.join(asset_dir, "equity_curve.png")
    fig, ax = plt.subplots(figsize=(10, 4))

    if not trade_log.empty and "pnl" in trade_log.columns and "fee" in trade_log.columns:
        net_pnl = (trade_log["pnl"] - trade_log["fee"]).values
        cum_pnl = np.cumsum(net_pnl)
        x = range(len(cum_pnl))
        ax.plot(x, cum_pnl, linewidth=1.5)
        ax.axhline(0, color="gray", linewidth=0.8)

    ax.set_xlabel("Trade #")
    ax.set_ylabel("Cumulative Net P&L")
    ax.set_title(f"{asset.upper()} Equity Curve")
    fig.tight_layout()
    fig.savefig(path, dpi=100)
    plt.close(fig)
    return path


def _plot_regime_heatmap(trade_log: pd.DataFrame, asset_dir: str, asset: str) -> str:
    """Bar chart of Sharpe by session regime."""
    path = os.path.join(asset_dir, "regime_heatmap.png")
    fig, ax = plt.subplots(figsize=(10, 4))

    if not trade_log.empty and "entry_time" in trade_log.columns:
        from backtesting.metrics.regime import compute_regime_metrics
        regime_df = compute_regime_metrics(trade_log)
        if not regime_df.empty and "sharpe" in regime_df.columns:
            # Filter to session rows only if scope column exists
            if "scope" in regime_df.columns:
                session_rows = regime_df[regime_df["scope"] == "session"]
            else:
                session_rows = regime_df

            if not session_rows.empty:
                # Use regime_value if available, else regime column
                if "regime_value" in session_rows.columns:
                    labels = session_rows["regime_value"].tolist()
                elif "regime" in session_rows.columns:
                    labels = session_rows["regime"].tolist()
                else:
                    labels = [str(i) for i in range(len(session_rows))]

                values = session_rows["sharpe"].fillna(0).tolist()
                ax.bar(labels, values)
                ax.axhline(0, color="gray", linewidth=0.8)
                ax.set_xlabel("Session")
                ax.set_ylabel("Sharpe")

    ax.set_title(f"{asset.upper()} Sharpe by Session")
    fig.tight_layout()
    fig.savefig(path, dpi=100)
    plt.close(fig)
    return path


def _plot_cpcv_splits(cpcv_results: Optional[pd.DataFrame], asset_dir: str, asset: str) -> str:
    """Bar chart of OOS Sharpe for each CPCV split."""
    path = os.path.join(asset_dir, "cpcv_splits.png")
    fig, ax = plt.subplots(figsize=(8, 4))

    if cpcv_results is not None and not cpcv_results.empty and "sharpe" in cpcv_results.columns:
        splits = cpcv_results["split_id"].tolist() if "split_id" in cpcv_results.columns else list(range(len(cpcv_results)))
        sharpes = cpcv_results["sharpe"].fillna(0).tolist()
        ax.bar(splits, sharpes)
        ax.axhline(0, color="gray", linewidth=0.8)
        ax.set_xlabel("CPCV Split")
        ax.set_ylabel("OOS Sharpe")

    ax.set_title(f"{asset.upper()} CPCV OOS Sharpe per Split")
    fig.tight_layout()
    fig.savefig(path, dpi=100)
    plt.close(fig)
    return path


def _plot_bootstrap_ci(oos_sharpes: np.ndarray, asset_dir: str, asset: str) -> str:
    """Bar chart with error bars showing bootstrap CI for mean OOS Sharpe."""
    from backtesting.validation.bootstrap import bootstrap_ci
    path = os.path.join(asset_dir, "bootstrap_ci.png")
    fig, ax = plt.subplots(figsize=(5, 4))

    if len(oos_sharpes) > 1:
        point, lo, hi = bootstrap_ci(oos_sharpes, np.mean, n_iterations=200)
        err_lo = max(0.0, point - lo)
        err_hi = max(0.0, hi - point)
        ax.bar(["OOS Sharpe"], [point], yerr=[[err_lo], [err_hi]], capsize=8)
        ax.axhline(0, color="gray", linewidth=0.8)
    else:
        ax.bar(["OOS Sharpe"], [float(oos_sharpes[0]) if len(oos_sharpes) == 1 else 0.0])

    ax.set_ylabel("Sharpe")
    ax.set_title(f"{asset.upper()} Bootstrap CI (95%)")
    fig.tight_layout()
    fig.savefig(path, dpi=100)
    plt.close(fig)
    return path


def _make_json_safe(obj):
    """Recursively convert nan/inf to None and numpy scalars to Python scalars."""
    if isinstance(obj, dict):
        return {k: _make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_make_json_safe(v) for v in obj]
    if isinstance(obj, float) and (np.isnan(obj) or np.isinf(obj)):
        return None
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        v = float(obj)
        return None if (np.isnan(v) or np.isinf(v)) else v
    return obj


def render_asset_report(
    asset: str,
    artifacts: dict[str, str],
    output_dir: str = "backtesting/output/reports",
) -> str:
    """
    Render the Jinja2 Markdown report for a single asset.
    Returns the rendered Markdown string (also saves to {asset_dir}/report.md).
    """
    import jinja2
    template_dir = os.path.join(os.path.dirname(__file__), "templates")
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(template_dir))
    template = env.get_template("asset_report.md.j2")

    # Load JSON summaries
    cal = _load_json_safe(artifacts.get("calibration_summary"))
    ts  = _load_json_safe(artifacts.get("trading_summary"))
    ov  = _load_json_safe(artifacts.get("overfitting_summary"))

    md = template.render(
        asset=asset.upper(),
        calibration=cal,
        trading=ts,
        overfitting=ov,
        artifacts=artifacts,
    )

    asset_dir = os.path.join(output_dir, asset)
    os.makedirs(asset_dir, exist_ok=True)
    out_path = os.path.join(asset_dir, "report.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(md)
    return md


def _load_json_safe(path: Optional[str]) -> dict:
    if path is None or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)
