"""
Per-event position selector for Strategy C1.

Within a single Kalshi hourly event (~40 strikes), C1 may flag several strikes
with edge.  This module de-correlates correlated adjacent picks and enforces a
maximum-positions-per-event cap.

Cross-event exposure limits belong to the execution layer, not here.
"""
from __future__ import annotations
import pandas as pd


def select_positions(candidates_df: pd.DataFrame, config: dict) -> pd.DataFrame:
    """
    Pick at most max_positions_per_event strikes from candidates_df.

    Selection rules:
    1. Sort candidates by |edge| descending.
    2. Pick the top candidate unconditionally.
    3. A second candidate is included only if its index in the original sorted
       ladder is at least min_strike_spacing_count positions away from the first
       pick's ladder index.  (candidates_df must have a 'ladder_rank' column
       representing the strike's ordinal position in the full ladder, 0-based.)
    4. Stop when max_positions_per_event is reached.

    Args:
        candidates_df: DataFrame with at minimum columns:
                           strike, edge, ladder_rank (int, ordinal rank in full ladder)
                       Any additional columns are preserved in output.
        config:        asset config dict; uses selection section:
                           selection.max_positions_per_event  (default 2)
                           selection.min_strike_spacing_count (default 3)

    Returns:
        Filtered DataFrame preserving ranking order (highest |edge| first).
    """
    if candidates_df.empty:
        return candidates_df.iloc[0:0].copy()

    sel_cfg = config.get("selection", {}) or {}
    max_pos: int = int(sel_cfg.get("max_positions_per_event", 2))
    min_spacing: int = int(sel_cfg.get("min_strike_spacing_count", 3))

    sorted_df = candidates_df.copy()
    sorted_df["_abs_edge"] = sorted_df["edge"].abs()
    sorted_df = sorted_df.sort_values("_abs_edge", ascending=False).reset_index(drop=True)

    selected_ranks: list[int] = []
    selected_indices: list[int] = []

    for idx, row in sorted_df.iterrows():
        rank = int(row["ladder_rank"])
        too_close = any(abs(rank - r) < min_spacing for r in selected_ranks)
        if not too_close:
            selected_ranks.append(rank)
            selected_indices.append(idx)
        if len(selected_indices) >= max_pos:
            break

    result = sorted_df.loc[selected_indices].drop(columns=["_abs_edge"])
    return result.reset_index(drop=True)
