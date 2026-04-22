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
