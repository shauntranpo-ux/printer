"""
Octagon AI prediction-market research client.

Singleton with two-level cache:
  1. _slug_cache  — event slug per series (permanent, one Kalshi API call per series)
  2. _report_cache — parsed Octagon table per event window (TTL: 900s/15m, 3300s/hourly)

query() is the only public entry point. Returns (None, None, None, False) on any
error or timeout so the caller can fall through (allow trade) without blocking.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Optional

import httpx

log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

OCTAGON_API_URL = "https://api.octagonagents.com/v1/responses"
OCTAGON_MODEL   = "octagon-prediction-markets-agent:cache"
KALSHI_EVENTS_URL = "https://api.elections.kalshi.com/trade-api/v2/events/{}"
TIMEOUT_S = 3.0
TTL_15M   = 900    # 15 minutes
TTL_HOURLY = 3300  # 55 minutes

# ── Module-level singletons ──────────────────────────────────────────────────

_http: Optional[httpx.Client] = None

# series_ticker (e.g. "kxbtcd") → event_slug (e.g. "bitcoin-price-above-below")
_slug_cache: dict[str, str] = {}

# event_ticker (e.g. "KXBTCD-26APR2300") → (parsed_table, fetched_at)
# parsed_table: dict[int, tuple[float, float]]  strike_int → (market_pct, model_pct)
_report_cache: dict[str, tuple[dict, float]] = {}


def _get_http() -> httpx.Client:
    global _http
    if _http is None:
        _http = httpx.Client(timeout=TIMEOUT_S)
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
    event_ticker = "-".join(parts[:-1])          # strip contract-level suffix
    event_slug = _get_event_slug(series, event_ticker)
    if event_slug is None:
        return None
    return f"https://kalshi.com/markets/{series}/{event_slug}/{event_ticker.lower()}"


# ── Report fetching ──────────────────────────────────────────────────────────

def _parse_table(text: str) -> dict[int, tuple[float, float]]:
    """
    Extract the markdown prediction table from an Octagon report.
    Returns {strike_int: (market_prob, model_prob)}.
    """
    lines = text.splitlines()
    table_lines = [ln for ln in lines if ln.count("|") >= 3]
    if not table_lines:
        return {}

    # Find all header rows (one per table in the report); keep the parse of each
    # and return whichever table yields the most strike rows (prefer "Model vs Market"
    # over the smaller "Who Wins and Why" summary table that appears first).
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
            if "above" not in outcome.lower():
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
            strike_int = max(int(n) for n in nums)
            result[strike_int] = (market_pct, model_pct)

        if len(result) > len(best):
            best = result

    return best


def _fetch_report(market_url: str, event_ticker: str, ttl: int) -> Optional[dict]:
    """Return the cached parsed table or fetch a fresh one from Octagon."""
    now = time.time()
    cached = _report_cache.get(event_ticker)
    if cached is not None and (now - cached[1]) < ttl:
        return cached[0]

    api_key = os.environ.get("OCTAGON_API_KEY", "")
    if not api_key:
        # Don't spam logs on every decide(); caller will fall through silently
        return None

    try:
        client = _get_http()
        resp = client.post(
            OCTAGON_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": OCTAGON_MODEL, "input": market_url},
        )
        resp.raise_for_status()
        data = resp.json()
        # Octagon native format: {"latest_report": {"markdown_report": "..."}}
        # Fall back to OpenAI-style output wrapper if native key absent
        if "latest_report" in data and isinstance(data.get("latest_report"), dict):
            text = data["latest_report"].get("markdown_report", "")
        else:
            text = data["output"][0]["content"][0]["text"]
    except httpx.TimeoutException:
        log.warning("Octagon: timeout fetching report for %s (3s limit)", event_ticker)
        return None
    except httpx.RequestError as exc:
        log.warning("Octagon: request error for %s: %s", event_ticker, exc)
        return None
    except (KeyError, IndexError, ValueError) as exc:
        log.warning("Octagon: unexpected response format for %s: %s", event_ticker, exc)
        return None

    table = _parse_table(text)
    if table:
        _report_cache[event_ticker] = (table, now)
    else:
        log.warning("Octagon: no parseable table in report for %s", event_ticker)
    return table or None


# ── Public API ───────────────────────────────────────────────────────────────

_FALLTHROUGH = (None, None, None, False)


def query(
    ticker: str,
    strike: float,
    yes_ask: float,
    no_ask: float,
    side: str,
    is_15m: bool,
) -> tuple[Optional[float], Optional[bool], Optional[str], bool]:
    """
    Query Octagon for a prediction on the given Kalshi contract.

    Returns (model_prob, direction_agrees, confidence, cache_hit).
    Returns (None, None, None, False) on any error — caller must fall through.

    confidence: "high" (>=5pp delta), "medium" (>=2pp), "low" (<2pp)
    direction_agrees: True when Octagon's implied direction matches `side`
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

        # Determine cache hit before fetch (fetch updates cache internally)
        cached = _report_cache.get(event_ticker)
        cache_hit = cached is not None and (time.time() - cached[1]) < ttl

        table = _fetch_report(market_url, event_ticker, ttl)
        if not table:
            return _FALLTHROUGH

        # Match row by strike — try exact int match, then nearest
        strike_int = int(strike)
        row = table.get(strike_int)
        if row is None:
            # Fallback: pick the key closest to the target strike
            closest = min(table.keys(), key=lambda k: abs(k - strike_int))
            if abs(closest - strike_int) > strike_int * 0.01:  # > 1% off → give up
                log.warning(
                    "Octagon: no table row within 1%% of strike %d for %s", strike_int, ticker
                )
                return _FALLTHROUGH
            row = table[closest]

        market_prob, model_prob = row

        # direction_agrees: Octagon bullish (model > market) → favours YES
        octagon_bullish = model_prob > market_prob
        direction_agrees = octagon_bullish == (side == "yes")

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
            ticker, strike_int, side,
            model_prob * 100, market_prob * 100,
            direction_agrees, confidence, cache_hit,
        )
        return model_prob, direction_agrees, confidence, cache_hit

    except Exception as exc:
        log.warning("Octagon: unexpected error for %s: %s", ticker, exc)
        return _FALLTHROUGH
