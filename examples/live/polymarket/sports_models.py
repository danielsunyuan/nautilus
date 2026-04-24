from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


SportTag = Literal["nba", "soccer", "esports", "mma", "tennis", "boxing", "cricket"]
SportsMarketType = Literal["moneyline", "totals", "spreads"]
PriceArena = Literal["sports_50c", "sports_60c", "sports_70c", "sports_80c", "sports_90c"]
TradeOutcome = Literal["win", "loss", "unresolved"]


def classify_price_arena(price: float) -> PriceArena | None:
    """Classify a price into a 10-cent sports arena, or None if out of range."""
    value = float(price)
    if 0.50 <= value < 0.60:
        return "sports_50c"
    if 0.60 <= value < 0.70:
        return "sports_60c"
    if 0.70 <= value < 0.80:
        return "sports_70c"
    if 0.80 <= value < 0.90:
        return "sports_80c"
    if 0.90 <= value <= 0.98:
        return "sports_90c"
    return None


def classify_resolved_trade(
    *,
    resolved: bool,
    settlement_price: float | None,
    pnl: float | None,
) -> TradeOutcome:
    """Classify a resolved trade as win, loss, or unresolved."""
    if not resolved:
        return "unresolved"
    if settlement_price == 1.0 and pnl is not None and pnl > 0:
        return "win"
    return "loss"


def breakeven_win_rate(entry_price: float) -> float:
    """Return the minimum win rate needed to break even at this entry price."""
    return float(entry_price)  # breakeven = cost / 1.00 payout


@dataclass(frozen=True, slots=True)
class SportsMarket:
    slug: str                    # Gamma market slug
    condition_id: str            # 0x... condition ID
    sport: str                   # "nba", "esports", etc.
    match_title: str             # "Raptors vs. Cavaliers"
    market_type: str             # "moneyline", "totals", "spreads"
    outcome_name: str            # "Raptors", "Cavaliers", "Over", "Under"
    token_id: str                # specific token ID for this outcome
    game_time: str               # ISO string from endDate, or ""
    active: bool
    accepting_orders: bool
    current_price: float | None = None


def select_highest_price_outcome_per_condition(
    markets: list[SportsMarket],
) -> list[SportsMarket]:
    """Keep one deterministic highest-priced outcome per condition_id."""
    best_by_condition: dict[str, SportsMarket] = {}
    condition_order: list[str] = []

    for market in markets:
        condition_id = market.condition_id
        if condition_id not in best_by_condition:
            best_by_condition[condition_id] = market
            condition_order.append(condition_id)
            continue

        incumbent = best_by_condition[condition_id]
        incumbent_price = -1.0 if incumbent.current_price is None else float(incumbent.current_price)
        candidate_price = -1.0 if market.current_price is None else float(market.current_price)

        if candidate_price > incumbent_price:
            best_by_condition[condition_id] = market
            continue

        if candidate_price == incumbent_price:
            candidate_key = (market.token_id, market.outcome_name.casefold())
            incumbent_key = (incumbent.token_id, incumbent.outcome_name.casefold())
            if candidate_key < incumbent_key:
                best_by_condition[condition_id] = market

    return [best_by_condition[condition_id] for condition_id in condition_order]
