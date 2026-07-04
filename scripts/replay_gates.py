"""
scripts/replay_gates.py - replay an exported trade CSV through the v3 gate set.

Answers: which of the historical trades would the rebuilt gates have blocked, and
what would the surviving P&L have been? Pure offline arithmetic over the logged
entry_signals - no network, no bot state. Only rows from the fair-value era (signals
carry z or residual) are gated; older rows are reported but left untouched.

    python3 scripts/replay_gates.py tests/fixtures/trades_export_2026-06-30_07-04.csv

Gate parameters mirror the live defaults in bot_infra._init_config. The vol-anchor
gate reprices the trade with the sigma implied by the market's own quote at entry
(the same back-out _implied_sigma_from_quote uses) and blocks it when the corrected
model no longer clears the market-edge floor.
"""
import csv
import json
import math
import sys
from statistics import NormalDist

ND = NormalDist()

PARAMS = {
    "time_min": 2.5, "time_max": 9.0,
    "entry_min_cents": 20.0, "entry_max_cents": 85.0,
    "tail_min_side_p": {"s1": 0.25, "s2": 0.20},
    "max_gap": 0.15,
    "min_market_edge": 0.04,
    "min_btc_ret": 0.0010,
}


def load_rows(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                r["_sig"] = json.loads(r.get("entry_signals") or "{}")
            except Exception:
                r["_sig"] = {}
            r["_pnl"] = float(r.get("pnl_dollars") or 0.0)
            r["_new_era"] = ("z" in r["_sig"]) or ("residual" in r["_sig"])
            rows.append(r)
    return rows


def _implied_sigma15(row):
    """Back the 15-min sigma out of the entry quote, or None (mirrors the live filter
    minus the spread/mid bounds - offline we want the estimate wherever it exists)."""
    sig = row["_sig"]
    try:
        spot = float(row["btc_price_at_entry"])
        strike = float(row["strike"])
        secs = float(row["seconds_left_at_entry"])
        mkt_p_side = float(sig["mkt_p"])
        p_yes = mkt_p_side if row["side"] == "yes" else 1.0 - mkt_p_side
        p_yes = min(max(p_yes, 0.01), 0.99)
        z = ND.inv_cdf(p_yes)
        if abs(z) < 0.10:
            return None
        ps = math.log(spot / strike) / z
        if ps <= 0:
            return None
        return ps / math.sqrt(max(1.0 / 900.0, secs / 900.0))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def evaluate_new_gates(row, params=PARAMS):
    """Return the list of v3 gates that would have blocked this exported trade row."""
    sig = row["_sig"]
    strategy = "s1" if "residual" in sig else "s2"
    blocked = []

    mins_left = sig.get("mins_left")
    if mins_left is None:
        try:
            mins_left = float(row["seconds_left_at_entry"]) / 60.0
        except (TypeError, ValueError):
            mins_left = None
    if mins_left is not None and not (params["time_min"] <= mins_left <= params["time_max"]):
        blocked.append("time_window")

    try:
        entry = float(row["entry_price_cents"])
        if not (params["entry_min_cents"] <= entry <= params["entry_max_cents"]):
            blocked.append("entry_band")
    except (TypeError, ValueError):
        pass

    mkt_p = sig.get("mkt_p")
    if mkt_p is not None and mkt_p < params["tail_min_side_p"][strategy]:
        blocked.append("tail_ban")

    raw = row.get("raw_p_yes")
    if raw not in (None, "") and mkt_p is not None:
        raw_side = float(raw) if row["side"] == "yes" else 1.0 - float(raw)
        if abs(raw_side - mkt_p) > params["max_gap"]:
            blocked.append("tgtbt")

    if strategy == "s1":
        resid = sig.get("residual")
        btc_ret = sig.get("btc_ret")
        if resid is not None and btc_ret is not None and resid * btc_ret <= 0:
            blocked.append("s1_fade")
        if btc_ret is not None and abs(btc_ret) < params["min_btc_ret"]:
            blocked.append("s1_btc_flat")

    # Vol anchor: reprice with the sigma implied by the entry quote itself. If the
    # corrected model no longer beats the market by the edge floor, the "signal" was
    # sigma error and the v3 engine would never have produced it.
    s15 = _implied_sigma15(row)
    if s15 is not None and mkt_p is not None:
        try:
            spot = float(row["btc_price_at_entry"])
            strike = float(row["strike"])
            secs = float(row["seconds_left_at_entry"])
            if strategy == "s1" and sig.get("predicted_spot"):
                spot = float(sig["predicted_spot"])
            ps = s15 * math.sqrt(max(1.0 / 900.0, secs / 900.0))
            p_yes = ND.cdf(math.log(spot / strike) / ps)
            p_side = p_yes if row["side"] == "yes" else 1.0 - p_yes
            if p_side - mkt_p < params["min_market_edge"]:
                blocked.append("vol_anchor")
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    return blocked


def replay(rows, params=PARAMS):
    """Gate every new-era row; return (results, summary). results = (row, blocked_by)."""
    results = []
    gate_counts = {}
    surviving_pnl = 0.0
    blocked_pnl = 0.0
    winners_surviving = 0
    for r in rows:
        if not r["_new_era"] or r.get("outcome") not in ("win", "loss"):
            continue
        blocked = evaluate_new_gates(r, params)
        results.append((r, blocked))
        if blocked:
            blocked_pnl += r["_pnl"]
            for g in blocked:
                gate_counts[g] = gate_counts.get(g, 0) + 1
        else:
            surviving_pnl += r["_pnl"]
            if r.get("outcome") == "win":
                winners_surviving += 1
    summary = {
        "n": len(results),
        "blocked": sum(1 for _, b in results if b),
        "surviving_pnl": surviving_pnl,
        "blocked_pnl": blocked_pnl,
        "gate_counts": gate_counts,
        "winners_surviving": winners_surviving,
    }
    return results, summary


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    rows = load_rows(argv[1])
    new_era = [r for r in rows if r["_new_era"]]
    results, s = replay(rows)
    print(f"rows: {len(rows)} total, {len(new_era)} new-era (gated), "
          f"{len(rows) - len(new_era)} old-era (ignored)")
    print(f"original new-era P&L: ${sum(r['_pnl'] for r in new_era):+.2f}")
    print(f"blocked {s['blocked']}/{s['n']} trades holding ${s['blocked_pnl']:+.2f} of P&L")
    print(f"surviving P&L: ${s['surviving_pnl']:+.2f} "
          f"({s['winners_surviving']} of the surviving trades won)")
    print("\nblocks per gate (a trade can hit several):")
    for g, n in sorted(s["gate_counts"].items(), key=lambda kv: -kv[1]):
        print(f"  {g:12s} {n}")
    print("\nper-trade matrix:")
    for r, blocked in results:
        tag = ",".join(blocked) if blocked else "SURVIVES"
        print(f"  {r['id']:>5s} {r['asset']:4s} {r['side']:3s} "
              f"{float(r['entry_price_cents']):3.0f}c pnl={r['_pnl']:+8.2f} {r['outcome']:4s}  {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
