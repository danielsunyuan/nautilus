"""
Daily temperature market discovery and parsing for Polymarket weather markets.

Parses Gamma API market dicts into structured DailyTemperatureMarket objects.
Conservative parser — rejects anything it cannot normalize safely.
"""

from __future__ import annotations

import json as _json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any


# ---------------------------------------------------------------------------
# Month name → number mapping
# ---------------------------------------------------------------------------

_MONTH_MAP: dict[str, int] = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

# ---------------------------------------------------------------------------
# Regex for temperature market questions
# ---------------------------------------------------------------------------

# Actual Polymarket patterns observed:
#   "Will the highest temperature in London be 13°C on April 14?"
#   "Will the highest temperature in Madrid be 15°C or below on April 14?"
#   "Will the highest temperature in London be 19°C or higher on April 15?"
#   "Will the highest temperature in Seattle be between 44-45°F on April 15?"
#   "Will the high temperature in New York City on April 15, 2026 be 70°F or above?"

# Pattern 1: "Will the highest/lowest temperature in CITY be TEMP°C/°F [qualifier] on MONTH DAY[, YEAR]?"
_QUESTION_RE_V2 = re.compile(
    r"Will the (highest|lowest|high|low) temperature in (.+?) be "
    r"(?:between\s+)?(\d+(?:\.\d+)?)"
    r"(?:\s*-\s*\d+(?:\.\d+)?)?"  # optional range end (e.g., "44-45")
    r"\s*°\s*(C|F)"
    r"(?:\s+or\s+(below|above|higher|lower))?"
    r"\s+on\s+(\w+)\s+(\d{1,2})(?:,?\s*(\d{4}))?\?",
    re.IGNORECASE,
)

# Pattern 2 (legacy): "Will the high temperature in CITY on MONTH DAY, YEAR be TEMP°F or above?"
_QUESTION_RE_LEGACY = re.compile(
    r"Will the (high|low) temperature in (.+?) on "
    r"(\w+) (\d{1,2}),?\s*(\d{4}) "
    r"be (\d+(?:\.\d+)?)\s*(?:°|degrees?\s*)F"
    r"\s+or\s+(above|below)\?",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class DailyTemperatureMarket:
    slug: str
    condition_id: str
    city: str
    observation_date: date
    metric: str  # "high" or "low"
    threshold_f: float
    yes_token_id: str
    no_token_id: str
    active: bool
    accepting_orders: bool
    best_ask: float | None = None  # YES token best ask from Gamma (None if unavailable)
    band_type: str = "or_higher"  # "exact" | "or_higher" | "or_lower"


def _extract_tokens(tokens: list[dict]) -> tuple[str, str] | None:
    """Extract (yes_token_id, no_token_id) from token list, or None if invalid."""
    yes_id: str | None = None
    no_id: str | None = None
    for tok in tokens:
        outcome = tok.get("outcome", "").strip().lower()
        token_id = tok.get("token_id", "")
        if outcome == "yes":
            yes_id = token_id
        elif outcome == "no":
            no_id = token_id
    if yes_id and no_id:
        return yes_id, no_id
    return None


def _normalize_metric(raw: str) -> str:
    """Normalize metric to 'high' or 'low'."""
    lower = raw.lower()
    if lower in ("highest", "high"):
        return "high"
    if lower in ("lowest", "low"):
        return "low"
    return lower


def _infer_year(month_num: int, day: int) -> int:
    """Infer the year when not provided in the question.

    Polymarket daily temperature markets are near-term, so pick the closest
    future occurrence of the given month/day.
    """
    from datetime import date as _date

    today = _date.today()
    candidate = _date(today.year, month_num, day)
    # If the date is more than 30 days in the past, assume next year
    if (today - candidate).days > 30:
        return today.year + 1
    return today.year


def parse_daily_temperature_market(
    gamma_market: dict,
) -> DailyTemperatureMarket | None:
    """Parse a Gamma API market dict into a DailyTemperatureMarket, or None if not parseable."""
    question = gamma_market.get("question", "")
    if not question:
        return None

    tokens = gamma_market.get("tokens", [])
    token_pair = _extract_tokens(tokens)
    if token_pair is None:
        return None
    yes_token_id, no_token_id = token_pair

    # Try v2 pattern first (actual Polymarket format)
    match = _QUESTION_RE_V2.match(question)
    if match is not None:
        metric_raw = match.group(1)
        city = match.group(2)
        threshold_str = match.group(3)
        unit = match.group(4)  # C or F
        # group(5) is qualifier (below/above/higher/lower) — optional
        month_str = match.group(6)
        day_str = match.group(7)
        year_str = match.group(8)  # may be None

        metric = _normalize_metric(metric_raw)
        month_num = _MONTH_MAP.get(month_str.lower())
        if month_num is None:
            return None

        try:
            day = int(day_str)
            year = int(year_str) if year_str else _infer_year(month_num, day)
            observation_date = date(year, month_num, day)
        except (ValueError, TypeError):
            return None

        threshold = float(threshold_str)

        # Derive band_type from qualifier group
        qualifier = (match.group(5) or "").lower()
        if qualifier in ("higher", "above"):
            band_type = "or_higher"
        elif qualifier in ("lower", "below"):
            band_type = "or_lower"
        else:
            band_type = "exact"  # no qualifier → exact band (e.g. "be 16°C")

        try:
            best_ask: float | None = float(gamma_market["bestAsk"]) if gamma_market.get("bestAsk") is not None else None
        except (ValueError, TypeError):
            best_ask = None
        return DailyTemperatureMarket(
            slug=gamma_market.get("slug", ""),
            condition_id=gamma_market.get("condition_id") or gamma_market.get("conditionId", ""),
            city=city,
            observation_date=observation_date,
            metric=metric,
            threshold_f=threshold,  # NOTE: may be °C despite field name
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            active=bool(gamma_market.get("active", False)),
            accepting_orders=bool(gamma_market.get("accepting_orders") or gamma_market.get("acceptingOrders", False)),
            best_ask=best_ask,
            band_type=band_type,
        )

    # Try legacy pattern (original assumed format)
    match = _QUESTION_RE_LEGACY.match(question)
    if match is not None:
        metric_raw, city, month_str, day_str, year_str, threshold_str, _direction = (
            match.groups()
        )
        metric = _normalize_metric(metric_raw)
        month_num = _MONTH_MAP.get(month_str.lower())
        if month_num is None:
            return None
        try:
            observation_date = date(int(year_str), month_num, int(day_str))
        except ValueError:
            return None

        direction = _direction.lower()
        band_type = "or_higher" if direction in ("above",) else "or_lower"
        return DailyTemperatureMarket(
            slug=gamma_market.get("slug", ""),
            condition_id=gamma_market.get("condition_id") or gamma_market.get("conditionId", ""),
            city=city,
            observation_date=observation_date,
            metric=metric,
            threshold_f=float(threshold_str),
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            active=bool(gamma_market.get("active", False)),
            accepting_orders=bool(gamma_market.get("accepting_orders") or gamma_market.get("acceptingOrders", False)),
            best_ask=float(gamma_market["bestAsk"]) if gamma_market.get("bestAsk") is not None else None,
            band_type=band_type,
        )

    return None


_GAMMA_PAGE_LIMIT = 500


async def _fetch_gamma_page(
    *,
    http_client: Any,
    gamma_base_url: str,
    params: dict[str, str],
    timeout: float,
) -> list[dict]:
    """Fetch a single page from Gamma /markets. Returns list of market dicts."""
    response = await http_client.get(
        f"{gamma_base_url}/markets",
        params=params,
        timeout_secs=int(timeout),
    )
    if response.status >= 400:
        raise RuntimeError(f"Gamma API returned HTTP {response.status}")
    data = _json.loads(response.body)
    return data if isinstance(data, list) else []


async def _paginated_gamma_fetch(
    *,
    http_client: Any,
    gamma_base_url: str,
    base_params: dict[str, str],
    timeout: float,
    limit: int = _GAMMA_PAGE_LIMIT,
    max_retries: int = 3,
) -> list[dict]:
    """Fetch all pages from Gamma /markets with offset pagination."""
    import asyncio as _asyncio

    all_markets: list[dict] = []
    offset = 0
    consecutive_failures = 0
    while True:
        params = {**base_params, "limit": str(limit), "offset": str(offset)}
        page: list[dict] = []
        for attempt in range(max_retries):
            try:
                page = await _fetch_gamma_page(
                    http_client=http_client,
                    gamma_base_url=gamma_base_url,
                    params=params,
                    timeout=timeout,
                )
                consecutive_failures = 0
                break
            except Exception:
                if attempt < max_retries - 1:
                    await _asyncio.sleep(2 ** attempt)
                else:
                    consecutive_failures += 1
        if consecutive_failures >= 2:
            break
        all_markets.extend(page)
        if len(page) < limit:
            break
        offset += limit
    return all_markets


async def _fetch_weather_events(
    *,
    http_client: Any,
    gamma_base_url: str,
    timeout: float,
    active: bool = True,
    closed: bool = False,
) -> list[dict]:
    """Fetch weather events from Gamma ``/events?tag_slug=weather``.

    Returns the nested market dicts extracted from each event.
    This is much faster than paginating ``/markets`` (hundreds of markets
    vs tens of thousands).
    """
    import asyncio as _asyncio

    all_markets: list[dict] = []
    offset = 0
    limit = 100
    params: dict[str, str] = {"tag_slug": "weather", "limit": str(limit)}
    if active:
        params["active"] = "true"
    if closed:
        params["closed"] = "true"
    else:
        params["closed"] = "false"

    while True:
        page_params = {**params, "offset": str(offset)}
        qs = "&".join(f"{k}={v}" for k, v in page_params.items())
        url = f"{gamma_base_url}/events?{qs}"

        page_events: list[dict] = []
        for attempt in range(3):
            try:
                response = await http_client.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"},
                    timeout_secs=int(timeout),
                )
                if response.status >= 400:
                    raise RuntimeError(f"Gamma events API returned HTTP {response.status}")
                page_events = _json.loads(response.body)
                if not isinstance(page_events, list):
                    page_events = []
                break
            except Exception:
                if attempt < 2:
                    await _asyncio.sleep(2 ** attempt)

        for event in page_events:
            markets = event.get("markets", [])
            if not isinstance(markets, list):
                continue
            for market in markets:
                # The /events endpoint omits the ``tokens`` list that
                # ``parse_daily_temperature_market`` needs.  Synthesize it
                # from ``clobTokenIds`` + ``outcomes`` when missing.
                if "tokens" not in market:
                    clob_ids = market.get("clobTokenIds")
                    outcomes = market.get("outcomes")
                    if isinstance(clob_ids, str):
                        try:
                            clob_ids = _json.loads(clob_ids)
                        except (ValueError, TypeError):
                            clob_ids = None
                    if isinstance(outcomes, str):
                        try:
                            outcomes = _json.loads(outcomes)
                        except (ValueError, TypeError):
                            outcomes = None
                    if isinstance(clob_ids, list) and isinstance(outcomes, list) and len(clob_ids) == len(outcomes):
                        market["tokens"] = [
                            {"token_id": tid, "outcome": outcome}
                            for tid, outcome in zip(clob_ids, outcomes)
                        ]
                all_markets.append(market)

        if len(page_events) < limit:
            break
        offset += limit

    return all_markets


async def discover_daily_temperature_markets(
    *,
    http_client: Any,
    gamma_base_url: str,
    timeout: float = 15.0,
    include_closed: bool = False,
) -> list[DailyTemperatureMarket]:
    """Discover daily temperature markets from Gamma API.

    Uses the ``/events?tag_slug=weather`` endpoint to fetch weather events
    (typically ~100 events with ~10 markets each), then applies the
    conservative regex parser to identify daily temperature markets.

    Args:
        include_closed: If True, also fetch closed/resolved markets
                        (needed by settlement resolver).
    """
    results: list[DailyTemperatureMarket] = []

    # Fetch active weather markets via events endpoint
    raw_active = await _fetch_weather_events(
        http_client=http_client,
        gamma_base_url=gamma_base_url,
        timeout=timeout,
        active=True,
        closed=False,
    )
    for raw in raw_active:
        parsed = parse_daily_temperature_market(raw)
        if parsed is not None:
            results.append(parsed)

    # Optionally fetch closed markets (for settlement resolution)
    if include_closed:
        raw_closed = await _fetch_weather_events(
            http_client=http_client,
            gamma_base_url=gamma_base_url,
            timeout=timeout,
            active=False,
            closed=True,
        )
        seen_slugs = {m.slug for m in results}
        for raw in raw_closed:
            parsed = parse_daily_temperature_market(raw)
            if parsed is not None and parsed.slug not in seen_slugs:
                results.append(parsed)

    return results
