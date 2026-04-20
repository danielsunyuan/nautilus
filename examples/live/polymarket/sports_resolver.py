"""
Sports market discovery and parsing for Polymarket sports markets.

Parses Gamma API market dicts into structured SportsMarket objects.
Conservative parser — rejects anything it cannot normalize safely.
"""

from __future__ import annotations

import importlib.util
import json as _json
from datetime import datetime
from pathlib import Path
import sys
from typing import Any

try:
    from examples.live.polymarket.sports_models import SportsMarket
except ModuleNotFoundError:
    module_name = "examples.live.polymarket.sports_models"
    module_path = Path(__file__).resolve().with_name("sports_models.py")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    SportsMarket = module.SportsMarket

# Tag slugs we query from Gamma.
# Only tags that have game-level binary markets (not just championship futures).
# Each was validated to return markets with sportsMarketType set.
#
#   nba      — NBA game moneyline / spreads / totals
#   tennis   — ATP/WTA moneyline
#   boxing   — boxing bouts moneyline
#   mlb      — MLB game moneyline / spreads / totals / nrfi (no-run-first-inning)
#   ufc      — UFC fight moneyline / method-of-victory / go-the-distance
#   hockey   — KHL moneyline (NHL has no game-level typed markets on Polymarket)
#
# Not included: nhl (0 typed markets), soccer/mls (futures only), mma (alias for ufc)
SPORTS_TAGS = ["nba", "tennis", "boxing", "mlb", "ufc", "hockey"]

# Market types we include (must match Gamma's sportsMarketType field)
# nrfi = No Run First Inning (baseball prop)
# ufc_method_of_victory = KO/TKO/submission prop
# ufc_go_the_distance = goes all scheduled rounds
SPORTS_MARKET_TYPES = {
    "moneyline",
    "totals",
    "spreads",
    "nrfi",
    "ufc_method_of_victory",
    "ufc_go_the_distance",
}


def _parse_outcome_prices(prices_str: str | list) -> list[float] | None:
    """Parse outcome prices from JSON string or list."""
    if isinstance(prices_str, list):
        try:
            return [float(p) for p in prices_str]
        except (ValueError, TypeError):
            return None
    if isinstance(prices_str, str):
        try:
            parsed = _json.loads(prices_str)
            if isinstance(parsed, list):
                return [float(p) for p in parsed]
        except (ValueError, TypeError):
            pass
    return None


def _parse_clob_token_ids(clob_ids_str: str | list) -> list[str] | None:
    """Parse clobTokenIds from JSON string or list."""
    if isinstance(clob_ids_str, list):
        return [str(t) for t in clob_ids_str]
    if isinstance(clob_ids_str, str):
        try:
            parsed = _json.loads(clob_ids_str)
            if isinstance(parsed, list):
                return [str(t) for t in parsed]
        except (ValueError, TypeError):
            pass
    return None


def _parse_outcomes(outcomes_str: str | list) -> list[str] | None:
    """Parse outcomes from JSON string or list."""
    if isinstance(outcomes_str, list):
        return [str(o) for o in outcomes_str]
    if isinstance(outcomes_str, str):
        try:
            parsed = _json.loads(outcomes_str)
            if isinstance(parsed, list):
                return [str(o) for o in parsed]
        except (ValueError, TypeError):
            pass
    return None


def _parse_game_time(end_date: str | int | None) -> str:
    """Return game time as an ISO string.

    Handles both ISO string values (from /events endpoint) and Unix-millisecond
    integers (from some /markets responses).
    """
    if end_date is None:
        return ""
    # Already an ISO string (most common via /events)
    if isinstance(end_date, str) and end_date:
        return end_date
    # Unix milliseconds integer
    if isinstance(end_date, (int, float)) and end_date > 0:
        try:
            dt = datetime.utcfromtimestamp(end_date / 1000.0)
            return dt.isoformat() + "Z"
        except (ValueError, OSError):
            return ""
    return ""


def parse_sports_market(
    gamma_market: dict,
    sport_tag: str,
) -> list[SportsMarket]:
    """
    Parse a Gamma API market dict into a list of SportsMarket objects (one per outcome).

    Returns [] if not parseable or not a supported market type.

    Args:
        gamma_market: The market dict from Gamma API
        sport_tag: The sport tag used in the query (e.g., "nba", "esports")
    """
    slug = gamma_market.get("slug", "")
    condition_id = gamma_market.get("condition_id") or gamma_market.get("conditionId", "")
    if not slug or not condition_id:
        return []

    # Extract market type
    market_type = gamma_market.get("sportsMarketType", "").lower().strip()
    if market_type not in SPORTS_MARKET_TYPES:
        return []

    # Extract question/title
    question = gamma_market.get("question", "").strip()
    match_title = gamma_market.get("title", question).strip()
    if not match_title:
        return []

    # Extract game time
    end_date = gamma_market.get("endDate")
    game_time = _parse_game_time(end_date)

    # Extract active/accepting_orders
    active = bool(gamma_market.get("active", False))
    accepting_orders = bool(gamma_market.get("accepting_orders") or gamma_market.get("acceptingOrders", False))

    # Extract outcome names
    outcomes = _parse_outcomes(gamma_market.get("outcomes"))
    if not outcomes:
        return []

    # Extract outcome prices
    outcome_prices = _parse_outcome_prices(gamma_market.get("outcomePrices"))
    if not outcome_prices or len(outcome_prices) != len(outcomes):
        return []

    # Extract token IDs
    clob_ids = _parse_clob_token_ids(gamma_market.get("clobTokenIds"))
    if not clob_ids or len(clob_ids) != len(outcomes):
        return []

    # Build SportsMarket objects: one per outcome, filtering by price in reasonable band
    results: list[SportsMarket] = []
    for outcome_name, price, token_id in zip(outcomes, outcome_prices, clob_ids):
        # Filter: only include prices in [0.40, 0.98] to catch tradeable outcomes
        if price < 0.40 or price > 0.98:
            continue

        results.append(
            SportsMarket(
                slug=slug,
                condition_id=condition_id,
                sport=sport_tag,
                match_title=match_title,
                market_type=market_type,
                outcome_name=outcome_name.strip(),
                token_id=token_id,
                game_time=game_time,
                active=active,
                accepting_orders=accepting_orders,
            ),
        )

    return results


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
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
    )
    if response.status >= 400:
        raise RuntimeError(f"Gamma API returned HTTP {response.status}")
    data = _json.loads(response.body)
    return data if isinstance(data, list) else []


async def _fetch_sports_events(
    *,
    http_client: Any,
    gamma_base_url: str,
    tag_slug: str,
    timeout: float,
) -> list[dict]:
    """
    Fetch sports events from Gamma ``/events?tag_slug={tag_slug}``.

    Returns the nested market dicts extracted from each event.
    """
    import asyncio as _asyncio

    all_markets: list[dict] = []
    offset = 0
    limit = 100
    max_pages = 5  # cap at 500 events per tag to avoid slow pagination on huge categories
    page_num = 0
    params: dict[str, str] = {"tag_slug": tag_slug, "limit": str(limit), "active": "true", "closed": "false"}

    while page_num < max_pages:
        page_params = {**params, "offset": str(offset)}
        qs = "&".join(f"{k}={v}" for k, v in page_params.items())
        url = f"{gamma_base_url}/events?{qs}"

        page_events: list[dict] = []
        for attempt in range(3):
            try:
                response = await http_client.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
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
                # Synthesize tokens from clobTokenIds + outcomes if missing
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

        page_num += 1
        if len(page_events) < limit:
            break
        offset += limit

    return all_markets


async def discover_sports_markets(
    *,
    http_client: Any,
    gamma_base_url: str,
    timeout: float = 15.0,
) -> list[SportsMarket]:
    """
    Discover sports markets from Gamma API across all SPORTS_TAGS.

    Uses the ``/events?tag_slug={tag}`` endpoint to fetch sports events,
    then applies the parser to identify market opportunities.

    Deduplicates by (condition_id, outcome_name).
    """
    import asyncio as _asyncio

    all_markets: list[SportsMarket] = []
    seen = set()

    for tag_slug in SPORTS_TAGS:
        try:
            raw_markets = await _fetch_sports_events(
                http_client=http_client,
                gamma_base_url=gamma_base_url,
                tag_slug=tag_slug,
                timeout=timeout,
            )
            for raw_market in raw_markets:
                parsed_list = parse_sports_market(raw_market, tag_slug)
                for parsed in parsed_list:
                    key = (parsed.condition_id, parsed.outcome_name)
                    if key not in seen:
                        seen.add(key)
                        all_markets.append(parsed)
        except Exception:
            # Skip this tag on error, continue with others
            pass

        # Small delay between tag queries to avoid overwhelming API
        await _asyncio.sleep(0.1)

    return all_markets
