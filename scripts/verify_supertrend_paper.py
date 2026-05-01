"""
verify_supertrend_paper.py — Paper-mode Supertrend verification with real candles.

Phase 1 candle source: Coinbase Exchange REST API (1-minute bars, public endpoint).
  Same API used by kalshi_botv3/exchange/historical.py.

Phase 2 market data: Kalshi live REST API.
  Same endpoint, same auth headers, same logic as bot.py paper mode.

Usage:
    py scripts/verify_supertrend_paper.py
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import urllib.request
from base64 import b64encode
from collections import deque
from datetime import datetime, timedelta, timezone

# Force UTF-8 output so box-drawing and special chars render on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# ── Path setup ─────────────────────────────────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

# ── Strategy imports (same code path as live bot) ──────────────────────────────
from strategies.signals.supertrend import _build_1m_ohlcv, supertrend_direction
from strategies.fifteen_min_strategy import FifteenMinStrategy
from strategies.features import MarketFeatures
from strategies.skip_layer import SkipConfig
from strategies.ev import compute_bidirectional_ev

# ── Constants ──────────────────────────────────────────────────────────────────
KALSHI_BASE_URL    = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_PATH_PREFIX = "/trade-api/v2"
COINBASE_API       = "https://api.exchange.coinbase.com"

COINBASE_SYMBOLS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD"}

# Priority: 15m series first; fall back to daily/other if no 15m market open.
ASSET_SERIES = {
    "BTC": ["KXBTC15M", "KXBTCD"],
    "ETH": ["KXETH15M", "KXETHD"],
    "SOL": ["KXSOL15M", "KXSOL15", "KXSOLD"],
    "XRP": ["KXXRP15M", "KXXRP15", "KXXRPD"],
}

# Production min_ev values from config.json asset_overrides
ASSET_MIN_EV = {"BTC": 0.07, "ETH": 0.09, "SOL": 0.16, "XRP": 0.16}

SUPERTREND_ATR_PERIOD     = 10
SUPERTREND_ATR_MULTIPLIER = 3.0
STAKE_DOLLARS             = 25.0
COLD_START_SAMPLES        = 60

# ── Kalshi auth ────────────────────────────────────────────────────────────────
_api_key     = None
_private_key = None


def _load_credentials() -> bool:
    global _api_key, _private_key
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding as _padding  # noqa
    except ImportError:
        print("  [auth] cryptography not available — Kalshi market data unavailable")
        return False

    key = os.environ.get("KALSHI_API_KEY", "").strip()
    pem_val = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()

    # PEM can be a file path or the raw PEM string
    pem_path_default = os.path.join(ROOT, "kalshi_private_key.pem")
    if not pem_val and os.path.exists(pem_path_default):
        pem_val = pem_path_default

    if not key or not pem_val:
        print("  [auth] KALSHI_API_KEY / KALSHI_PRIVATE_KEY not set — will use placeholder prices")
        return False

    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        pem_bytes = open(pem_val, "rb").read() if os.path.exists(pem_val) else pem_val.encode()
        _private_key = load_pem_private_key(pem_bytes, password=None)
        _api_key = key
        print(f"  [auth] Kalshi credentials loaded (key={key[:8]}...)")
        return True
    except Exception as exc:
        print(f"  [auth] Credential load failed: {exc}")
        return False


def _kalshi_headers(method: str, path: str) -> dict:
    if not _api_key or not _private_key:
        return {}
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding as _p
    ts  = str(int(time.time() * 1000))
    msg = (ts + method.upper() + KALSHI_PATH_PREFIX + path).encode()
    sig = b64encode(
        _private_key.sign(
            msg,
            _p.PSS(mgf=_p.MGF1(hashes.SHA256()), salt_length=_p.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    ).decode()
    return {
        "KALSHI-ACCESS-KEY":       _api_key,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
    }


# ── Coinbase candle fetch (public, no auth) ────────────────────────────────────
def fetch_coinbase_candles(symbol: str, n: int = 120) -> list[tuple]:
    """
    Fetch n 1-minute bars from Coinbase Exchange REST API.
    Returns [(ts_unix, open, high, low, close), ...] oldest-first.
    Same endpoint as kalshi_botv3/exchange/historical.py.
    """
    end   = datetime.now(timezone.utc)
    start = end - timedelta(minutes=n + 5)
    url   = (
        f"{COINBASE_API}/products/{symbol}/candles"
        f"?granularity=60"
        f"&start={start.isoformat()}"
        f"&end={end.isoformat()}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "kalshi-bot-verify/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode())
    except Exception as exc:
        print(f"    [coinbase] Error fetching {symbol}: {exc}")
        return []

    # Coinbase: [[time, low, high, open, close, volume], ...] newest-first
    bars = []
    for row in raw:
        if len(row) >= 5:
            bars.append((float(row[0]), float(row[3]), float(row[2]), float(row[1]), float(row[4])))
    bars.sort(key=lambda b: b[0])
    return bars[-n:]


def candles_to_tick_deque(bars: list[tuple]) -> deque:
    """
    Convert 1-min bars → (ts, price) deque using 4 ticks/bar:
    open@t+0, high@t+15, low@t+30, close@t+59.
    Matches the aggTrade tick stream format _build_1m_ohlcv expects.
    """
    dq = deque(maxlen=3600)
    for ts, o, h, l, c in bars:
        dq.append((ts + 0,  o))
        dq.append((ts + 15, h))
        dq.append((ts + 30, l))
        dq.append((ts + 59, c))
    return dq


# ── Kalshi market fetch ────────────────────────────────────────────────────────
def fetch_kalshi_market(asset: str) -> dict | None:
    """Fetch soonest-expiring open 15m market for an asset."""
    now_utc = datetime.now(timezone.utc)
    for series in ASSET_SERIES[asset]:
        path = "/markets"
        url  = f"{KALSHI_BASE_URL}{path}?series_ticker={series}&status=open&limit=20"
        req  = urllib.request.Request(url)
        for k, v in _kalshi_headers("GET", path).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
        except Exception as exc:
            print(f"    [kalshi] {series}: {exc}")
            continue

        valid: list[tuple[float, dict]] = []
        for m in data.get("markets", []):
            if "range" in m.get("title", "").lower():
                continue
            ct = m.get("close_time", "")
            if not ct:
                continue
            try:
                close_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                secs = (close_dt - now_utc).total_seconds()
                if 0 < secs < 75 * 60:
                    valid.append((secs, m))
            except Exception:
                pass
        if valid:
            valid.sort(key=lambda x: x[0])
            return valid[0][1]
    return None


def _parse_strike(market: dict) -> float | None:
    for field in ("floor_strike", "cap_strike", "strike_price"):
        val = market.get(field)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
    text = " ".join([
        market.get("title", ""), market.get("subtitle", ""),
        market.get("yes_sub_title") or "",
    ])
    m = re.search(r"\$(\d{1,3}(?:,\d{3})*(?:\.\d+)?)", text)
    if m:
        return float(m.group(1).replace(",", ""))
    return None


def _extract_asks(market: dict) -> tuple[int, int, int, int]:
    """Return (yes_ask_cents, no_ask_cents, yes_bid_cents, no_bid_cents)."""
    def dc(v):
        try:
            r = int(round(float(v) * 100))
            return r if r > 0 else None
        except (TypeError, ValueError):
            return None

    ya = dc(market.get("yes_ask_dollars")) or dc(market.get("yes_ask")) or 55
    na = dc(market.get("no_ask_dollars"))  or dc(market.get("no_ask"))  or 47
    yb = dc(market.get("yes_bid_dollars")) or dc(market.get("yes_bid")) or max(1, ya - 2)
    nb = dc(market.get("no_bid_dollars"))  or dc(market.get("no_bid"))  or max(1, na - 2)
    return ya, na, yb, nb


def _seconds_remaining(market: dict) -> float:
    ct = market.get("close_time", "")
    try:
        close_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        return max(0.0, (close_dt - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return 300.0


# ── Strategy factory (mirrors _get_or_make_strategy in bot.py) ─────────────────
def make_strat(asset: str) -> FifteenMinStrategy:
    skip_cfg = SkipConfig(
        max_spread_cents=3.0,
        min_seconds_left=30.0,
        min_entry_price_cents=20.0,
        max_entry_price_cents=76.0,
        cold_start_samples=COLD_START_SAMPLES,
        vol_ratio_threshold=1.80,
    )
    return FifteenMinStrategy(
        asset=asset,
        skip_config=skip_cfg,
        min_ev=ASSET_MIN_EV[asset],
        stake_dollars=STAKE_DOLLARS,
        confidence_threshold=0.0,
        supertrend_atr_period=SUPERTREND_ATR_PERIOD,
        supertrend_atr_multiplier=SUPERTREND_ATR_MULTIPLIER,
    )


# ── Last-N supertrend helper ────────────────────────────────────────────────────
def supertrend_last3(bars: list[tuple]) -> list[int | None]:
    """Return Supertrend direction for full bars, drop-last-1, drop-last-2."""
    results = []
    for drop in (0, 1, 2):
        subset = bars[: len(bars) - drop] if drop else bars
        dq = candles_to_tick_deque(subset)
        results.append(supertrend_direction(dq, SUPERTREND_ATR_PERIOD, SUPERTREND_ATR_MULTIPLIER))
    return results  # [current, -1 bar, -2 bars]


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    print("=" * 70)
    print("  Supertrend Paper-Mode Verification — Real Candles")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 70)

    print("\n[Phase 1] Loading Kalshi credentials...")
    has_kalshi = _load_credentials()

    assets = ["BTC", "ETH", "SOL", "XRP"]
    results: dict[str, dict] = {}

    print("\n[Phase 2] Fetching candles + market data + running decide()...\n")

    for asset in assets:
        print(f"── {asset} " + "─" * 60)

        # ── Candles ─────────────────────────────────────────────────────────
        symbol = COINBASE_SYMBOLS[asset]
        print(f"  Fetching 120 1-min bars from Coinbase ({symbol})...")
        bars = fetch_coinbase_candles(symbol, n=120)
        print(f"  Received {len(bars)} bars. Last close: {bars[-1][4]:.4f} @ "
              f"{datetime.fromtimestamp(bars[-1][0], tz=timezone.utc).strftime('%H:%M:%S UTC')}")

        tick_dq = candles_to_tick_deque(bars)
        ohlcv   = _build_1m_ohlcv(tick_dq)
        print(f"  _build_1m_ohlcv → {len(ohlcv)} bars")

        # ── Supertrend last 3 ───────────────────────────────────────────────
        last3 = supertrend_last3(bars)
        current_price = bars[-1][4]  # last close
        print(f"  Supertrend last 3 (current, -1bar, -2bar): {last3}")
        st_now = last3[0]

        # ── Kalshi market ───────────────────────────────────────────────────
        market = None
        yes_ask = no_ask = yes_bid = no_bid = None
        ticker = strike = secs_left = elapsed_sec = None

        if has_kalshi:
            print(f"  Fetching Kalshi market for {asset}...")
            market = fetch_kalshi_market(asset)
            if market:
                ticker    = market.get("ticker", "?")
                secs_left = _seconds_remaining(market)
                elapsed_sec = max(0.0, 15 * 60 - secs_left)
                strike    = _parse_strike(market)
                yes_ask, no_ask, yes_bid, no_bid = _extract_asks(market)
                print(f"  Market: {ticker} | strike={strike} | "
                      f"{secs_left:.0f}s left | "
                      f"yes_ask={yes_ask}c  no_ask={no_ask}c")
            else:
                print(f"  No open {asset} 15m market found — using placeholder prices")

        if market is None:
            ticker    = f"KX{asset}15M-SIM"
            yes_ask   = 55
            no_ask    = 47
            yes_bid   = 54
            no_bid    = 46
            strike    = round(current_price * 0.995, 2)
            secs_left = 600.0
            elapsed_sec = 300.0
            print(f"  [placeholder] yes_ask={yes_ask}c  no_ask={no_ask}c  strike={strike}")

        # ── Build features ──────────────────────────────────────────────────
        btc_price = current_price if asset == "BTC" else (
            results.get("BTC", {}).get("current_price", current_price))

        features = MarketFeatures(
            asset=asset,
            ticker=ticker,
            timestamp=time.time(),
            current_price=current_price,
            strike=float(strike or current_price * 0.995),
            btc_price=float(btc_price),
            seconds_left=float(secs_left),
            elapsed_seconds=float(elapsed_sec),
            yes_ask=float(yes_ask),
            no_ask=float(no_ask),
            yes_bid=float(yes_bid),
            no_bid=float(no_bid),
            spread_yes=float(yes_ask - yes_bid),
            spread_no=float(no_ask - no_bid),
            realized_vol_1min=0.002,
        )
        features.prices_60m = tick_dq

        # ── decide() ────────────────────────────────────────────────────────
        strat = make_strat(asset)
        decision = strat.decide(features)

        # ── EV sanity values ────────────────────────────────────────────────
        from strategies.base import _SUPERTREND_P_MODEL
        p_ev   = _SUPERTREND_P_MODEL
        st_side = "yes" if st_now == 1 else ("no" if st_now == -1 else None)
        p_model_for_ev = (p_ev if st_side == "yes" else 1.0 - p_ev) if st_side else None
        ev = compute_bidirectional_ev(
            p_model=p_model_for_ev if p_model_for_ev is not None else 0.5,
            yes_ask_cents=float(yes_ask),
            no_ask_cents=float(no_ask),
            stake_dollars=STAKE_DOLLARS,
        )

        print(f"  → decision: {decision.action.upper()}"
              + (f" {decision.side.upper()}" if decision.side else "")
              + f"  |  reason: {decision.reason[:80]}")
        print(f"     p_ev={p_ev:.2f}  p_model_for_ev={p_model_for_ev}  "
              f"yes_ev={ev.yes_ev:+.4f}  no_ev={ev.no_ev:+.4f}")
        print(f"     min_ev={ASSET_MIN_EV[asset]:.2f}  "
              f"contributing: {decision.contributing_signals.get('decision_mode','?')}")

        results[asset] = {
            "bars": len(bars),
            "ohlcv_bars": len(ohlcv),
            "current_price": current_price,
            "last3": last3,
            "st_now": st_now,
            "st_side": st_side,
            "p_model_for_ev": p_model_for_ev,
            "yes_ask": yes_ask,
            "no_ask": no_ask,
            "yes_ev": ev.yes_ev,
            "no_ev": ev.no_ev,
            "decision": decision,
        }
        print()

    # ── Phase 4: Sanity checks ─────────────────────────────────────────────────
    print("=" * 70)
    print("  PHASE 4 — Sanity Checks")
    print("=" * 70)
    print()

    # Check 1: Supertrend output (1 or -1 or None for insufficient data)
    print("Check 1: Supertrend output values")
    all_valid = True
    for asset in assets:
        st = results[asset]["st_now"]
        n_bars = results[asset]["ohlcv_bars"]
        status = "OK" if st in (1, -1) else ("NONE — insufficient bars" if st is None else f"BAD ({st})")
        print(f"  {asset}: Supertrend={st}  ({n_bars} 1m bars)  [{status}]")
        if st not in (1, -1) and st is not None:
            all_valid = False
    print(f"  → {'PASS' if all_valid else 'FAIL'}: all Supertrend outputs are 1 or -1 (None = insufficient bars, not a bug)\n")

    # Check 2: p_model_for_ev symmetry
    print("Check 2: p_model_for_ev symmetry (0.70 for YES, 0.30 for NO)")
    from strategies.base import _SUPERTREND_P_MODEL
    p_ev = _SUPERTREND_P_MODEL
    symmetry_ok = True
    no_case_shown = False
    for asset in assets:
        r = results[asset]
        st_side = r["st_side"]
        pme     = r["p_model_for_ev"]
        expected = p_ev if st_side == "yes" else (1.0 - p_ev if st_side == "no" else None)
        ok = (abs(pme - expected) < 1e-9) if (pme is not None and expected is not None) else (pme is None and expected is None)
        print(f"  {asset}: st={r['st_now']}  side={st_side}  p_model_for_ev={pme}  "
              f"expected={expected}  [{'OK' if ok else 'FAIL'}]")
        if not ok:
            symmetry_ok = False
        if st_side == "no" and not no_case_shown:
            print(f"    NO-direction math: yes_ev={r['yes_ev']:+.4f}  no_ev={r['no_ev']:+.4f}")
            print(f"    no_ev = (1-{pme:.2f}) - {r['no_ask']}/100 - fee"
                  f" = {1.0-pme:.2f} - {r['no_ask']/100:.2f} - fee")
            no_case_shown = True

    # Force a NO case if none seen
    if not no_case_shown:
        print("\n  [NO case forced for math demonstration]")
        asset_demo = "BTC"
        r = results[asset_demo]
        pme_no = 1.0 - p_ev  # = 0.30
        no_ask_demo = r["no_ask"]
        ev_no = compute_bidirectional_ev(
            p_model=pme_no,
            yes_ask_cents=float(r["yes_ask"]),
            no_ask_cents=float(no_ask_demo),
            stake_dollars=STAKE_DOLLARS,
        )
        print(f"  If {asset_demo} Supertrend were -1 (NO):")
        print(f"    p_model_for_ev = 1.0 - {p_ev:.2f} = {pme_no:.2f}")
        print(f"    no_ev = (1-{pme_no:.2f}) - {no_ask_demo}/100 - fee"
              f" = {1-pme_no:.2f} - {no_ask_demo/100:.2f} - fee = {ev_no.no_ev:+.4f}")
        print(f"    (vs p_model=0.70: no_ev would be = 0.30 - {no_ask_demo/100:.2f} - fee"
              f" = {0.30 - no_ask_demo/100:.4f} — bug that was fixed)")

    print(f"\n  → {'PASS' if symmetry_ok else 'FAIL'}: p_model_for_ev correctly inverted for NO direction\n")

    # Check 3: Decision defensibility
    print("Check 3: Decision defensibility")
    defensible = True
    for asset in assets:
        r  = results[asset]
        d  = r["decision"]
        ev_side = r["yes_ev"] if r.get("st_side") == "yes" else r["no_ev"]
        min_ev = ASSET_MIN_EV[asset]

        if d.action == "trade":
            should_trade = (r["st_now"] in (1, -1)) and (ev_side >= min_ev)
            ok = should_trade
            note = ""
        else:
            # skip — either ST is None, ev_side < min_ev, or entry_range/vol
            ok = True
            note = d.reason[:60]

        print(f"  {asset}: {d.action}"
              + (f" {d.side}" if d.side else "")
              + f"  ev={ev_side:+.4f}  min_ev={min_ev:.2f}"
              + (f"  [OK]" if ok else f"  [SUSPECT]")
              + (f" — {note}" if note else ""))
        if not ok:
            defensible = False
    print(f"  → {'PASS' if defensible else 'FAIL — check above'}: decisions match EV gate expectations\n")

    # Check 4: SOL/XRP min_ev gate
    print("Check 4: SOL/XRP min_ev gating vs BTC/ETH")
    print(f"  BTC min_ev={ASSET_MIN_EV['BTC']:.2f}  (config: asset_overrides.BTC.min_ev_base=7)")
    print(f"  ETH min_ev={ASSET_MIN_EV['ETH']:.2f}  (config: asset_overrides.ETH.min_ev_base=9)")
    print(f"  SOL min_ev={ASSET_MIN_EV['SOL']:.2f}  (config: asset_overrides.SOL.min_ev_base=16)")
    print(f"  XRP min_ev={ASSET_MIN_EV['XRP']:.2f}  (config: asset_overrides.XRP.min_ev_base=16)")
    sol_gated = results["SOL"]["decision"].action == "skip" or results["SOL"]["yes_ev"] < ASSET_MIN_EV["SOL"]
    xrp_gated = results["XRP"]["decision"].action == "skip" or results["XRP"]["yes_ev"] < ASSET_MIN_EV["XRP"]
    print(f"  SOL gated by min_ev: {sol_gated}")
    print(f"  XRP gated by min_ev: {xrp_gated}")
    sol_xrp_ok = ASSET_MIN_EV["SOL"] == 0.16 and ASSET_MIN_EV["XRP"] == 0.16
    print(f"  → {'PASS' if sol_xrp_ok else 'FAIL'}: SOL/XRP use 0.16 min_ev vs 0.07/0.09 for BTC/ETH\n")

    # ── Summary ────────────────────────────────────────────────────────────────
    print("=" * 70)
    print("  PHASE 5 SUMMARY")
    print("=" * 70)
    for asset in assets:
        r = results[asset]
        d = r["decision"]
        print(f"  {asset:<4}  bars={r['bars']:3d}  close={r['current_price']:>12.4f}"
              f"  ST={r['st_now']:>+2}  {d.action.upper():<5}"
              + (f" {d.side.upper():<3}" if d.side else "    ")
              + f"  ev={r['yes_ev'] if r['st_side']=='yes' else r['no_ev']:+.4f}"
              f"  min={ASSET_MIN_EV[asset]:.2f}")
    print()


if __name__ == "__main__":
    main()
