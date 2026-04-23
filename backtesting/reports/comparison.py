"""
Cross-asset comparison report builder.
Aggregates per-asset summaries into a single comparison table and Markdown report.
"""
from __future__ import annotations
import json
import os


def build_comparison_report(
    asset_artifacts: dict[str, dict[str, str]],  # {asset: {artifact_name: path}}
    output_dir: str = "backtesting/output/reports",
) -> str:
    """
    Build comparison report from per-asset artifact dicts.
    Reads {asset}/trading_summary.json and {asset}/overfitting_summary.json for each asset.
    Returns rendered Markdown string.
    Saves to {output_dir}/comparison_report.md.
    """
    rows = []
    for asset, artifacts in asset_artifacts.items():
        ts = _load_json_safe(artifacts.get("trading_summary"))
        ov = _load_json_safe(artifacts.get("overfitting_summary"))
        rows.append({
            "asset":     asset.upper(),
            "n_trades":  ts.get("n_trades", 0),
            "sharpe":    ts.get("sharpe"),
            "win_rate":  ts.get("win_rate"),
            "total_pnl": ts.get("total_pnl"),
            "dsr":       ov.get("dsr"),
            "pbo":       ov.get("pbo"),
            "psr":       ov.get("psr"),
        })

    import jinja2
    template_dir = os.path.join(os.path.dirname(__file__), "templates")
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(template_dir))
    template = env.get_template("comparison_report.md.j2")
    md = template.render(table=rows, assets=list(asset_artifacts.keys()))

    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, "comparison_report.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(md)
    return md


def _load_json_safe(path) -> dict:
    if path is None or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_strategy_c_comparison_report(
    asset_artifacts: dict[str, dict[str, str]],  # {asset: {artifact_name: path}}
    output_dir: str = "backtesting/output/reports",
) -> str:
    """
    Build a Strategy C cross-asset comparison report.
    Reads strategy_c_summary.json and strategy_c2_summary.json for each asset.
    Returns rendered Markdown string.
    Saves to {output_dir}/strategy_c_comparison.md.
    """
    rows = []
    for asset, artifacts in asset_artifacts.items():
        s = _load_json_safe(artifacts.get("strategy_c_summary"))
        c2 = _load_json_safe(artifacts.get("strategy_c2_summary"))
        rows.append({
            "asset": asset.upper(),
            "n_events_traded": s.get("n_events_traded", 0),
            "c1_trades": s.get("c1_trades", 0),
            "c2_trades": s.get("c2_trades", 0),
            "combined_sharpe": s.get("combined_sharpe"),
            "combined_win_rate": s.get("combined_win_rate"),
            "combined_net_pnl": s.get("combined_net_pnl"),
            "fee_drag_pct": s.get("fee_drag_pct"),
            "c2_n_monotonicity": c2.get("n_monotonicity", 0),
            "c2_n_convexity": c2.get("n_convexity", 0),
            "c2_net_pnl": c2.get("net_pnl"),
            "c2_win_rate": c2.get("win_rate"),
        })

    import jinja2
    template_dir = os.path.join(os.path.dirname(__file__), "templates")
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(template_dir))
    template = env.get_template("strategy_c_comparison.md.j2")
    md = template.render(table=rows, assets=list(asset_artifacts.keys()))

    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, "strategy_c_comparison.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(md)
    return md
