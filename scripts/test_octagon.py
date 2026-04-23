"""
Octagon integration smoke test.
Usage: python scripts/test_octagon.py KXBTCD-26APR2300-T100000 100000
Replace the ticker and strike with a live Kalshi contract.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

from strategies.signals import octagon_client as oc

TICKER = sys.argv[1] if len(sys.argv) > 1 else "KXBTCD-26APR2300-T100000"
STRIKE = float(sys.argv[2]) if len(sys.argv) > 2 else 100000.0

print(f"Ticker : {TICKER}")
print(f"Strike : {STRIKE:,.0f}")
event = "-".join(TICKER.split("-")[:-1])
url = oc._build_market_url(TICKER)
print(f"URL    : {url}")
print()

# ── Raw response dump (helps debug parser) ──────────────────────────────────
if url:
    import httpx
    api_key = os.environ.get("OCTAGON_API_KEY", "")
    print("── Raw Octagon response (first 80 lines) " + "─" * 15)
    try:
        r = httpx.post(
            oc.OCTAGON_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": oc.OCTAGON_MODEL, "input": url},
            timeout=30.0,
        )
        data = r.json()
        print(f"  top-level keys : {list(data.keys())}")
        lr = data.get("latest_report")
        print(f"  latest_report  : {type(lr).__name__}  keys={list(lr.keys()) if isinstance(lr, dict) else 'N/A'}")
        if isinstance(lr, dict):
            raw = lr.get("markdown_report") or ""
            print(f"  markdown len   : {len(raw)} chars")
            lines = raw.splitlines()
            table_lines = [ln for ln in lines if ln.count("|") >= 3]
            print(f"  table_lines    : {len(table_lines)} rows with ≥3 pipes")
            print(f"  first 40 lines of markdown:")
            for i, ln in enumerate(lines[:40]):
                print(f"    {ln}")
        print()
    except Exception as e:
        print(f"  [raw dump failed: {e}]")
        print()

print("── Call 1 (expect cache MISS) " + "─" * 30)
prob, agrees, conf, hit = oc.query(TICKER, STRIKE, 55.0, 47.0, "yes", is_15m=True)
print(f"  model_prob          = {prob}")
print(f"  direction_agrees    = {agrees}")
print(f"  confidence          = {conf}")
print(f"  cache_hit           = {hit}  ← should be False")

cached = oc._report_cache.get(event)
if cached:
    table, ts = cached
    print(f"\n── Parsed table ({len(table)} strike row(s)) " + "─" * 20)
    for strike_key, (mkt, mdl) in sorted(table.items()):
        print(f"  strike={strike_key:>9,}   market={mkt:.1%}   model={mdl:.1%}   delta={mdl-mkt:+.1%}")
elif prob is None:
    print("\n  [!] No data cached — API call failed or key missing")
    print(f"  OCTAGON_API_KEY set: {'yes' if os.environ.get('OCTAGON_API_KEY') else 'NO'}")
    sys.exit(1)

print(f"\n── Call 2 (expect cache HIT) " + "─" * 31)
prob2, agrees2, conf2, hit2 = oc.query(TICKER, STRIKE, 55.0, 47.0, "yes", is_15m=True)
print(f"  model_prob          = {prob2}")
print(f"  direction_agrees    = {agrees2}")
print(f"  confidence          = {conf2}")
print(f"  cache_hit           = {hit2}  ← should be True")

print()
if hit2 and prob == prob2:
    print("✓ Octagon integration OK")
else:
    print("✗ Something unexpected — check output above")
