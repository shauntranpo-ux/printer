"""
Octagon AI prediction-market research client.

Singleton with two-level cache:
  1. _slug_cache  — event slug per series (permanent, one Kalshi API call per series)
  2. _report_cache — parsed Octagon table per event window (TTL: 900s/15m, 3300s/hourly)

query() is the only public entry point. Returns (None, None, None, False) on any
error or timeout so the caller can fall through to the BV3 fallback.

Report lifecycle:
  - Primary call uses :cache model (fast, free, no credits). Returns None when
    Octagon has no cached report for this market event.
  - On cache miss, a background daemon thread calls :refresh (generates a fresh
    report in ~10-30s, costs credits). Subsequent queries within the same TTL
    window hit the local _report_cache populated by the background thread.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Optional

import httpx

log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

OCTAGON_API_URL    = "https://api.octagonagents.com/v1/responses"
OCTAGON_MODEL      = "octagon-prediction-markets-agent:cache"
OCTAGON_MODEL_REFRESH = "octagon-prediction-markets-agent:refresh"
KALSHI_EVENTS_URL  = "https://api.elections.kalshi.com/trade-api/v2/events/{}"
TIMEOUT_CACHE_S    = 3.0    # :cache is fast (cached lookup)
TIMEOUT_REFRESH_S  = 30.0   # :refresh generates a new report
TTL_15M    = 900    # 15 minutes
TTL_HOURLY = 3300   # 55 minutes

# ── Module-level singletons ──────────────────────────────────────────────────

_http: Optional[httpx.Client] = None

# series_ticker (e.g. "kxbtc15m") → event_slug (e.g. "btc-15-min-target")
_slug_cache: dict[str, str] = {}

# event_ticker → (parsed_table, fetched_at)
# parsed_table: dict[int, tuple[float, float]]  strike_int → (market_pct, model_pct)
_report_cache: dict[str, tuple[dict, float]] = {}

# event_tickers currently being refreshed in background — prevents duplicate threads
_refresh_pending: set[str] = set()

# Tracks call outcomes for dashboard health display
_status: dict = {
    "last_ok_ts":   None,
    "last_fail_ts": None,
    "key_present":  False,
    "calls":        0,   # every HTTP attempt (cache + refresh)
    "hits":         0,   # local cache hits (no HTTP needed)
    "refresh_calls": 0,  # background :refresh calls started
}


def _get_http() -> httpx.Client:
    global _http
    if _http is None:
        _http = httpx.Client(timeout=TIMEOUT_CACHE_S)
    return _http


# ── URL construction ─────────────────────────────────────────────────────────

def _slugify(title: str) -> str:
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def _get_event_slug(series: str, event_ticker: str) -> Optional[str]:
    """Return the slug for the Kalshi event, fetching and caching if needed."""
    if series in _slug_cache:
        return _slug_cache[series]
    try:
        client = _get_http()
        resp = client.get(KALSHI_EVENTS_URL.format(event_ticker))
        resp.raise_for_status()
        title = resp.json()["event"]["title"]
        slug = _slugify(title)
        _slug_cache[series] = slug
        log.debug("Octagon: cached event slug %r → %r", series, slug)
        return slug
    except Exception as exc:
        log.warning("Octagon: failed to fetch event slug for %s: %s", event_ticker, exc)
        return None


def _build_market_url(ticker: str) -> Optional[str]:
    parts = ticker.split("-")
    if len(parts) < 3:
        return None
    series = parts[0].lower()
    event_ticker = "-".join(parts[:-1])
    event_slug = _get_event_slug(series, event_ticker)
    if event_slug is None:
        return None
    return f"https://kalshi.com/markets/{series}/{event_slug}/{event_ticker.lower()}"


# ── Report parsing ────────────────────────────────────────────────────────────

def _extract_text_from_response(data: dict) -> str:
    """Pull the markdown text out of an Octagon API response envelope."""
    try:
        raw_text = data["output"][0]["content"][0]["text"]
    except (KeyError, IndexError):
        return ""

    # Octagon wraps the report in a JSON string (double-encoded).
    # Try to unwrap; fall back to treating raw_text as plain markdown.
    if raw_text.startswith("{"):
        try:
            inner = json.loads(raw_text)
            lr = inner.get("latest_report")
            if isinstance(lr, dict):
                md = lr.get("markdown_report") or ""
                if md:
                    return md
            # If latest_report is absent or empty, the response is a cache-miss
            # envelope — no useful text here.
            return ""
        except (json.JSONDecodeError, AttributeError):
            pass
    # Plain markdown (non-JSON response or :refresh returning markdown directly)
    return raw_text


def _parse_table(text: str) -> dict[int, tuple[float, float]]:
    """
    Extract the markdown prediction table from an Octagon report.
    Returns {strike_int: (market_prob, model_prob)}.

    Handles two outcome-column formats:
      - "BTC above $93,500"  (hourly above/below markets)
      - "Target Price: $93,500.00"  (15m and target markets)
    """
    lines = text.splitlines()
    table_lines = [ln for ln in lines if ln.count("|") >= 3]
    if not table_lines:
        return {}

    # Find all header rows; keep whichever table yields the most strike rows.
    best: dict[int, tuple[float, float]] = {}

    for header_idx, ln in enumerate(table_lines):
        lower = ln.lower()
        if not ("outcome" in lower and "market" in lower and "model" in lower):
            continue

        header_cells = [c.strip().lower() for c in table_lines[header_idx].split("|")]
        try:
            outcome_col = next(i for i, c in enumerate(header_cells) if "outcome" in c)
            market_col  = next(i for i, c in enumerate(header_cells) if "market" in c)
            model_col   = next(i for i, c in enumerate(header_cells) if "model" in c)
        except StopIteration:
            continue

        result: dict[int, tuple[float, float]] = {}
        for data_ln in table_lines[header_idx + 1:]:
            if re.match(r"^\s*\|[-| ]+\|\s*$", data_ln):
                continue  # separator row
            cells = [c.strip() for c in data_ln.split("|")]
            if len(cells) <= max(outcome_col, market_col, model_col):
                continue
            outcome = cells[outcome_col]
            # Accept both "above" and "target price" outcome formats
            outcome_lower = outcome.lower()
            if not any(kw in outcome_lower for kw in ("above", "target price", "target")):
                continue
            try:
                market_pct = float(cells[market_col].replace("%", "").strip()) / 100.0
                model_pct  = float(cells[model_col].replace("%", "").strip()) / 100.0
            except ValueError:
                continue
            cleaned = outcome.replace(",", "").replace("$", "")
            nums = re.findall(r"\d+", cleaned)
            if not nums:
                continue
            # Use the largest number as the strike (filters out small word-numbers like "15")
            strike_int = max(int(n) for n in nums)
            if strike_int < 100:
                continue  # sanity check — no real strike is < $100
            result[strike_int] = (market_pct, model_pct)

        if len(result) > len(best):
            best = result

    return best


# ── Background :refresh ───────────────────────────────────────────────────────

def _background_refresh(event_ticker: str, market_url: str, ttl: int) -> None:
    """
    Called in a daemon thread. Fetches a fresh report via :refresh and stores
    it in _report_cache so the next query() call within the same window can use it.
    """
    try:
        api_key = os.environ.get("OCTAGON_API_KEY", "")
        if not api_key:
            return
        # Use a fresh client for the background thread (httpx.Client is not thread-safe)
        with httpx.Client(timeout=TIMEOUT_REFRESH_S) as client:
            resp = client.post(
                OCTAGON_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": OCTAGON_MODEL_REFRESH, "input": market_url},
            )
            resp.raise_for_status()
            text = _extract_text_from_response(resp.json())

        if not text:
            log.warning("Octagon [:refresh] empty response for %s", event_ticker)
            return

        table = _parse_table(text)
        if table:
            _report_cache[event_ticker] = (table, time.time())
            _status["last_ok_ts"] = time.time()
            log.info(
                "Octagon [:refresh] cached %d-row table for %s",
                len(table), event_ticker,
            )
        else:
            log.warning("Octagon [:refresh] no parseable table for %s", event_ticker)
    except httpx.TimeoutException:
        log.warning("Octagon [:refresh] timeout for %s (%ss limit)", event_ticker, TIMEOUT_REFRESH_S)
        _status["last_fail_ts"] = time.time()
    except Exception as exc:
        log.warning("Octagon [:refresh] error for %s: %s", event_ticker, exc)
        _status["last_fail_ts"] = time.time()
    finally:
        _refresh_pending.discard(event_ticker)


# ── Primary fetch ─────────────────────────────────────────────────────────────

def _fetch_report(market_url: str, event_ticker: str, ttl: int) -> Optional[dict]:
    """
    Return the cached parsed table or fetch a fresh one.

    1. If local TTL cache is warm → return immediately (no HTTP call).
    2. Call :cache (fast, free) → parse the report.
    3. If :cache returns no report → trigger background :refresh thread.
       The current call returns None (BV3 fallback will be used); the next
       call within the same 15m window will hit the warm local cache.
    """
    now = time.time()
    cached = _report_cache.get(event_ticker)
    if cached is not None and (now - cached[1]) < ttl:
        _status["hits"] += 1
        return cached[0]

    api_key = os.environ.get("OCTAGON_API_KEY", "")
    if not api_key:
        return None

    _status["key_present"] = True
    _status["calls"] += 1

    try:
        client = _get_http()
        resp = client.post(
            OCTAGON_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"model": OCTAGON_MODEL, "input": market_url},
        )
        resp.raise_for_status()
        _status["last_ok_ts"] = now
        text = _extract_text_from_response(resp.json())
    except httpx.TimeoutException:
        log.warning("Octagon: timeout for %s (%.1fs limit)", event_ticker, TIMEOUT_CACHE_S)
        _status["last_fail_ts"] = now
        return None
    except httpx.RequestError as exc:
        log.warning("Octagon: request error for %s: %s", event_ticker, exc)
        _status["last_fail_ts"] = now
        return None
    except (KeyError, IndexError, ValueError) as exc:
        log.warning("Octagon: unexpected response for %s: %s", event_ticker, exc)
        _status["last_fail_ts"] = now
        return None

    if not text:
        # :cache has no report — kick off background :refresh
        if event_ticker not in _refresh_pending:
            _refresh_pending.add(event_ticker)
            _status["refresh_calls"] += 1
            threading.Thread(
                target=_background_refresh,
                args=(event_ticker, market_url, ttl),
                daemon=True,
                name=f"octagon-refresh-{event_ticker}",
            ).start()
            log.info("Octagon: no cache for %s — refresh started in background", event_ticker)
        return None

    table = _parse_table(text)
    if table:
        _report_cache[event_ticker] = (table, now)
    else:
        # Report exists but table couldn't be parsed (format issue) — also refresh
        log.warning("Octagon: unparseable table for %s — triggering refresh", event_ticker)
        if event_ticker not in _refresh_pending:
            _refresh_pending.add(event_ticker)
            _status["refresh_calls"] += 1
            threading.Thread(
                target=_background_refresh,
                args=(event_ticker, market_url, ttl),
                daemon=True,
                name=f"octagon-refresh-{event_ticker}",
            ).start()
    return table or None


# ── Public API ───────────────────────────────────────────────────────────────

_FALLTHROUGH = (None, None, None, False)


def query(
    ticker: str,
    strike: float,
    yes_ask: float,
    no_ask: float,
    side: Optional[str],
    is_15m: bool,
) -> tuple[Optional[float], Optional[bool], Optional[str], bool]:
    """
    Query Octagon for a prediction on the given Kalshi contract.

    Returns (model_prob, direction_agrees, confidence, cache_hit).
    Returns (None, None, None, False) on any error — caller falls back to BV3.

    confidence: "high" (>=5pp delta), "medium" (>=2pp), "low" (<2pp)
    direction_agrees: True when Octagon's implied direction matches `side`;
                      None when side is None (caller derives direction from model_prob)
    """
    try:
        parts = ticker.split("-")
        if len(parts) < 3:
            return _FALLTHROUGH
        event_ticker = "-".join(parts[:-1])
        ttl = TTL_15M if is_15m else TTL_HOURLY

        market_url = _build_market_url(ticker)
        if market_url is None:
            return _FALLTHROUGH

        cached_before = _report_cache.get(event_ticker)
        cache_hit = cached_before is not None and (time.time() - cached_before[1]) < ttl

        table = _fetch_report(market_url, event_ticker, ttl)
        if not table:
            return _FALLTHROUGH

        strike_int = int(strike)
        row = table.get(strike_int)
        if row is None:
            closest = min(table.keys(), key=lambda k: abs(k - strike_int))
            if abs(closest - strike_int) > strike_int * 0.01:
                log.warning(
                    "Octagon: no table row within 1%% of strike %d for %s (closest=%d)",
                    strike_int, ticker, closest,
                )
                return (None, None, None, cache_hit)
            row = table[closest]

        market_prob, model_prob = row

        octagon_bullish = model_prob > market_prob
        direction_agrees = (octagon_bullish == (side == "yes")) if side is not None else None

        diff = abs(model_prob - market_prob)
        if diff >= 0.05:
            confidence = "high"
        elif diff >= 0.02:
            confidence = "medium"
        else:
            confidence = "low"

        log.info(
            "Octagon [%s] strike=%d side=%s model=%.1f%% market=%.1f%% "
            "agrees=%s conf=%s cache=%s",
            ticker, strike_int, side or "N/A",
            model_prob * 100, market_prob * 100,
            direction_agrees, confidence, cache_hit,
        )
        return model_prob, direction_agrees, confidence, cache_hit

    except Exception as exc:
        log.warning("Octagon: unexpected error for %s: %s", ticker, exc)
        _status["last_fail_ts"] = time.time()
        return _FALLTHROUGH
