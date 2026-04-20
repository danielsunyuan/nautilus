"""
Vegas odds fetcher for CLV (Closing Line Value) comparison.

Fetches h2h (moneyline) and totals odds from The Odds API and converts to
implied probabilities for comparison against Polymarket ask prices.

Requires env var THE_ODDS_API_KEY. Returns None gracefully if key is missing
or API call fails — callers should treat None as "no data, don't block entry".
"""
from __future__ import annotations

import os
from typing import Any

ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports"

POLYMARKET_SPORT_TO_ODDS_API: dict[str, str] = {
    "nba": "basketball_nba",
    "tennis": "tennis_atp",   # ATP — WTA is "tennis_wta"
    "ufc": "mma_mixed_martial_arts",
    "mlb": "baseball_mlb",
}


def american_to_implied_prob(american_odds: float) -> float:
    """Convert American odds to implied probability (no vig removal)."""
    if american_odds > 0:
        return 100 / (american_odds + 100)
    return abs(american_odds) / (abs(american_odds) + 100)


def has_clv_edge(
    *,
    polymarket_ask: float,
    vegas_implied: float | None,
    min_edge: float = 0.05,
) -> bool:
    """
    Return True if Polymarket is underpriced vs Vegas by at least min_edge,
    OR if no Vegas data is available (don't block on missing data).
    """
    if vegas_implied is None:
        return True
    return (vegas_implied - polymarket_ask) >= min_edge


async def fetch_implied_prob(
    *,
    sport: str,
    home_team: str,
    away_team: str,
    outcome_name: str,
    http_client: Any,
    market: str = "h2h",
) -> float | None:
    """
    Fetch implied probability for a specific outcome from The Odds API.

    Returns None if API key missing, call fails, or team not found.
    Callers should treat None as "no data, don't block entry".
    """
    api_key = os.getenv("THE_ODDS_API_KEY")
    if not api_key:
        return None
    sport_key = POLYMARKET_SPORT_TO_ODDS_API.get(sport)
    if not sport_key:
        return None
    try:
        resp = await http_client.get(
            f"{ODDS_API_BASE}/{sport_key}/odds/",
            params={"apiKey": api_key, "regions": "us", "markets": market, "oddsFormat": "american"},
        )
        resp.raise_for_status()
        events = resp.json()
    except Exception:
        return None
    for event in events:
        teams = {event.get("home_team", "").lower(), event.get("away_team", "").lower()}
        if home_team.lower() not in teams and away_team.lower() not in teams:
            continue
        for bookmaker in event.get("bookmakers", [])[:3]:  # use first 3 books, average
            for mkt in bookmaker.get("markets", []):
                if mkt.get("key") != market:
                    continue
                for outcome in mkt.get("outcomes", []):
                    if outcome.get("name", "").lower() in outcome_name.lower() or \
                       outcome_name.lower() in outcome.get("name", "").lower():
                        return american_to_implied_prob(float(outcome["price"]))
    return None
