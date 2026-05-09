#!/usr/bin/env python3
"""
scripts/run_calibration.py

Load .env, run calibrate_winrates.py, parse stdout, auto-patch bot_strategy.py.

Usage:
    py scripts/run_calibration.py [--days N]

Options:
    --days N    Days of Kalshi history to fetch (default: 30)
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v)


def extract_dict_block(text: str, var_name: str) -> str | None:
    """Extract a multi-line Python dict assignment from calibration output."""
    pattern = rf"^({re.escape(var_name)}: dict = \{{[\s\S]*?^\}})"
    m = re.search(pattern, text, re.MULTILINE)
    return m.group(1) if m else None


def patch_bot_strategy(s1_block: str | None, s2_block: str | None) -> None:
    strat = ROOT / "bot_strategy.py"
    text = strat.read_text(encoding="utf-8")

    if s1_block:
        text = re.sub(
            r"_S1_WIN_RATE: dict = \{[\s\S]*?^\}",
            s1_block,
            text,
            count=1,
            flags=re.MULTILINE,
        )
        print("[patcher] _S1_WIN_RATE patched")

    if s2_block:
        text = re.sub(
            r"_S2_WIN_RATE: dict = \{[\s\S]*?^\}",
            s2_block,
            text,
            count=1,
            flags=re.MULTILINE,
        )
        print("[patcher] _S2_WIN_RATE patched")

    strat.write_text(text, encoding="utf-8")


def summarize_table(block: str, var_name: str) -> None:
    """Print per-asset bucket fill counts."""
    asset_pattern = r'"(\w+)":\s*\{([^}]*)\}'
    for asset, body in re.findall(asset_pattern, block):
        entries = re.findall(r"\((\d+),(\d+)\):\s*([\d.]+|None)", body)
        filled = sum(1 for _, _, v in entries if v != "None")
        print(f"  {var_name} {asset}: {filled}/{len(entries)} buckets filled")


def main() -> None:
    days = 30
    for i, arg in enumerate(sys.argv[1:]):
        if arg == "--days" and i + 1 < len(sys.argv) - 1:
            days = int(sys.argv[i + 2])

    load_dotenv(ROOT / ".env")

    env = os.environ.copy()
    env["CALIBRATE_DAYS"] = str(days)
    env["SKIP_S1"] = "1"  # S1 tables already populated in bot_strategy.py

    print(f"[runner] Starting calibration with CALIBRATE_DAYS={days} SKIP_S1=1")
    print(f"[runner] KALSHI_API_KEY: {'set' if env.get('KALSHI_API_KEY') else 'MISSING'}")
    print(f"[runner] KALSHI_PRIVATE_KEY: {env.get('KALSHI_PRIVATE_KEY', 'MISSING')}")

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "calibrate_winrates.py")],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=env,
    )

    if result.stderr:
        print("[calibrate stderr]")
        print(result.stderr)

    stdout = result.stdout
    if not stdout.strip():
        print("[runner] ERROR: no stdout from calibrate_winrates.py")
        print("[runner] Return code:", result.returncode)
        sys.exit(1)

    print("[runner] Calibration complete. Parsing output...")

    s1_block = extract_dict_block(stdout, "_S1_WIN_RATE")
    s2_block = extract_dict_block(stdout, "_S2_WIN_RATE")

    if not s1_block:
        print("[runner] WARNING: could not parse _S1_WIN_RATE from output")
    if not s2_block:
        print("[runner] WARNING: could not parse _S2_WIN_RATE from output")

    if s1_block or s2_block:
        patch_bot_strategy(s1_block, s2_block)
        if s1_block:
            summarize_table(s1_block, "_S1_WIN_RATE")
        if s2_block:
            summarize_table(s2_block, "_S2_WIN_RATE")
        print("[runner] bot_strategy.py patched successfully")
    else:
        print("[runner] Nothing to patch. Raw stdout:")
        print(stdout[:2000])


if __name__ == "__main__":
    main()
