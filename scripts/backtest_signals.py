"""
scripts/backtest_signals.py - test intra-window signal theses on candles + settlements.

Joins data/historical/{ASSET}_candles_1m.parquet (from scripts/fetch_candles.py) to
{ASSET}_kalshi_settlements.parquet and asks, against every settled 15-min window:

  A. S1 MOMENTUM: after controlling for strike distance (z), does the side of the
     recent 3-min move settle MORE often than the drift-free Bachelier probability
     says? Positive excess = momentum continuation is real; ~zero = S1's thesis
     adds nothing over the market's own pricing; negative = momentum anti-predicts.
  B. S4 STALL-FADE: when a >=1.5-sigma run has stalled, does fading it beat the
     model probability of the fade side?
  C. FAVORITE CALIBRATION (S2/S8): across model-probability deciles, does the
     favorite side settle more often than the model says (favorites underpriced)?

The candles are exchange spot, not Kalshi contract prices, so results measure
SIGNAL QUALITY vs a fair-value model - not fee-inclusive P&L. A signal that can't
beat the model here has no business paying taker fees on it live.

Offline analysis only - NOT loaded at runtime; pandas/pyarrow deliberately not bot
dependencies.

    python scripts/backtest_signals.py [data/historical]
"""
import glob
import math
import os
import sys

try:
    import pandas as pd
except ImportError:
    print("pandas required"); sys.exit(1)

_DECISION_MINS = (10, 7, 5, 3)   # minutes-left checkpoints per window
_VOL_LOOKBACK_MIN = 60
_MIN_VOL_RETURNS = 30
_MOM_LOOKBACK_MIN = 3


def _phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def wilson_lb(w, n, z=1.96):
    if n == 0:
        return 0.0
    p = w / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    adj = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - adj) / denom


def load_closes(path):
    """Candle parquet -> {epoch_minute_start: close}. Keyed by the candle's OWN
    start minute; spot_at() shifts back one minute so lookups never see a candle
    that hadn't closed yet at decision time."""
    df = pd.read_parquet(path)
    return dict(zip((df["ts"].astype("int64") // 10**9).tolist(), df["close"].tolist()))


def spot_at(closes, epoch):
    """Last COMPLETED candle close strictly before `epoch` (minute-aligned).
    The candle starting at epoch-60 covers [epoch-60, epoch) - its close is the
    freshest print available without lookahead. Walks back up to 5 min over gaps."""
    base = int(epoch) // 60 * 60
    for k in range(1, 6):
        c = closes.get(base - 60 * k)
        if c is not None and c > 0:
            return c
    return None


def sigma15_at(closes, epoch, lookback_min=_VOL_LOOKBACK_MIN):
    """Trailing realized 15-min sigma (fractional) from 1-min log returns ending
    strictly before `epoch`. None when fewer than _MIN_VOL_RETURNS valid pairs."""
    base = int(epoch) // 60 * 60
    prev, sum_r2, n = None, 0.0, 0
    for k in range(lookback_min, 0, -1):
        c = closes.get(base - 60 * k)
        if c is None or c <= 0:
            prev = None
            continue
        if prev is not None:
            r = math.log(c / prev)
            sum_r2 += r * r
            n += 1
        prev = c
    if n < _MIN_VOL_RETURNS:
        return None
    return math.sqrt((sum_r2 / n) * 15.0)


def build_rows(closes, settlements):
    """One row per (window, minutes-left checkpoint) with spot/z/model-p/momentum.
    settlements: DataFrame with close_time (tz-aware), window_open, strike, result."""
    rows = []
    for w_open, w_close, strike, result in zip(
            settlements["window_open"], settlements["close_time"],
            settlements["strike"], settlements["result"]):
        close_ep = int(w_close.timestamp())
        open_ep = int(w_open.timestamp())
        spot_open = spot_at(closes, open_ep + 60)   # first in-window print
        for mins in _DECISION_MINS:
            ep = close_ep - mins * 60
            spot = spot_at(closes, ep)
            sigma = sigma15_at(closes, ep)
            if spot is None or sigma is None or sigma <= 0 or strike <= 0:
                continue
            z = (spot - strike) / (spot * sigma * math.sqrt(mins / 15.0))
            p_yes = _phi(z)
            mom_ref = spot_at(closes, ep - _MOM_LOOKBACK_MIN * 60)
            mom = math.log(spot / mom_ref) if mom_ref else None
            mom_z = (mom / (sigma * math.sqrt(_MOM_LOOKBACK_MIN / 15.0))
                     if mom is not None else None)
            run_z = None
            if spot_open and spot_open > 0:
                elapsed = max(1.0, (ep - open_ep) / 60.0)
                run_z = (math.log(spot / spot_open)
                         / (sigma * math.sqrt(elapsed / 15.0)))
            rows.append({"mins": mins, "z": z, "p_yes": p_yes, "mom_z": mom_z,
                         "run_z": run_z, "result": int(result)})
    return pd.DataFrame(rows)


def momentum_report(df):
    print("\n=== A. S1 momentum: does the 3-min move's side beat the model? ===")
    print("excess = actual win rate of the momentum side MINUS its mean model prob;")
    print("LB-excess uses the Wilson lower bound of the actual rate. Positive = edge.")
    print(f"{'mins':>4} {'|mom_z|':>9} {'n':>6} {'actual':>7} {'model':>6} "
          f"{'excess':>7} {'LB-excess':>9}")
    for mins in (10, 7, 5):
        sub = df[(df["mins"] == mins) & df["mom_z"].notna()]
        for lo, hi, label in ((0.3, 0.7, "0.3-0.7"), (0.7, 1.5, "0.7-1.5"),
                              (1.5, 99.0, ">=1.5")):
            d = sub[(sub["mom_z"].abs() >= lo) & (sub["mom_z"].abs() < hi)]
            n = len(d)
            if n == 0:
                continue
            side_win = ((d["mom_z"] > 0) == (d["result"] == 1))
            model = d["p_yes"].where(d["mom_z"] > 0, 1.0 - d["p_yes"])
            w = int(side_win.sum())
            flag = "" if n >= 1000 else "  (thin)"
            print(f"{mins:>4} {label:>9} {n:>6} {w / n:>7.3f} {model.mean():>6.3f} "
                  f"{w / n - model.mean():>+7.3f} {wilson_lb(w, n) - model.mean():>+9.3f}{flag}")


def stall_fade_report(df):
    print("\n=== B. S4 stall-fade: fade a stalled >=1.5-sigma run ===")
    print(f"{'mins':>4} {'n':>6} {'actual':>7} {'model':>6} {'excess':>7} {'LB-excess':>9}")
    for mins in (7, 5, 3):
        sub = df[(df["mins"] == mins) & df["run_z"].notna() & df["mom_z"].notna()]
        # Run extended but the last 3 min went quiet: |run| big, |mom| small.
        d = sub[(sub["run_z"].abs() >= 1.5) & (sub["mom_z"].abs() < 0.25)]
        n = len(d)
        if n == 0:
            continue
        fade_win = ((d["run_z"] > 0) == (d["result"] == 0))   # fade side wins
        model = (1.0 - d["p_yes"]).where(d["run_z"] > 0, d["p_yes"])
        w = int(fade_win.sum())
        flag = "" if n >= 1000 else "  (thin)"
        print(f"{mins:>4} {n:>6} {w / n:>7.3f} {model.mean():>6.3f} "
              f"{w / n - model.mean():>+7.3f} {wilson_lb(w, n) - model.mean():>+9.3f}{flag}")


def calibration_report(df):
    print("\n=== C. Favorite calibration at 5 min left (S2/S8) ===")
    print("empirical > model in the high deciles = favorites resolve MORE often than")
    print("fair value implies (supports favorite-buying); below = they are overpriced.")
    sub = df[df["mins"] == 5]
    print(f"{'model p':>12} {'n':>6} {'empirical':>9} {'diff':>7}")
    for lo in [x / 10 for x in range(0, 10)]:
        d = sub[(sub["p_yes"] >= lo) & (sub["p_yes"] < lo + 0.1)]
        n = len(d)
        if n == 0:
            continue
        emp = d["result"].mean()
        flag = "" if n >= 500 else "  (thin)"
        print(f"{lo:>5.1f}-{lo + 0.1:<5.1f} {n:>6} {emp:>9.3f} "
              f"{emp - d['p_yes'].mean():>+7.3f}{flag}")


def main():
    data_dir = sys.argv[1] if len(sys.argv) > 1 else "data/historical"
    settles = sorted(glob.glob(os.path.join(data_dir, "*_kalshi_settlements.parquet")))
    frames = []
    for spath in settles:
        asset = os.path.basename(spath).split("_")[0]
        cpath = os.path.join(data_dir, f"{asset}_candles_1m.parquet")
        if not os.path.exists(cpath):
            print(f"{asset}: no candle parquet - run scripts/fetch_candles.py first")
            continue
        closes = load_closes(cpath)
        settlements = pd.read_parquet(spath)
        rows = build_rows(closes, settlements)
        rows["asset"] = asset
        frames.append(rows)
        print(f"{asset}: {len(rows)} decision rows from {len(settlements)} windows")
    if not frames:
        sys.exit(1)
    df = pd.concat(frames, ignore_index=True)
    momentum_report(df)
    stall_fade_report(df)
    calibration_report(df)
    print("\nRead: a thesis is only actionable when LB-excess stays positive at a cell"
          "\nwith n >= 1000 - anything thinner or model-hugging is noise, and taker fees"
          "\n(~3.5c round trip at mid prices) still have to come out of it.")


if __name__ == "__main__":
    main()
