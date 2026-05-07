"""Tests for go-live reliability fixes."""
import pathlib

ROOT = pathlib.Path(__file__).parent.parent  # tests/ -> repo root


def test_session_ev_adjustment_removed():
    """_session_ev_adjustment must not exist in any bot module — it was a dead stub."""
    for fname in ["bot_strategy.py", "bot_loops.py", "bot_risk.py"]:
        src = (ROOT / fname).read_text(encoding="utf-8")
        assert "_session_ev_adjustment" not in src, (
            f"Found '_session_ev_adjustment' in {fname} — remove it"
        )
