"""
scripts/calibration.py - model self-calibration fitters.

fit_prob_scale: probability-space calibration weight from settled decision_log rows.
fit_basis_offset: per-asset settlement level offset from settlement_basis rows.
load_calibration / save_calibration: data/calibration.json persistence.

All fitters are data-gated (return the neutral value below the sample minimum) and
clamped, so a thin or pathological dataset can never push the model far from its
uncalibrated behavior.
"""
import json
import os
import tempfile

# Fit gates and clamps.
MIN_PROB_SAMPLES = 200
PROB_SCALE_LO = 0.5
PROB_SCALE_HI = 1.2
MIN_BASIS_SAMPLES = 150
BASIS_OFFSET_CAP = 0.0010   # 10bp cap on the fitted level offset
MIN_BETA_PAIRS = 30
BETA_LO = 0.05
BETA_HI = 1.5
# Auto-gate: block a bucket once it has this many settled picks and its
# Wilson-LB net-$/contract is not positive (the GATE-1 kill criterion per bucket).
AUTOGATE_MIN_N = 150

CALIBRATION_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "calibration.json"
)


def fit_prob_scale(rows) -> float:
    """
    Fit w minimizing the Brier score of the recalibrated probability
    0.5 + w*(p - 0.5) against outcomes.

    rows: iterable of (model_p_yes, outcome) with outcome 'yes'/'no'.
    Least squares in w has the closed form w* = sum(d*(y-0.5)) / sum(d^2) with
    d = p - 0.5. Returns 1.0 (no-op) below MIN_PROB_SAMPLES; clamped to
    [PROB_SCALE_LO, PROB_SCALE_HI].
    """
    pts = []
    for p, outcome in rows:
        if p is None or outcome not in ("yes", "no"):
            continue
        try:
            p = float(p)
        except (TypeError, ValueError):
            continue
        if not (0.0 < p < 1.0):
            continue
        pts.append((p - 0.5, 1.0 if outcome == "yes" else 0.0))
    if len(pts) < MIN_PROB_SAMPLES:
        return 1.0
    denom = sum(d * d for d, _ in pts)
    if denom <= 1e-12:
        return 1.0
    w = sum(d * (y - 0.5) for d, y in pts) / denom
    return round(min(PROB_SCALE_HI, max(PROB_SCALE_LO, w)), 3)


def fit_basis_offset(rows) -> float:
    """
    Fit the signed-distance threshold that best separates Kalshi YES from NO settles.

    rows: iterable of (signed_dist, kalshi) where signed_dist = (our_spot-strike)/strike
    at settle and kalshi is the official result. With zero basis the threshold is 0;
    a persistent offset means our spot reference differs from Kalshi's settlement value.
    1-D scan over midpoints of the sorted distances, minimizing misclassifications of
    predict-YES-iff dist > b. Returns 0.0 below MIN_BASIS_SAMPLES; clamped to
    +/- BASIS_OFFSET_CAP.
    """
    pts = []
    for dist, kalshi in rows:
        if dist is None or kalshi not in ("yes", "no"):
            continue
        try:
            dist = float(dist)
        except (TypeError, ValueError):
            continue
        pts.append((dist, 1 if kalshi == "yes" else 0))
    if len(pts) < MIN_BASIS_SAMPLES:
        return 0.0
    pts.sort()
    dists = [d for d, _ in pts]
    candidates = [0.0]
    for i in range(len(dists) - 1):
        if dists[i] != dists[i + 1]:
            candidates.append((dists[i] + dists[i + 1]) / 2.0)
    best_b, best_err = 0.0, float("inf")
    for b in candidates:
        err = sum(1 for d, y in pts if (1 if d > b else 0) != y)
        if err < best_err:
            best_err, best_b = err, b
    return round(min(BASIS_OFFSET_CAP, max(-BASIS_OFFSET_CAP, best_b)), 6)


def _grid_prices(pts, t_start: float, t_end: float, step: float) -> list:
    """
    Sample nearest prices on a fixed time grid from (ts, price) points.
    Returns [price_or_None per grid point]; None where no print lies within step/2.
    """
    out = []
    if not pts:
        return out
    idx = 0
    n = len(pts)
    t = t_start
    while t <= t_end + 1e-9:
        while idx + 1 < n and abs(pts[idx + 1][0] - t) <= abs(pts[idx][0] - t):
            idx += 1
        ts, price = pts[idx]
        out.append(price if abs(ts - t) <= step / 2.0 and price > 0 else None)
        t += step
    return out


def fit_rolling_beta(btc_pts, alt_pts, step: float = 30.0, window: float = 1800.0,
                     now: float | None = None):
    """
    Through-origin regression of alt log-returns on BTC log-returns over a rolling
    window, both sampled on the same step grid so returns are co-timed.

    btc_pts/alt_pts: chronological (ts, price) sequences (the live deques).
    Returns (beta, n_pairs); beta is None when data is thin (n < MIN_BETA_PAIRS)
    or the fit is degenerate. Clamped to [BETA_LO, BETA_HI].
    """
    import math
    btc_pts = list(btc_pts)
    alt_pts = list(alt_pts)
    if not btc_pts or not alt_pts:
        return None, 0
    t_end = min(btc_pts[-1][0], alt_pts[-1][0])
    t_start = t_end - window
    if now is not None:
        t_end = min(t_end, now)
    b_grid = _grid_prices(btc_pts, t_start, t_end, step)
    a_grid = _grid_prices(alt_pts, t_start, t_end, step)
    sxx = sxy = 0.0
    n = 0
    for i in range(1, min(len(b_grid), len(a_grid))):
        b0, b1 = b_grid[i - 1], b_grid[i]
        a0, a1 = a_grid[i - 1], a_grid[i]
        if None in (b0, b1, a0, a1):
            continue
        x = math.log(b1 / b0)
        y = math.log(a1 / a0)
        sxx += x * x
        sxy += x * y
        n += 1
    if n < MIN_BETA_PAIRS or sxx <= 1e-12:
        return None, n
    beta = sxy / sxx
    return round(min(BETA_HI, max(BETA_LO, beta)), 3), n


def compute_auto_blocks(picks, session_for_iso, pnl_stats) -> dict:
    """
    GATE-1 discipline per bucket: from settled PICKS rows (dicts with ts, strategy,
    asset, side, outcome, entry_price_cents), find ET sessions and (strategy, asset)
    pairs whose Wilson-LB net-$/contract is not positive at n >= AUTOGATE_MIN_N.

    session_for_iso / pnl_stats are injected (sessions.session_for_iso and
    edge_report._pnl_stats) to keep this module dependency-free.
    Returns {"sessions": [...], "strategy_assets": [["strategy1","SOL"], ...],
             "stats": {...}} - lists JSON-serializable for calibration.json.
    """
    by_session: dict = {}
    by_sa: dict = {}
    for r in picks:
        key = session_for_iso(r.get("ts"))
        if key:
            by_session.setdefault(key, []).append(r)
        by_sa.setdefault((r.get("strategy"), r.get("asset")), []).append(r)
    blocked_sessions = []
    blocked_sa = []
    stats: dict = {"sessions": {}, "strategy_assets": {}}
    for key, rows in by_session.items():
        st = pnl_stats(rows)
        stats["sessions"][key] = {"n": st["n"], "wlb_pnl": st["wlb_pnl"]}
        if st["n"] >= AUTOGATE_MIN_N and st["wlb_pnl"] is not None and st["wlb_pnl"] <= 0:
            blocked_sessions.append(key)
    for (strategy, asset), rows in by_sa.items():
        if not strategy or not asset:
            continue
        st = pnl_stats(rows)
        stats["strategy_assets"][f"{strategy}/{asset}"] = {"n": st["n"], "wlb_pnl": st["wlb_pnl"]}
        if st["n"] >= AUTOGATE_MIN_N and st["wlb_pnl"] is not None and st["wlb_pnl"] <= 0:
            blocked_sa.append([strategy, asset])
    return {"sessions": sorted(blocked_sessions), "strategy_assets": sorted(blocked_sa),
            "stats": stats}


def load_calibration(path: str = CALIBRATION_PATH) -> dict:
    """Read the persisted calibration, or {} on any error (fail-open to neutral)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_calibration(data: dict, path: str = CALIBRATION_PATH) -> bool:
    """Atomically write the calibration JSON. Returns False on failure (never raises)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".json.tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return False
